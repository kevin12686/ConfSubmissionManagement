"""Read-only, hash-bound comparisons; never publication or review evidence."""

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import struct
import tempfile
import uuid
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path

import fitz
from django.conf import settings
from django.core import signing
from django.utils import timezone

from submissions.models import FinalSubmission, FormattingRevision
from submissions.services.audit import audit_failure, audit_success
from submissions.services.file_inspection import FileChangedDuringInspection, FileInspectionContext
from submissions.services.file_manager import publication_pdf_info
from submissions.services.formatting_history import open_revision_file
from submissions.services.preview_storage import PREVIEW_TOKEN_PATTERN, purge_expired_preview_directories
from submissions.services.publication_decisions import publication_master_paper_ids
from submissions.services.pdf_text_alignment import (
    identity_supported, needs_identity_review, split_confidence_ranges, structure_changes, token_key,
)


TTL = timedelta(hours=2)
SALT = "submissions.pdf-comparison.v1"
COMPARISON_VERSION = 6
MAX_BYTES = 64 * 1024 * 1024
MAX_PAGES = 100
MAX_TOKENS = 30000
MAX_FINDINGS = 300
MAX_BOXES = 160
MAX_PAIRING_STEPS = 250000
ERROR_PATTERN = re.compile(r"Error!\s*(?:Reference source not found|Bookmark not defined)\.?", re.I)
PLACEHOLDER_PATTERN = re.compile(r"\[\s*\?\s*\]|\?\?")


class PDFComparisonError(ValueError):
    pass


class StaleUploadComparison(PDFComparisonError):
    pass


def comparison_root(*, create=False):
    root = Path(settings.BASE_DIR) / "data" / "formatting_comparison_previews"
    if root.is_symlink():
        raise PDFComparisonError("The comparison staging folder cannot be a symbolic link.")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def comparison_submission(submission_id):
    submission = FinalSubmission.objects.filter(
        pk=submission_id, active_version=True, discarded=False,
        paper_id_filled__in=publication_master_paper_ids(),
    ).first()
    if not submission:
        raise PDFComparisonError("This submission is no longer a current Publishing paper. No other version was substituted.")
    if FinalSubmission.objects.filter(
        paper_id_filled=submission.paper_id_filled, active_version=True, discarded=False,
    ).exclude(pk=submission.pk).exists():
        raise PDFComparisonError("Multiple active submissions exist for this paper. Resolve the version ambiguity before comparing publication files.")
    return submission


def comparison_identity(submission):
    return {
        "submission_id": submission.pk, "paper_id": submission.paper_id_filled,
        "final_id": submission.final_submission_id, "origin": submission.submission_origin,
        "original_name": submission.pdf_file.name or "",
        "corrected_name": submission.formatted_pdf_file.name or "",
    }


def validate_upload_binding(submission, binding, *, pdf_sha256=None):
    revision = FormattingRevision.objects.filter(
        pk=binding["revision_id"], submission_id=submission.pk,
        revision_key=binding["revision_key"], event_type="upload",
    ).first()
    if (
        not revision or "pdf" not in revision.changed_files
        or comparison_identity(submission) != binding["identity"]
        or revision.pdf_metadata.get("sha256") != binding["pdf_sha256"]
        or revision.pdf_metadata.get("live_name") != submission.formatted_pdf_file.name
        or revision.pdf_metadata.get("uploaded_at") != (
            submission.formatted_pdf_uploaded_at.isoformat() if submission.formatted_pdf_uploaded_at else None
        )
        or any("pdf" in changed for changed in FormattingRevision.objects.filter(
            submission_id=submission.pk, number__gt=revision.number,
        ).values_list("changed_files", flat=True))
        or (pdf_sha256 is not None and pdf_sha256 != binding["pdf_sha256"])
    ):
        raise StaleUploadComparison("This upload is no longer the current saved revision. No newer PDF was substituted. Open Compare PDF for the current files.")
    return revision


def _live_bytes(info):
    if not info["exists"]:
        raise PDFComparisonError("The selected PDF is missing. No other version was substituted.")
    path = Path(info["path"])
    if path.stat().st_size > MAX_BYTES:
        raise PDFComparisonError("This PDF exceeds the 64 MiB comparison limit. Use the original PDF viewer for manual review.")
    try:
        data = FileInspectionContext().read_snapshot_bytes(path)
    except FileChangedDuringInspection as exc:
        raise PDFComparisonError("The PDF changed while being read. Reload the current files and compare again.") from exc
    if len(data) > MAX_BYTES:
        raise PDFComparisonError("The PDF grew beyond the comparison limit. Reload the current files.")
    return data


def _original_info(submission):
    field = submission.pdf_file
    return {
        "exists": bool(field and field.storage.exists(field.name)),
        "path": field.path if field else "",
    }


