"""Read-only font coverage evidence, using pypdf's structured stream parser.

No character is guessed or repaired. Unsupported encodings remain uncertain.
"""

import io
import re

from pypdf import PdfReader
from pypdf.generic import ByteStringObject, ContentStream, TextStringObject


MAX_ENTRIES = 100000
STANDARD_FONTS = {
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic", "Symbol", "ZapfDingbats",
}
STANDARD_ENCODINGS = {"/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding"}


def font_name(value):
    return re.sub(r"^[A-Z]{6}\+", "", str(value).lstrip("/")).replace(" ", "")


def raw_bytes(value):
    if isinstance(value, ByteStringObject):
        return bytes(value)
    if isinstance(value, TextStringObject):
        return value.original_bytes
    raise ValueError("Invalid PDF character string")


def unicode_map(stream, reader):
    """Interpret bounded bfchar/bfrange records, never regular-expression CMaps."""
    mapping, spaces = {}, []
    for values, op in ContentStream(stream, reader).operations:
        if op == b"usecmap":
            raise ValueError("Inherited character map requires manual review")
        if op == b"endcodespacerange":
            if len(values) % 2:
                raise ValueError("Invalid code space")
            spaces.extend((raw_bytes(a), raw_bytes(b)) for a, b in zip(values[::2], values[1::2]))
        elif op == b"endbfchar":
            if len(values) % 2:
                raise ValueError("Invalid character mapping")
            for source, target in zip(values[::2], values[1::2]):
                key, value = raw_bytes(source), raw_bytes(target).decode("utf-16-be")
                if key in mapping and mapping[key] != value:
                    raise ValueError("Conflicting character mapping")
                mapping[key] = value
        elif op == b"endbfrange":
            if len(values) % 3:
                raise ValueError("Invalid character range")
            for start, end, target in zip(values[::3], values[1::3], values[2::3]):
                start, end = raw_bytes(start), raw_bytes(end)
                first, last = int.from_bytes(start, "big"), int.from_bytes(end, "big")
                if len(start) != len(end) or last < first or last - first + len(mapping) >= MAX_ENTRIES:
                    raise ValueError("Character map exceeds safe bounds")
                targets = target if isinstance(target, list) else None
                base = None if targets is not None else raw_bytes(target)
                if targets is not None and len(targets) != last - first + 1:
                    raise ValueError("Incomplete character range")
                for offset, code in enumerate(range(first, last + 1)):
                    value = raw_bytes(targets[offset]) if targets is not None else (int.from_bytes(base, "big") + offset).to_bytes(len(base), "big")
                    key = code.to_bytes(len(start), "big")
                    decoded = value.decode("utf-16-be")
                    if key in mapping and mapping[key] != decoded:
                        raise ValueError("Conflicting character mapping")
                    mapping[key] = decoded
        if len(mapping) > MAX_ENTRIES:
            raise ValueError("Character map exceeds safe bounds")
    if not spaces or any(not a or len(a) != len(b) or a > b for a, b in spaces):
        raise ValueError("Missing or invalid character code space")
    return mapping, spaces


def _codes(data, spaces):
    index = 0
    lengths = sorted({len(start) for start, _ in spaces})
    while index < len(data):
        matches = [data[index:index + length] for length in lengths
                   if index + length <= len(data) and any(
                       len(start) == length and start <= data[index:index + length] <= end
                       for start, end in spaces)]
        if len(matches) != 1:
            raise ValueError("Ambiguous or invalid character code")
        yield matches[0]
        index += len(matches[0])


def _font_risk(font, strings, reader):
    name = font_name(font.get("/BaseFont", "Unknown"))
    ref = getattr(font, "indirect_reference", None)
    record = {"font": name, "xref": ref.idnum if ref else None, "all": False, "reason": ""}
    if not strings:
        return record
    try:
        encoding = font.get("/Encoding")
        if "/ToUnicode" not in font:
            if (name in STANDARD_FONTS and encoding is None) or (isinstance(encoding, str) and encoding in STANDARD_ENCODINGS):
                return record
            raise ValueError("No reliable Unicode mapping for this font")
        mapping, spaces = unicode_map(font["/ToUnicode"], reader)
        simple = font.get("/Subtype") in {"/TrueType", "/Type1", "/MMType1", "/Type3"}
        # Simple-font content strings are single-byte character codes even when
        # an exporter puts a wider code-space declaration in its ToUnicode map.
        codes = (bytes([c]) for data in strings for c in data) if simple else (code for data in strings for code in _codes(data, spaces))
        missing = {code for code in codes
                   if code not in mapping or not mapping[code] or "\ufffd" in mapping[code]}
        if missing:
            # Extractors can guess different Unicode for the same missing code.
            # Do not assume a particular fallback character to locate the risk.
            record.update(
                all=True,
                reason="Used PDF character codes have no valid Unicode mapping.",
            )
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError) as exc:
        record.update(all=True, reason=str(exc))
    return record


def document_font_evidence(data):
    reader = PdfReader(io.BytesIO(data))
    result = {}
    for number, page in enumerate(reader.pages, 1):
        used = {}
        budget = [MAX_ENTRIES]

        def walk(stream, resources, current=None, ancestors=()):
            if len(ancestors) > 20:
                raise ValueError("Nested PDF resources exceed inspection limit")
            stack = []
            for operands, op in ContentStream(stream, reader).operations:
                budget[0] -= 1
                if budget[0] < 0:
                    raise ValueError("PDF text operations exceed inspection limit")
                if op == b"q":
                    stack.append(current)
                elif op == b"Q":
                    current = stack.pop() if stack else None
                elif op == b"Tf":
                    current = resources["/Font"][operands[0]]
                elif op in {b"Tj", b"TJ", b"'", b'"'}:
                    if current is None:
                        raise ValueError("Text uses an unresolved font")
                    font = current.get_object()
                    ref = getattr(font, "indirect_reference", None)
                    key = (ref.idnum, ref.generation) if ref else id(font)
                    entry = used.setdefault(key, {"font": font, "strings": []})
                    values = operands[0] if op == b"TJ" else [operands[-1]]
                    entry["strings"].extend(raw_bytes(v) for v in values if isinstance(v, (ByteStringObject, TextStringObject)))
                elif op == b"Do":
                    obj = resources["/XObject"][operands[0]].get_object()
                    if obj.get("/Subtype") != "/Form":
                        continue
                    ref = getattr(obj, "indirect_reference", None)
                    identity = ref.idnum if ref else id(obj)
                    if identity in ancestors:
                        raise ValueError("Cyclic PDF text resources")
                    walk(obj, obj.get("/Resources", resources), current, (*ancestors, identity))
        try:
            walk(page.get_contents(), page.get("/Resources", {}))
            result[str(number)] = {"fonts": [_font_risk(e["font"], e["strings"], reader) for e in used.values()], "error": ""}
        except Exception:
            # Inspection failure must be visible, not a guessed clean page.
            result[str(number)] = {"fonts": [], "error": "Font mapping inspection could not be completed on this page."}
    return result
