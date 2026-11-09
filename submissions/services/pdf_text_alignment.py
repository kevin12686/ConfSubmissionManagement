"""Bounded, occurrence-sensitive alignment of already inspected PDF tokens.

This module knows nothing about files, submissions, publication, or review state.
"""

from collections import Counter
from difflib import SequenceMatcher
import json


MAX_ALIGNMENT_WORK = 2_000_000


def context_key(token):
    context = token.get("context_key")
    if isinstance(context, (list, dict)):
        return json.dumps(context, sort_keys=True, separators=(",", ":"))
    return context or ""


def token_key(token):
    if token.get("opaque_key"):
        return "glyph", token["opaque_key"], context_key(token)
    return "text", token["text"], context_key(token)


def identity_supported(token):
    # A supported identity can still have structural coverage limitations.
    if token.get("opaque_key"):
        return token.get("text_identity_supported") is True
    return token.get("text_identity_supported", not token.get("issues")) is True


def needs_identity_review(token):
    return bool(token.get("opaque_key")) or not identity_supported(token)


def align_tokens(before, after, *, identical_bytes=False, work_limit=MAX_ALIGNMENT_WORK):
    """Retain every token; stop expensive matching explicitly instead of guessing.

    Removing exact common ends first avoids quadratic work on long repeated
    passages with a small edit. For an ambiguous expensive middle, keep its
    entire remainder visible rather than enabling SequenceMatcher's autojunk.
    """
    def keys(items, side):
        return [("unresolved", side, index) if not identity_supported(item) and not identical_bytes
                else token_key(item) for index, item in enumerate(items)]

    a, b = keys(before, "before"), keys(after, "after")
    ca, cb = Counter(a), Counter(b)
    if sum(count * cb[key] for key, count in ca.items()) <= work_limit:
        # Let the whole-document matcher choose anchors. Trimming common words
        # first can consume the first "The" of two reordered columns and split
        # an otherwise exact moved paragraph at both ends.
        return SequenceMatcher(None, a, b, autojunk=False).get_opcodes(), False
    prefix = 0
    while prefix < min(len(a), len(b)) and a[prefix] == b[prefix]:
        prefix += 1
    end_a, end_b = len(a), len(b)
    while end_a > prefix and end_b > prefix and a[end_a - 1] == b[end_b - 1]:
        end_a -= 1
        end_b -= 1
    left, right = a[prefix:end_a], b[prefix:end_b]
    ca, cb = Counter(left), Counter(right)
    limited = sum(count * cb[key] for key, count in ca.items()) > work_limit
    if limited:
        middle = [("replace", 0, len(left), 0, len(right))]
    else:
        middle = SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
    opcodes = [("equal", 0, prefix, 0, prefix)] if prefix else []
    opcodes.extend((op, i + prefix, j + prefix, k + prefix, l + prefix)
                   for op, i, j, k, l in middle)
    if end_a < len(a):
        opcodes.append(("equal", end_a, len(a), end_b, len(b)))
    return opcodes, limited


def split_confidence_ranges(tokens, start, end):
    """An unreadable glyph must not swallow a neighboring, provable deletion."""
    ranges = []
    for index in range(start, end):
        uncertain = needs_identity_review(tokens[index])
        if ranges and ranges[-1][2] == uncertain:
            ranges[-1] = (ranges[-1][0], index + 1, uncertain)
        else:
            ranges.append((index, index + 1, uncertain))
    return [(a, b) for a, b, _ in ranges]


def structure_changes(before, after, matched_ranges):
    """Compare supported glyph-role evidence separately from text content.

    A missing role is not a baseline assertion. Unsupported structures remain
    extraction coverage limitations rather than receiving invented semantics.
    """
    for a0, a1, b0, b1 in matched_ranges:
        for i, j in zip(range(a0, a1), range(b0, b1)):
            left, right = before[i], after[j]
            if not identity_supported(left) or not identity_supported(right):
                continue
            a_meta, b_meta = left.get("char_meta", []), right.get("char_meta", [])
            if len(a_meta) != len(b_meta):
                continue
            changed = []
            for index, (a, b) in enumerate(zip(a_meta, b_meta)):
                ra, rb = a.get("script_role"), b.get("script_role")
                if ra and rb and ra != rb:
                    changed.append(index)
            if changed:
                yield i, j, changed