def _read_baseline(submission, baseline):
    if baseline == "original":
        data = _live_bytes(_original_info(submission))
        return data, {
            "key": "original", "label": "Original submission",
            "filename": submission.original_file_name or Path(submission.pdf_file.name).name,
        }
    if not re.fullmatch(r"revision-[1-9][0-9]*", baseline):
        raise PDFComparisonError("Choose an original or a revision belonging to this submission.")
    revision = FormattingRevision.objects.filter(pk=int(baseline[9:]), submission=submission).first()
    if not revision:
        raise PDFComparisonError("This revision does not belong to the selected submission.")
    if (revision.pdf_metadata.get("size") or 0) > MAX_BYTES:
        raise PDFComparisonError("The revision PDF exceeds the comparison size limit.")
    with open_revision_file(revision, "pdf") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise PDFComparisonError("The revision PDF exceeds the comparison size limit.")
    return data, {
        "key": baseline, "label": f"Revision {revision.number}",
        "filename": revision.pdf_metadata.get("filename") or f"revision-{revision.number}.pdf",
    }


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _validate_evidence(document):
    if not isinstance(document, dict) or not all(isinstance(document.get(key), list) for key in ("tokens", "words", "pages", "controls")):
        raise ValueError("Invalid document evidence")
    pages = document["pages"]
    if not 0 < len(pages) <= MAX_PAGES:
        raise PDFComparisonError("This PDF exceeds the comparison page limit. Review the full PDFs manually.")
    if max(len(document["tokens"]), len(document["words"])) > MAX_TOKENS:
        raise PDFComparisonError("This PDF exceeds the comparison token limit. Review the full PDFs manually.")
    for number, page in enumerate(pages, 1):
        if page.get("number") != number or any(not isinstance(page.get(key), (int, float)) or not math.isfinite(page[key]) or page[key] <= 0 for key in ("width", "height")):
            raise ValueError("Invalid page evidence")
        source_key = page.get("render_source_key")
        if source_key is not None and (not isinstance(source_key, str) or not re.fullmatch(r"[0-9a-f]{64}", source_key)):
            raise ValueError("Invalid page render source evidence")

    def box(value):
        return (isinstance(value, list) and len(value) == 4
                and all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in value)
                and value[0] <= value[2] and value[1] <= value[3])

    regions = document.get("uncertain_regions", [])
    if not isinstance(regions, list) or len(regions) > MAX_TOKENS:
        raise ValueError("Invalid region evidence")
    for region in regions:
        if (not isinstance(region, dict) or type(region.get("page")) is not int
                or not 1 <= region["page"] <= len(pages) or not box(region.get("box"))
                or not isinstance(region.get("issues"), list) or not region["issues"]
                or any(not isinstance(issue, str) for issue in region["issues"])):
            raise ValueError("Invalid unresolved region")
        values = region.get("candidate_values", [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise ValueError("Invalid region candidates")
        for side in ("left", "right"):
            index = region.get(f"{side}_token_index")
            if index is not None and (type(index) is not int or not 0 <= index < len(document["tokens"])):
                raise ValueError("Invalid region anchor")
        appearance = region.get("appearance_key")
        if appearance is not None and (not isinstance(appearance, str) or not re.fullmatch(r"[0-9a-f]{64}", appearance)):
            raise ValueError("Invalid vector appearance evidence")
        geometry = region.get("anchor_geometry")
        if geometry is not None and (not isinstance(geometry, dict) or set(geometry) != {"left", "right"}
                or any(not isinstance(values, list) or len(values) != 4
                       or any(not isinstance(v, str) or len(v) > 128 or not re.fullmatch(r"-?\d+(?:/[1-9]\d*)?", v) for v in values)
                       for values in geometry.values())):
            raise ValueError("Invalid relative vector geometry")

    for kind in ("words", "tokens", "controls"):
        for item in document[kind]:
            if (not isinstance(item, dict) or not isinstance(item.get("page"), int)
                    or not 1 <= item["page"] <= len(pages) or not box(item.get("box"))):
                raise ValueError("Invalid occurrence evidence")
            if kind != "controls":
                if not isinstance(item.get("text"), str) or not item["text"]:
                    raise ValueError("Invalid occurrence text")
                if not isinstance(item.get("issues", []), list) or any(not isinstance(issue, str) for issue in item.get("issues", [])):
                    raise ValueError("Invalid evidence limitations")
            if kind == "tokens":
                supported = item.get("text_identity_supported")
                if supported is not None and type(supported) is not bool:
                    raise ValueError("Invalid text identity evidence")
                opaque = item.get("opaque_key")
                if opaque is not None and (not isinstance(opaque, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", opaque) or item["text"] != "\ufffc"
                        or type(supported) is not bool or not item.get("issues")):
                    raise ValueError("Invalid opaque glyph evidence")
                glyphs = item.get("glyphs")
                if not isinstance(glyphs, list) or len(glyphs) != len(item["text"]) or not all(box(glyph) for glyph in glyphs):
                    raise ValueError("Incomplete glyph accounting")
                if not isinstance(item.get("word_start"), int) or not isinstance(item.get("word_end"), int) or item["word_end"] - item["word_start"] != len(item["text"]):
                    raise ValueError("Incomplete token accounting")
                metadata = item.get("char_meta", [])
                if metadata and (len(metadata) != len(glyphs) or not all(isinstance(part, dict) for part in metadata)):
                    raise ValueError("Incomplete structural evidence")


def _pdf_evidence(before, after):
    # All font/native inspection runs outside the process rendering review
    # images. Transfer exact snapshot bytes, never live paths or model objects.
    if not all(0 < len(data) <= MAX_BYTES for data in (before, after)):
        raise PDFComparisonError("Comparison requires two non-empty PDFs of at most 64 MiB each.")
    try:
        with tempfile.TemporaryFile() as diagnostics:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("pdf_comparison_worker.py"))],
                input=struct.pack(">QQ", len(before), len(after)) + before + after,
                stdout=subprocess.PIPE, stderr=diagnostics, timeout=95, check=True,
            )
        if len(result.stdout) > 64 * 1024 * 1024:
            raise ValueError("Evidence output too large")
        documents = json.loads(result.stdout)
        if documents == {"error": "password_protected"}:
            raise PDFComparisonError("Password-protected PDFs cannot be compared. No files were changed.")
        if not isinstance(documents, list) or len(documents) != 2:
            raise ValueError("Invalid evidence response")
        for document in documents:
            _validate_evidence(document)
        _validate_alignment(documents, identical_bytes=before == after)
        return documents
    except PDFComparisonError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        raise PDFComparisonError("PDF text evidence could not be checked safely. No complete comparison is available; review the PDFs manually.") from exc


