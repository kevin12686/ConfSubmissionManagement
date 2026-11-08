"""Isolated comparison evidence: table geometry and used-font mapping coverage.

No Django, filesystem writes or publication metadata access.
"""

import json
import sys
from contextlib import redirect_stdout

import fitz
from pdf_text_evidence import document_font_evidence, font_name


def font_evidence(data):
    evidence = document_font_evidence(data)
    aliases = {}
    with fitz.open(stream=data, filetype="pdf") as document:
        for page in evidence.values():
            for record in page["fonts"]:
                xref = record["xref"]
                if xref not in aliases:
                    aliases[xref] = []
                    try:
                        buffer = document.extract_font(xref)[3] if xref else b""
                        if buffer:
                            aliases[xref] = [font_name(fitz.Font(fontbuffer=buffer).name)]
                    except (RuntimeError, ValueError):
                        pass
                record["aliases"] = [record["font"], *aliases[xref]]
    return evidence


def read_cells(data):
    result = {}
    with fitz.open(stream=data, filetype="pdf") as document:
        for number, page in enumerate(document, 1):
            # Text extraction coordinates are unrotated. This changes only the
            # worker's in-memory document, never the source or its snapshot.
            page.set_rotation(0)
            paths = page.get_drawings()
            if not paths:
                continue
            finder = page.find_tables(paths=paths)
            if finder is None:
                raise ValueError("Table detection failed")
            result[str(number)] = [
                [cell for row in table.rows for cell in row.cells if cell]
                for table in finder.tables
            ]
    return result


if __name__ == "__main__":
    # Keep library diagnostics away from the structured response.
    fitz.set_messages(stream=sys.stderr)
    with redirect_stdout(sys.stderr):
        data = sys.stdin.buffer.read()
        evidence = {"tables": read_cells(data), "fonts": font_evidence(data)}
    sys.stdout.write(json.dumps(evidence))
