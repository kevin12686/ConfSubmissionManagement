"""Bounded lexical hyphen pairs; never a global dash-to-empty substitution."""
from collections import defaultdict
import unicodedata

HYPHENS = frozenset('-\u2010\u2011')
MATH = frozenset('=+*/^_<>\u2212\u2013\u2014\u2264\u2265\u00d7\u00f7')
BRACKETS = frozenset('()[]{}')
MAX_WORD_CHARACTERS = 1600


def _letters(value):
    return bool(value) and all(unicodedata.name(c, '').startswith('LATIN ') for c in value)


def _touching(left, right):
    return (left.get('uid') == right.get('uid')
            and isinstance(left.get('text_end'), int)
            and left['text_end'] == right.get('text_start'))


def _joined(left, dash, right):
    if not _touching(left, dash):
        return False
    return _touching(dash, right) or (
        dash.get('at_line_end') and right.get('at_line_start')
        and dash.get('uid') != right.get('uid')
        and 0 <= right['page'] - dash['page'] <= 1
    )


def _boundary(tokens, start, end):
    # Do not take the alphabetic substring of an identifier, numeric range or
    # longer ambiguous chain. Explicit nearby math syntax stays a warning.
    for index, edge in ((start-1, tokens[start]), (end, tokens[end-1])):
        if not 0 <= index < len(tokens):
            continue
        neighbour = tokens[index]
        touching = _touching(neighbour, edge) if index < start else _touching(edge, neighbour)
        if touching and (neighbour['text'].isalnum() or neighbour['text'] == '_' or neighbour['text'] in HYPHENS):
            return False
        direction = -1 if index < start else 1
        while 0 <= index < len(tokens) and tokens[index]['text'] in BRACKETS:
            index += direction
        if 0 <= index < len(tokens) and tokens[index]['text'] in MATH:
            return False
    return True


def word_spans(tokens):
    """Maximal tight compounds, including explicit end-of-line hyphenation.

    At least two Latin letters per part excludes short variable subtraction.
    OCR offsets establish tight joining; native text never supplies spelling.
    """
    spans = []
    i = 0
    while i < len(tokens):
        end = i+1
        parts = [tokens[i]['text']]
        if _letters(parts[0]):
            while (end+1 < len(tokens) and tokens[end]['text'] in HYPHENS
                   and _letters(tokens[end+1]['text'])
                   and _joined(tokens[end-1], tokens[end], tokens[end+1])):
                parts.append(tokens[end+1]['text'])
                end += 2
            if (end > i+1 and all(len(part) >= 2 for part in parts)
                    and sum(map(len, parts)) <= MAX_WORD_CHARACTERS
                    and len({t.get('owner') for t in tokens[i:end]}) == 1
                    and _boundary(tokens, i, end)):
                spans.append((i, end, ''.join(parts)))
        i = end
    return spans


def interiors(tokens):
    return {i: (start, end) for start, end, _ in word_spans(tokens) for i in range(start+1, end)}


def text_equivalent(before, after):
    """Validate the literal displayed word pair, not its untrusted kind label."""
    def word(text):
        for dash in HYPHENS:
            text = text.replace(dash, '-')
        parts = [''.join(part.split()) for part in text.split('-')]
        if not all(_letters(part) and len(part) >= 2 for part in parts):
            return None
        value = ''.join(parts)
        return value if len(value) <= MAX_WORD_CHARACTERS else None
    return (before != after and any(c in HYPHENS for c in before+after)
            and word(before) is not None and word(before) == word(after))


def _counterparts(tokens, word, compounds, lower, upper):
    by_start = {start: (end, key) for start, end, key in compounds}
    inside = {i for start, end, _ in compounds for i in range(start+1, end)}
    found = []
    for start in range(lower, upper):
        if start in inside:
            continue
        end, joined, owner = start, '', tokens[start].get('owner')
        while end < upper and len(joined) < len(word):
            stop, part = by_start.get(end, (end+1, tokens[end]['text']))
            if (stop > upper or not _letters(part) or len(part) < 2
                    or any(t.get('owner') != owner for t in tokens[end:stop])):
                break
            joined += part
            end = stop
            if not word.startswith(joined):
                break
            if joined == word and _boundary(tokens, start, end):
                found.append((start, end))
    return found


def alignment_pairs(a, b, *, spans_a=None, spans_b=None, range_a=None, range_b=None):
    """Whole-word, occurrence-preserving transitions for the local edit DP."""
    aa = word_spans(a) if spans_a is None else spans_a
    bb = word_spans(b) if spans_b is None else spans_b
    low_a, high_a = range_a or (0, len(a))
    low_b, high_b = range_b or (0, len(b))
    matches = set()
    for source, target, spans, other, low, high, target_low, target_high, reverse in (
        (a, b, aa, bb, low_a, high_a, low_b, high_b, False),
        (b, a, bb, aa, low_b, high_b, low_a, high_a, True),
    ):
        cache = {}
        for start, end, word in spans:
            if start < low or end > high:
                continue
            if word not in cache:
                cache[word] = _counterparts(target, word, other, target_low, target_high)
            for left, right in cache[word]:
                before, after = source[start:end], target[left:right]
                if (len({t.get('owner') for t in before+after}) != 1
                        or [t['text'] for t in before] == [t['text'] for t in after]):
                    continue
                match = (left, right, start, end) if reverse else (start, end, left, right)
                matches.add(match)
    ends = defaultdict(list)
    for start, end, left, right in sorted(matches):
        ends[end-low_a, right-low_b].append((start-low_a, left-low_b))
    return ends