def _validate_alignment(documents, *, identical_bytes):
    a, b = (document["tokens"] for document in documents)
    alignment = documents[0].get("alignment")
    if not isinstance(alignment, dict) or not isinstance(alignment.get("limited"), bool) or not isinstance(alignment.get("opcodes"), list):
        raise ValueError("Incomplete alignment evidence")
    previous_a = previous_b = 0
    for opcode in alignment["opcodes"]:
        if not isinstance(opcode, list) or len(opcode) != 5:
            raise ValueError("Invalid alignment range")
        op, a0, a1, b0, b1 = opcode
        if (op not in {"equal", "replace", "insert", "delete"}
                or any(type(value) is not int for value in (a0, a1, b0, b1))
                or a0 != previous_a or b0 != previous_b
                or not a0 <= a1 <= len(a) or not b0 <= b1 <= len(b)
                or (a0 == a1 and b0 == b1)):
            raise ValueError("Non-contiguous or invalid alignment")
        if op == "equal":
            if a1 - a0 != b1 - b0 or any(
                token_key(left) != token_key(right) or (not identical_bytes and (not identity_supported(left) or not identity_supported(right)))
                for left, right in zip(a[a0:a1], b[b0:b1])
            ):
                raise ValueError("Unproven equal-text alignment")
        elif (op == "insert" and a0 != a1) or (op == "delete" and b0 != b1):
            raise ValueError("Invalid change alignment")
        previous_a, previous_b = a1, b1
    if previous_a != len(a) or previous_b != len(b):
        raise ValueError("Incomplete occurrence accounting")


def _extract(data):
    """Inspect a standalone document through the same isolated evidence path."""
    return _pdf_evidence(data, data)[0]


def _locations(items):
    seen, result = set(), []
    for item in items:
        key = (item["page"], tuple(item["box"]))
        if key not in seen:
            result.append({"page": item["page"], "box": item["box"]})
            seen.add(key)
        if len(result) >= MAX_BOXES:
            break
    return result


def _snippet(items):
    words, previous, previous_end = [], None, None
    for item in items:
        key = _word_key(item)
        value = "[unresolved glyph]" if item.get("opaque_key") else item["text"]
        if key == previous:
            gap = previous_end is not None and item.get("word_start", previous_end) != previous_end
            words[-1] += (" ... " if gap else "") + value
        else:
            words.append(value)
        previous = key
        previous_end = item.get("word_end")
    text = " ".join(words)
    return text[:1200] + ("..." if len(text) > 1200 else "")


def _scanned_findings(before, after):
    findings = []
    for category, label, pattern in (
        ("reference", "New reference error", ERROR_PATTERN),
        ("placeholder", "Possible unresolved reference", PLACEHOLDER_PATTERN),
        ("character", "New replacement character", re.compile("\ufffd")),
    ):
        texts, offsets = [], []
        for document in (before, after):
            parts, starts, offset = [], [], 0
            for word in document["words"]:
                starts.append(offset)
                parts.append(word["text"])
                offset += len(word["text"]) + 1
            texts.append(" ".join(parts))
            offsets.append(starts)
        previous = Counter()
        for match in pattern.finditer(texts[0]):
            first = max(0, bisect_right(offsets[0], match.start()) - 1)
            last = bisect_right(offsets[0], match.end() - 1)
            if not any(item.get("issues") for item in before["words"][first:last]):
                previous[" ".join(match.group().lower().split())] += 1
        for match in pattern.finditer(texts[1]):
            key = " ".join(match.group().lower().split())
            if previous[key]:
                previous[key] -= 1
                continue
            first = max(0, bisect_right(offsets[1], match.start()) - 1)
            last = bisect_right(offsets[1], match.end() - 1)
            items = after["words"][first:last]
            if any(item.get("issues") for item in items):
                # Untrusted text cannot prove a new error pattern. Its tokens
                # are never aligned away and remain in the uncertainty findings.
                continue
            findings.append({
                "category": category, "label": label if before["words"] else {
                    "reference": "Reference error pattern", "placeholder": "Possible unresolved reference",
                    "character": "Replacement character pattern",
                }[category], "before_text": "",
                "after_text": match.group(), "before_locations": [],
                "after_locations": _locations(items),
                "before_page": None, "after_page": items[0]["page"] if items else 1,
                "before_pages": [], "after_pages": sorted({item["page"] for item in items}),
            })
    return findings


def _word_key(token):
    return token["page"], tuple(token.get("word_box", token["box"]))


def _unique_seeds(tokens):
    """Count seeds over the whole document, including already aligned text."""
    positions = {}
    for index in range(len(tokens) - 2):
        if any(not identity_supported(t) for t in tokens[index:index + 3]):
            continue
        key = tuple(token_key(item) for item in tokens[index:index + 3])
        positions[key] = -1 if key in positions else index
    return positions


def _pairing_boundaries(tokens):
    starts, ends = set(), set()
    unscoped, ambiguous = [0], [0]
    flow_keys = [
        (item["page"], item["raw_context"]) if item.get("raw_context") else None
        for item in tokens
    ]
    # A dangling label/assignment marker binds the next flow line to this one.
    for index, item in enumerate(tokens):
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if previous is None or _word_key(previous) != _word_key(item):
            if (previous is None or previous["text"] in {".", "!", "?"}
                    or (flow_keys[index] is not None and flow_keys[index] != flow_keys[index - 1]
                        and previous["text"] not in {":", "="})):
                starts.add(index)
        if following is None or _word_key(following) != _word_key(item):
            if (following is None or item["text"] in {".", "!", "?"}
                    or (flow_keys[index] is not None and flow_keys[index] != flow_keys[index + 1]
                        and item["text"] not in {":", "="})):
                ends.add(index + 1)
        context = item.get("semantic_context")
        ambiguous.append(ambiguous[-1] + bool(isinstance(context, dict) and context.get("ambiguous")))
        unscoped.append(unscoped[-1] + (not bool(item.get("context_key"))))
    return starts, ends, unscoped, ambiguous


