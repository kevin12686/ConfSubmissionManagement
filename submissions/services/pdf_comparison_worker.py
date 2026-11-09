"""Isolated occurrence-level text, font and glyph comparison evidence.

No Django, filesystem writes or publication metadata access.
"""

import json
import sys
import struct
from contextlib import redirect_stdout
from pathlib import Path

import fitz
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def read_pair(stream):
    header = stream.read(16)
    if len(header) != 16:
        raise ValueError("Incomplete comparison request")
    lengths = struct.unpack(">QQ", header)
    if any(not 0 < size <= MAX_INPUT_BYTES for size in lengths):
        raise ValueError("Comparison input exceeds safe bounds")
    documents = [stream.read(size) for size in lengths]
    if any(len(data) != size for data, size in zip(documents, lengths)) or stream.read(1):
        raise ValueError("Invalid comparison request length")
    return documents


def resource_limits():
    # Windows relies on the parent wall-clock timeout and bounded inputs.
    # Linux also bounds native allocations; macOS reserves large virtual ranges
    # for its runtime, so an address-space limit there would reject valid files.
    try:
        import resource
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (90, 95))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))


def inspect_pair(before, after):
    from submissions.services.pdf_glyph_evidence import inspect_pdf_pair
    from submissions.services.pdf_text_alignment import align_tokens

    for data in (before, after):
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                return {"error": "password_protected"}
            if not 0 < len(document) <= 100:
                raise ValueError("Comparison page count exceeds safe bounds")
    documents = inspect_pdf_pair(before, after)
    # Native glyph inventories support attribution inside the worker. The web
    # layer consumes bounded occurrence evidence, not the repeated native data.
    result = [{key: document[key] for key in (
        "tokens", "words", "pages", "controls", "coverage_reasons", "evidence_summary", "uncertain_regions",
    )} for document in documents]
    opcodes, limited = align_tokens(result[0]["tokens"], result[1]["tokens"], identical_bytes=before == after)
    result[0]["alignment"] = {"opcodes": opcodes, "limited": limited}
    return result


if __name__ == "__main__":
    # Keep library diagnostics away from the structured response.
    fitz.set_messages(stream=sys.stderr)
    resource_limits()
    with redirect_stdout(sys.stderr):
        before, after = read_pair(sys.stdin.buffer)
        evidence = inspect_pair(before, after)
        encoded = json.dumps(evidence, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("Comparison evidence exceeds safe output bounds")
    sys.stdout.buffer.write(encoded)
