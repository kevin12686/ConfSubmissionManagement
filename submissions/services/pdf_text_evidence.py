"""Bounded PDF character-map parsing shared by glyph occurrence inspection."""

from pypdf.generic import ByteStringObject, ContentStream, TextStringObject


MAX_ENTRIES = 100000
STANDARD_FONTS = {
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic", "Symbol", "ZapfDingbats",
}
STANDARD_ENCODINGS = {"/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding"}


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