def _sequence_pairs(a, b, changes):
    if not changes:
        return [], set(), set(), False
    owners_a, owners_b = [None] * len(a), [None] * len(b)
    for _, a0, a1, b0, b1 in changes:
        owners_a[a0:a1] = [(a0, a1)] * (a1 - a0)
        owners_b[b0:b1] = [(b0, b1)] * (b1 - b0)
    used_a, used_b, pairs = set(), set(), []
    seeds_a, seeds_b = _unique_seeds(a), _unique_seeds(b)
    starts_a, ends_a, unscoped_a, ambiguous_a = _pairing_boundaries(a)
    starts_b, ends_b, unscoped_b, ambiguous_b = _pairing_boundaries(b)
    steps = 0
    for key, i in seeds_a.items():
        j = seeds_b.get(key, -1)
        if i < 0 or j < 0 or i in used_a or j in used_b:
            continue
        left, right = owners_a[i], owners_b[j]
        if not left or not right or i + 3 > left[1] or j + 3 > right[1]:
            continue
        a0, b0, a1, b1 = i, j, i + 3, j + 3
        while a0 > left[0] and b0 > right[0] and a0 - 1 not in used_a and b0 - 1 not in used_b:
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            if not identity_supported(a[a0 - 1]) or not identity_supported(b[b0 - 1]) or token_key(a[a0 - 1]) != token_key(b[b0 - 1]):
                break
            a0, b0 = a0 - 1, b0 - 1
        while a1 < left[1] and b1 < right[1] and a1 not in used_a and b1 not in used_b:
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            if not identity_supported(a[a1]) or not identity_supported(b[b1]) or token_key(a[a1]) != token_key(b[b1]):
                break
            a1, b1 = a1 + 1, b1 + 1
        # Do not reclassify a matching prefix/suffix inside a changed PDF word.
        while a0 < a1 and (
            (a0 > 0 and _word_key(a[a0]) == _word_key(a[a0 - 1]))
            or (b0 > 0 and _word_key(b[b0]) == _word_key(b[b0 - 1]))
        ):
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            a0, b0 = a0 + 1, b0 + 1
        while a1 > a0 and (
            (a1 < len(a) and _word_key(a[a1]) == _word_key(a[a1 - 1]))
            or (b1 < len(b) and _word_key(b[b1]) == _word_key(b[b1 - 1]))
        ):
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            a1, b1 = a1 - 1, b1 - 1
        if ambiguous_a[a1] != ambiguous_a[a0] or ambiguous_b[b1] != ambiguous_b[b0]:
            continue
        if unscoped_a[a1] != unscoped_a[a0] or unscoped_b[b1] != unscoped_b[b0]:
            # Exact words alone cannot prove that a fragment kept its meaning
            # under a different label. Keep only complete flow/sentence units.
            while a0 < a1 and (a0 not in starts_a or b0 not in starts_b):
                steps += 1
                if steps > MAX_PAIRING_STEPS:
                    return pairs, used_a, used_b, True
                a0, b0 = a0 + 1, b0 + 1
            while a1 > a0 and (a1 not in ends_a or b1 not in ends_b):
                steps += 1
                if steps > MAX_PAIRING_STEPS:
                    return pairs, used_a, used_b, True
                a1, b1 = a1 - 1, b1 - 1
        if a1 - a0 < 3 or not (a0 <= i and i + 3 <= a1):
            continue
        if len({_word_key(item) for item in a[a0:a1] if any(c.isalpha() for c in item["text"])}) < 2:
            continue
        if any(index in used_a for index in range(a0, a1)) or any(index in used_b for index in range(b0, b1)):
            continue
        pairs.append((a0, a1, b0, b1))
        used_a.update(range(a0, a1))
        used_b.update(range(b0, b1))
    return pairs, used_a, used_b, False


def _remaining_ranges(start, end, used):
    ranges = []
    for index in range(start, end):
        if index in used:
            continue
        if ranges and ranges[-1][1] == index:
            ranges[-1] = (ranges[-1][0], index + 1)
        else:
            ranges.append((index, index + 1))
    return ranges


def _text_finding(a, b, a0, a1, b0, b1):
    left, right = a[a0:a1], b[b0:b1]
    issues = sorted({issue for item in left + right for issue in item.get("issues", [])})
    display_left, display_right = left, right
    # Refine a single changed token to the actual differing glyphs. Coverage
    # continues to refer to the complete source tokens, not this presentation.
    if (not issues and len(left) == len(right) == 1
            and left[0]["text"] != right[0]["text"]
            and len(left[0]["text"]) * len(right[0]["text"]) <= MAX_PAIRING_STEPS
            and not (left[0]["text"].isnumeric() or right[0]["text"].isnumeric())):
        display_left, display_right = [], []
        for op, i, j, k, l in SequenceMatcher(None, left[0]["text"], right[0]["text"], autojunk=False).get_opcodes():
            if op == "equal":
                continue
            for source, start, end, target in ((left[0], i, j, display_left), (right[0], k, l, display_right)):
                if start != end:
                    glyphs = source["glyphs"][start:end]
                    target.append({**source, "text": source["text"][start:end], "box": _union(glyphs),
                                   "word_start": source["word_start"] + start, "word_end": source["word_start"] + end})
    pages_a, pages_b = sorted({item["page"] for item in left}), sorted({item["page"] for item in right})
    return {
        "category": "uncertain" if any(needs_identity_review(item) for item in left + right) else "text",
        "kind": "changed" if left and right else "removed" if left else "added",
        "label": "Check text / formula" if any(needs_identity_review(item) for item in left + right) else "Changed text" if left and right else "Removed text" if left else "Added text",
        "issues": issues,
        "fonts": sorted({font for item in left + right for font in item.get("fonts", [])}),
        "before_raw": "\n".join(dict.fromkeys(t.get("raw_context", t["text"]) for t in left))[:2400],
        "after_raw": "\n".join(dict.fromkeys(t.get("raw_context", t["text"]) for t in right))[:2400],
        "before_text": _snippet(display_left), "after_text": _snippet(display_right),
        "before_context": _snippet(a[max(0, a0 - 12):min(len(a), a1 + 12)]),
        "after_context": _snippet(b[max(0, b0 - 12):min(len(b), b1 + 12)]),
        "before_locations": _locations(display_left), "after_locations": _locations(display_right),
        "before_pages": pages_a, "after_pages": pages_b,
        "before_page": pages_a[0] if pages_a else None,
        "after_page": pages_b[0] if pages_b else None,
        "before_token_range": [a0, a1], "after_token_range": [b0, b1],
        "locations_truncated": any(
            len({(item["page"], tuple(item["box"])) for item in items}) > MAX_BOXES
            for items in (display_left, display_right)
        ),
    }


