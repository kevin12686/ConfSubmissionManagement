"""OCR lexical regions and whitespace-based reading order."""
import re
import unicodedata

TOKEN = re.compile(r'[^\W\d_]+|\d+|_|[^\w\s]', re.UNICODE)


def lex(text):
    return [m.group() for m in TOKEN.finditer(unicodedata.normalize('NFC', text))]

def xy_order(lines, width, depth=0):
    """Whitespace-only recursive reading order, shared across all inputs."""
    if len(lines) < 2 or depth > 80:
        return sorted(lines, key=lambda l: (l['box'][1], l['box'][0]))
    def gaps(axis):
        intervals = sorted((l['box'][axis], l['box'][axis+2]) for l in lines)
        edge, found = intervals[0][1], []
        for lo, hi in intervals[1:]:
            if lo > edge:
                found.append((lo-edge, (edge+lo)/2))
            edge = max(edge, hi)
        return found
    x = gaps(0)
    x = [g for g in x if g[0] >= width * .015]
    y = gaps(1)
    if x:
        axis, split = 0, max(x)[1]
    elif y:
        axis, split = 1, max(y)[1]
    else:
        return sorted(lines, key=lambda l: (l['box'][1], l['box'][0]))
    first = [l for l in lines if (l['box'][axis]+l['box'][axis+2])/2 < split]
    second = [l for l in lines if l not in first]
    if not first or not second:
        return sorted(lines, key=lambda l: (l['box'][1], l['box'][0]))
    return xy_order(first, width, depth+1) + xy_order(second, width, depth+1)


def line_tokens(line, page, line_number):
    text = unicodedata.normalize('NFC', line['text'])
    regions, cursor = [], 0
    for word in line.get('words', []):
        word_text = unicodedata.normalize('NFC', word['text'])
        pos = text.find(word_text, cursor)
        if pos < 0:
            continue
        regions.append((pos, pos+len(word_text), word['box']))
        cursor = pos+len(word_text)
    tokens = []
    for m in TOKEN.finditer(text):
        boxes = [box for start, end, box in regions if start < m.end() and end > m.start()]
        box = ([min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else line['box'])
        tokens.append({'text': m.group(), 'page': page, 'line': line_number,
                       'text_start': m.start(), 'text_end': m.end(),
                       'box': box, 'precision': 'word' if boxes else 'line', 'score': line['score'], 'raw_text': line['text'], 'raw_box': line['box']})
    return tokens
