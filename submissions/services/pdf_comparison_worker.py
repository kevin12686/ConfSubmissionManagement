"""Isolated full-page RapidOCR comparison.

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
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


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
    # CPU includes all four ONNX threads; wall time is independently 240s.
    resource.setrlimit(resource.RLIMIT_CPU, (900, 920))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (6 * 1024 ** 3, 6 * 1024 ** 3))


def inspect_pair(before, after):
    from submissions.services.rapidocr_comparison.result import VERSION, build_result, validate_result
    from submissions.services.rapidocr_comparison.runtime import build_recognizer, extract_document

    try:
        recognizer = build_recognizer()
    except Exception:
        return {"version": VERSION, "error": "configuration"}
    try:
        pa, na = extract_document(before, recognizer)
        pb, nb = extract_document(after, recognizer)
        result = build_result(pa, pb, na, nb, identical_bytes=before == after)
        validate_result(result)
        return {"version": VERSION, "result": result}
    except Exception:
        return {"version": VERSION, "error": "ocr_failed"}


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