def _page_maps(a, b, opcodes):
    votes = defaultdict(Counter)
    for operation, a0, a1, b0, b1 in opcodes:
        if operation == "equal":
            for left, right in zip(a[a0:a1], b[b0:b1]):
                votes[left["page"]][right["page"]] += 1
    page_map = {}
    inverse_map = {}
    before_page_sizes = Counter(item["page"] for item in a)
    after_page_sizes = Counter(item["page"] for item in b)
    for number, counts in votes.items():
        candidate, count = counts.most_common(1)[0]
        # Avoid inventing a page correspondence from a page number or a lone header.
        if (
            count >= 12 and count / sum(counts.values()) >= 0.6
            and count / before_page_sizes[number] >= 0.35
            and count / after_page_sizes[candidate] >= 0.35
        ):
            page_map[str(number)] = candidate
    for target_page in sorted(after_page_sizes):
        candidates = [(votes[int(left)][target_page], int(left)) for left in page_map if page_map[left] == target_page]
        if candidates:
            inverse_map[str(target_page)] = max(candidates)[1]
    return page_map, inverse_map


def _region_findings(before, after):
    # Uninterpreted vector glyphs are evidence limitations, not invented text.
    # Keep their locations visible without consuming neighboring text changes.
    for side, document in (("before", before), ("after", after)):
        for region in document.get("uncertain_regions", []):
            if region.get("_counterpart") is not None or region.get("_matched_region") or region.get("_reviewed_with_before"):
                continue
            finding = {
                "category": "uncertain", "kind": "unresolved", "label": "Check vector / formula",
                "issues": region["issues"], "fonts": [], "locations_truncated": False,
                "before_text": "", "after_text": "", "before_raw": "", "after_raw": "",
                "before_context": "", "after_context": "",
                "before_locations": [], "after_locations": [],
                "before_pages": [], "after_pages": [], "before_page": None, "after_page": None,
            }
            finding[f"{side}_text"] = "Vector glyph: logical text unresolved"
            finding[f"{side}_locations"] = _locations([region])
            finding[f"{side}_pages"] = [region["page"]]
            finding[f"{side}_page"] = region["page"]
            counterpart = region.get("_review_counterpart")
            if counterpart is not None:
                finding["after_text"] = "Vector glyph: logical text unresolved"
                finding["after_locations"] = _locations([counterpart])
                finding["after_pages"] = [counterpart["page"]]
                finding["after_page"] = counterpart["page"]
                finding["issues"] = sorted(set([*region["issues"], *counterpart["issues"],
                    "Neighboring text locates a possible counterpart; vector equality or attachment is not established."]))
            yield finding


