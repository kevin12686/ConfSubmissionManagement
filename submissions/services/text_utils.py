import re


def natural_text_key(value):
    """Return a case-insensitive key that keeps numeric ID chunks in numeric order."""
    parts = re.split(r"(\d+)", str(value or ""))
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in parts
        if part
    )


def clean_note_text(value):
    if value is None:
        return ""
    lines = str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned = []
    previous_blank = False
    for line in lines:
        line = line.strip()
        if not line:
            if cleaned and not previous_blank:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned)