def _vector_correspondence(before, after, opcodes):
    """Pair proved occurrences and locate unresolved candidate counterparts.

    Exact neighboring text can locate an unmatched character opposite an
    uninterpreted vector. Keep both visible as uncertain, not a proven edit.
    Ambiguous or many-to-one proposals remain separate findings.
    """
    maps = ({}, {})
    for op, a0, a1, b0, b1 in opcodes:
        if op == "equal":
            for a, b in zip(range(a0, a1), range(b0, b1)):
                maps[0][a], maps[1][b] = b, a
    # An exact, bounded page/resource closure can establish the same drawing
    # occurrence without text anchors. It never supplies guessed Unicode or
    # waives a text finding. Duplicate pages/regions remain ambiguous.
    page_sources = [Counter(page.get("render_source_key") for page in doc["pages"])
                    for doc in (before, after)]
    source_regions = (defaultdict(list), defaultdict(list))
    for side, document in enumerate((before, after)):
        for region in document.get("uncertain_regions", []):
            page = document["pages"][region["page"] - 1]
            source = page.get("render_source_key")
            appearance = region.get("appearance_key")
            if source and appearance and all(counts[source] == 1 for counts in page_sources):
                key = source, appearance, page["width"], page["height"], tuple(region["box"])
                source_regions[side][key].append(region)
    source_matches = 0
    for key in source_regions[0].keys() & source_regions[1].keys():
        if len(source_regions[0][key]) == len(source_regions[1][key]) == 1:
            source_regions[0][key][0]["_matched_region"] = source_regions[1][key][0]["_matched_region"] = True
            source_matches += 1
    # Equal path appearance alone is insufficient: both text anchors and the
    # exact relative geometry must agree, so an exponent cannot become a base.
    def vector_key(region, mapping=None):
        left, right = (region.get(f"{side}_token_index") for side in ("left", "right"))
        if left is None or right is None or not region.get("appearance_key") or not region.get("anchor_geometry"):
            return None
        if mapping is not None:
            if left not in mapping or right not in mapping:
                return None
            left, right = mapping[left], mapping[right]
        return region["appearance_key"], left, right, json.dumps(region["anchor_geometry"], sort_keys=True)

    grouped = (defaultdict(list), defaultdict(list))
    for side, document in enumerate((before, after)):
        for region in document.get("uncertain_regions", []):
            if region.get("_matched_region"):
                continue
            key = vector_key(region, maps[0] if side == 0 else None)
            if key is not None:
                grouped[side][key].append(region)
    matched = 0
    for key in grouped[0].keys() & grouped[1].keys():
        if len(grouped[0][key]) == len(grouped[1][key]) == 1:
            grouped[0][key][0]["_matched_region"] = grouped[1][key][0]["_matched_region"] = True
            matched += 1
    for document in (before, after):
        summary = document.setdefault("evidence_summary", {})
        summary["exact_vector_region_pairs"] = matched + source_matches
        summary["exact_render_source_region_pairs"] = source_matches
    links = ({}, {})
    for source_side, (source, target) in enumerate(((before, after), (after, before))):
        proposed = defaultdict(list)
        for region in source.get("uncertain_regions", []):
            if region.get("_matched_region"):
                continue
            left, right = (region.get(f"{side}_token_index") for side in ("left", "right"))
            if left is None and right is None:
                continue
            if any(index is not None and index not in maps[source_side] for index in (left, right)):
                continue
            mapped_left, mapped_right = (maps[source_side].get(index) for index in (left, right))
            start = mapped_left + 1 if mapped_left is not None else mapped_right - 1
            end = mapped_right if mapped_right is not None else mapped_left + 2
            if end - start != 1 or not 0 <= start < len(target["tokens"]):
                continue
            token = target["tokens"][start]
            if start in maps[1 - source_side] or token["text"] not in region.get("candidate_values", []):
                continue
            proposed[start].append(region)
        for index, regions in proposed.items():
            if len(regions) != 1:
                continue
            region = regions[0]
            token = target["tokens"][index]
            token["issues"] = sorted(set([*token.get("issues", []),
                "Corresponding baseline/current vector glyph is unresolved; character equality is not established.",
                *region["issues"]]))
            token["text_identity_supported"] = False
            region["_counterpart"] = index
            links[1 - source_side][index] = region
    # Present an uncertain pair together when its exact text anchors agree.
    # Unlike the strict appearance/geometry proof above, this leaves a warning
    # and both sets of marks. It does not consume or normalize any text token.
    uncertain = (defaultdict(list), defaultdict(list))
    for side, document in enumerate((before, after)):
        for region in document.get("uncertain_regions", []):
            if region.get("_matched_region") or region.get("_counterpart") is not None:
                continue
            left, right = (region.get(f"{name}_token_index") for name in ("left", "right"))
            if left is None or right is None:
                continue
            if side == 0:
                if left not in maps[0] or right not in maps[0]:
                    continue
                left, right = maps[0][left], maps[0][right]
            uncertain[side][(left, right)].append(region)
    for key in uncertain[0].keys() & uncertain[1].keys():
        if len(uncertain[0][key]) == len(uncertain[1][key]) == 1:
            uncertain[0][key][0]["_review_counterpart"] = uncertain[1][key][0]
            uncertain[1][key][0]["_reviewed_with_before"] = True
    return links


def compare_pdf_bytes(before_data, after_data):
    before, after = _pdf_evidence(before_data, after_data)
    a, b = before["tokens"], after["tokens"]
    patterns = _scanned_findings(before, after)
    findings = patterns[:MAX_FINDINGS]
    total_counts = Counter(item["category"] for item in patterns)
    total_findings = len(patterns)

    opcodes = before["alignment"]["opcodes"]
    vector_links = _vector_correspondence(before, after, opcodes) if before_data != after_data else ({}, {})

    def add_finding(a0, a1, b0, b1):
        nonlocal total_findings
        finding = _text_finding(a, b, a0, a1, b0, b1)
        for side, start, end, links in (("after", a0, a1, vector_links[0]), ("before", b0, b1, vector_links[1])):
            regions = [links[index] for index in range(start, end) if index in links]
            if regions:
                locations = finding[f"{side}_locations"] + _locations(regions)
                finding[f"{side}_locations"] = locations[:MAX_BOXES]
                finding[f"{side}_pages"] = sorted(set(finding[f"{side}_pages"] + [r["page"] for r in regions]))
                finding[f"{side}_page"] = finding[f"{side}_pages"][0]
                finding[f"{side}_text"] = finding[f"{side}_text"] or "Vector glyph: logical text unresolved"
                finding["locations_truncated"] |= len(locations) > MAX_BOXES
        total_findings += 1
        total_counts[finding["category"]] += 1
        if len(findings) < MAX_FINDINGS:
            findings.append(finding)

    alignment_limited = before["alignment"]["limited"]
    changes = [row for row in opcodes if row[0] != "equal"]
    pairs, used_a, used_b, pairing_limited = _sequence_pairs(a, b, changes)
    pairing_limited = pairing_limited or alignment_limited
    remaining_a = remaining_b = 0
    for _, a0, a1, b0, b1 in changes:
        ranges_a = [part for start, end in _remaining_ranges(a0, a1, used_a)
                    for part in split_confidence_ranges(a, start, end)]
        ranges_b = [part for start, end in _remaining_ranges(b0, b1, used_b)
                    for part in split_confidence_ranges(b, start, end)]
        remaining_a += sum(end - start for start, end in ranges_a)
        remaining_b += sum(end - start for start, end in ranges_b)
        if len(ranges_a) == len(ranges_b) == 1:
            add_finding(*ranges_a[0], *ranges_b[0])
        else:
            for start, end in ranges_a:
                add_finding(start, end, b0, b0)
            for start, end in ranges_b:
                add_finding(a0, a0, start, end)
    # Exact one-to-one moved text is accounted for, but is not a content finding.
    equal = sum(a1 - a0 for operation, a0, a1, _, _ in opcodes if operation == "equal")
    if equal + len(used_a) + remaining_a != len(a) or equal + len(used_b) + remaining_b != len(b):
        raise PDFComparisonError("Text accounting failed. No incomplete comparison was presented as complete.")
    matches = [(a0, a1, b0, b1) for op, a0, a1, b0, b1 in opcodes if op == "equal"] + pairs
    for i, j, indices in structure_changes(a, b, matches):
        finding = _text_finding(a, b, i, i + 1, j, j + 1)
        finding.update({
            "category": "structure", "label": "Changed text structure", "kind": "changed",
            "before_text": _snippet([a[i]]), "after_text": _snippet([b[j]]),
            "before_locations": _locations({"page": a[i]["page"], "box": a[i]["glyphs"][k]} for k in indices),
            "after_locations": _locations({"page": b[j]["page"], "box": b[j]["glyphs"][k]} for k in indices),
            "locations_truncated": any(len({tuple(token["glyphs"][k]) for k in indices}) > MAX_BOXES for token in (a[i], b[j])),
            "issues": ["Matching characters have different supported subscript/superscript roles. Check the marked expression."],
        })
        total_findings += 1
        total_counts["structure"] += 1
        if len(findings) < MAX_FINDINGS:
            findings.append(finding)
    page_map, inverse_map = _page_maps(a, b, [("equal", *row) for row in matches])
    if before_data != after_data:
        for finding in _region_findings(before, after):
            total_findings += 1
            total_counts["uncertain"] += 1
            if len(findings) < MAX_FINDINGS:
                findings.append(finding)
    if before_data != after_data and (before["controls"] or after["controls"]):
        total_findings += 1
        total_counts["formatting"] += 1
        if len(findings) < MAX_FINDINGS:
            findings.append({
                "category": "formatting", "label": "Invisible formatting characters", "kind": "formatting",
                "before_text": f'{len(before["controls"])} non-drawing U+200B character(s)',
                "after_text": f'{len(after["controls"])} non-drawing U+200B character(s)',
                "before_locations": _locations(before["controls"]), "after_locations": _locations(after["controls"]),
                "before_pages": sorted({t["page"] for t in before["controls"]}),
                "after_pages": sorted({t["page"] for t in after["controls"]}),
                "before_page": before["controls"][0]["page"] if before["controls"] else None,
                "after_page": after["controls"][0]["page"] if after["controls"] else None,
                "before_raw": "\n".join(dict.fromkeys(t["context"] for t in before["controls"]))[:2400],
                "after_raw": "\n".join(dict.fromkeys(t["context"] for t in after["controls"]))[:2400],
                "issues": ["Only U+200B glyphs with empty ink bounds and no detected font-mapping issue were normalized. Visible characters and ordinary spaces were retained."],
                "fonts": [],
                "locations_truncated": len(before["controls"]) > MAX_BOXES or len(after["controls"]) > MAX_BOXES,
            })
    for index, finding in enumerate(findings):
        finding["id"] = index
        finding["before_reference_page"] = inverse_map.get(str(finding["after_page"])) if not finding["before_pages"] else None
        finding["after_reference_page"] = page_map.get(str(finding["before_page"])) if not finding["after_pages"] else None
    coverage_reasons = sorted(set(before.get("coverage_reasons", []) + after.get("coverage_reasons", [])
        + [issue for token in a + b for issue in token.get("issues", [])]
        + [issue for document in (before, after) for region in document.get("uncertain_regions", []) for issue in region["issues"]]))
    if total_counts["uncertain"]:
        coverage_reasons.append("Some text or formula evidence remains unresolved; those locations are listed for verification.")
    if pairing_limited:
        coverage_reasons.append("Exact-text alignment reached its work limit; unmatched content has not been discarded.")
    if not a or not b:
        coverage_reasons.append("One or both documents have no comparable text; inspect the full PDFs.")
    if total_findings > MAX_FINDINGS or any(row.get("locations_truncated") for row in findings):
        coverage_reasons.append("The display limit was reached; not all findings or locations are shown.")
    return {
        "findings": findings, "counts": dict(Counter(item["category"] for item in findings)),
        "before_pages": before["pages"], "after_pages": after["pages"],
        "page_map": page_map, "inverse_map": inverse_map,
        "text_available": bool(a and b), "truncated": total_findings > MAX_FINDINGS,
        "pairing_limited": pairing_limited,
        "total_findings": total_findings,
        "shown_findings": len(findings),
        "total_counts": dict(total_counts),
        "total_attention_count": total_findings - total_counts["formatting"],
        "attention_count": sum(item["category"] != "formatting" for item in findings),
        "uncertain_count": total_counts["uncertain"],
        "content_count": total_counts["text"] + total_counts["reference"] + total_counts["placeholder"] + total_counts["character"] + total_counts["structure"],
        "info_count": total_counts["formatting"],
        "sequence_count": 0,
        "normalized_controls": {"before": len(before["controls"]), "after": len(after["controls"])},
        "token_coverage": {"comparable": bool(a and b), "aligned": equal, "paired": len(used_a), "unmatched_before": remaining_a, "unmatched_after": remaining_b, "before_total": len(a), "after_total": len(b)},
        "identical_bytes": before_data == after_data,
        "coverage_status": "limited" if coverage_reasons else "complete",
        "coverage_reasons": coverage_reasons,
        "evidence_summary": {"before": before.get("evidence_summary", {}), "after": after.get("evidence_summary", {})},
    }


def create_pdf_comparison(submission_id, baseline="original", *, request=None, expected_upload=None):
    token = uuid.uuid4().hex
    directory = None
    submission = None
    try:
        submission = comparison_submission(submission_id)
        identity = comparison_identity(submission)
        if expected_upload is not None:
            if baseline != "original":
                raise PDFComparisonError("Automatic upload comparison must use the Original PDF.")
            validate_upload_binding(submission, expected_upload)
        target_info = publication_pdf_info(submission, include_url=False)
        after = _live_bytes(target_info)
        if expected_upload is not None:
            validate_upload_binding(submission, expected_upload, pdf_sha256=hashlib.sha256(after).hexdigest())
        before, baseline_info = _read_baseline(submission, baseline)
        result = compare_pdf_bytes(before, after)
        current = comparison_submission(submission_id)
        current_info = publication_pdf_info(current, include_url=False)
        if comparison_identity(current) != identity or hashlib.sha256(_live_bytes(current_info)).digest() != hashlib.sha256(after).digest():
            raise PDFComparisonError("The publication PDF changed during comparison. Reload the current files and compare again.")
        new_before, _ = _read_baseline(current, baseline)
        if hashlib.sha256(new_before).digest() != hashlib.sha256(before).digest():
            raise PDFComparisonError("The baseline changed during comparison. Compare again.")
        if expected_upload is not None:
            validate_upload_binding(current, expected_upload, pdf_sha256=hashlib.sha256(after).hexdigest())
        root = comparison_root(create=True)
        purge_expired_preview_directories(root, TTL)
        candidate_directory = root / token
        candidate_directory.mkdir(exist_ok=False)
        directory = candidate_directory
        payload = {
            "version": COMPARISON_VERSION, "token": token, "created_at": timezone.now().isoformat(),
            "identity": identity, "baseline": baseline_info,
            "target": {"label": target_info["label"], "filename": target_info["filename"]},
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(), "result": result,
        }
        if expected_upload is not None:
            payload["upload_binding"] = expected_upload
        # A killed writer still leaves a dated staging folder that can expire.
        (directory / "payload.json").write_text(json.dumps({"created_at": payload["created_at"]}), encoding="utf-8")
        (directory / "before.pdf").write_bytes(before)
        (directory / "after.pdf").write_bytes(after)
        signed_payload = json.dumps({
            "created_at": payload["created_at"], "signed": signing.dumps(payload, salt=SALT, compress=True),
        })
        if len(signed_payload.encode("utf-8")) > 8 * 1024 * 1024:
            raise PDFComparisonError("The comparison result exceeds the safe snapshot limit. No complete comparison is available.")
        (directory / "payload.json").write_text(signed_payload, encoding="utf-8")
        audit_success(
            "formatting_pdf_compare", "Read-only PDF comparison created; publication files and review state unchanged.",
            request=request, submission=submission,
            file_hashes={"before": payload["before_sha256"], "after": payload["after_sha256"]},
            result_counts={"findings": result["total_findings"], "content": result["content_count"], "uncertain": result["uncertain_count"], "limited": result["coverage_status"] == "limited"},
            extra={"baseline": baseline_info, "publication_pdf_source": target_info["source"],
                   "comparison_method": COMPARISON_VERSION, "coverage_status": result["coverage_status"],
                   **({"automatic": True, "revision_id": expected_upload["revision_id"]} if expected_upload else {})},
        )
        return payload
    except Exception as exc:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        audit_failure("formatting_pdf_compare", exc, "PDF comparison could not be created; no publication or review data changed.", request=request, submission=submission)
        if isinstance(exc, PDFComparisonError):
            raise
        if isinstance(exc, (OSError, ValueError, RuntimeError)):
            raise PDFComparisonError(str(exc)) from exc
        raise


def load_pdf_comparison(submission_id, token, *, validate_current=True):
    if not isinstance(token, str) or not PREVIEW_TOKEN_PATTERN.fullmatch(token):
        raise PDFComparisonError("Invalid PDF comparison token.")
    root = comparison_root()
    directory = root / token
    if directory.is_symlink():
        raise PDFComparisonError("Invalid PDF comparison folder.")
    try:
        path = directory / "payload.json"
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise PDFComparisonError("Invalid PDF comparison payload.")
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        payload = signing.loads(wrapper["signed"], salt=SALT, max_age=int(TTL.total_seconds()))
    except (OSError, KeyError, ValueError, signing.BadSignature) as exc:
        raise PDFComparisonError("Comparison expired or unavailable. Build a new comparison.") from exc
    if payload.get("version") != COMPARISON_VERSION:
        raise PDFComparisonError("This comparison uses an older comparison method. Build a new comparison; the saved PDFs are unchanged.")
    if payload["token"] != token or payload["identity"]["submission_id"] != submission_id:
        raise PDFComparisonError("This comparison does not belong to the selected submission.")
    if validate_current:
        current = comparison_submission(submission_id)
        info = publication_pdf_info(current, include_url=False)
        if comparison_identity(current) != payload["identity"] or hashlib.sha256(_live_bytes(info)).hexdigest() != payload["after_sha256"]:
            raise PDFComparisonError("The publication PDF changed after this comparison. Build a new comparison of the current files.")
        data, _ = _read_baseline(current, payload["baseline"]["key"])
        if hashlib.sha256(data).hexdigest() != payload["before_sha256"]:
            raise PDFComparisonError("The baseline changed after this comparison. Build a new comparison.")
        if payload.get("upload_binding"):
            validate_upload_binding(current, payload["upload_binding"], pdf_sha256=payload["after_sha256"])
    return payload


def comparison_pdf_bytes(submission_id, token, side):
    if side not in {"before", "after"}:
        raise PDFComparisonError("Unknown comparison side.")
    payload = load_pdf_comparison(submission_id, token)
    path = comparison_root() / token / f"{side}.pdf"
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise PDFComparisonError("Invalid comparison snapshot.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != payload[f"{side}_sha256"]:
        raise PDFComparisonError("The comparison snapshot failed its integrity check. Build a new comparison.")
    return data


def comparison_page_png(submission_id, token, side, page_number):
    data = comparison_pdf_bytes(submission_id, token, side)
    with fitz.open(stream=data, filetype="pdf") as document:
        if not 1 <= page_number <= len(document):
            raise PDFComparisonError("This page does not exist in the compared PDF.")
        page = document[page_number - 1]
        scale = min(1400 / page.rect.width, 2000 / page.rect.height, 2)
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
