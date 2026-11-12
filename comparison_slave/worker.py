"""Isolated runner. Never configure the conference app or load its models."""

from contextlib import redirect_stdout
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import struct
import sys

from .storage import MAX_PDF, MAX_RESULT, read_private


def configure(namespace_dir, secret_file):
    from django.conf import settings
    if settings.configured:
        raise RuntimeError("Worker requires a fresh settings namespace")
    secret = read_private(secret_file, 4096).strip()
    settings.configure(
        SECRET_KEY=hmac.new(secret, str(namespace_dir).encode(), hashlib.sha256).hexdigest(),
        BASE_DIR=Path(namespace_dir), INSTALLED_APPS=[], DATABASES={},
        USE_TZ=True,
    )


def read_pair(stream):
    header = stream.read(16)
    if len(header) != 16:
        raise ValueError("Invalid request")
    lengths = struct.unpack(">QQ", header)
    if any(not 0 < n <= MAX_PDF for n in lengths):
        raise ValueError("Invalid request")
    pair = [stream.read(n) for n in lengths]
    if any(len(data) != n for data, n in zip(pair, lengths)) or stream.read(1):
        raise ValueError("Invalid request")
    return pair


def execute(before, after):
    from submissions.services.comparison_protocol import page_render_hashes
    from submissions.services.pdf_comparison_execution import local_pdf_evidence
    from submissions.services.pdf_comparison_errors import ComparisonInProgress, PDFComparisonError
    diagnostics = {}
    try:
        result = local_pdf_evidence(before, after, diagnostics=diagnostics)
        return {"result": result, "diagnostics": diagnostics,
                "render_hashes": {"before": page_render_hashes(before), "after": page_render_hashes(after)}}
    except ComparisonInProgress:
        return {"error": "comparison_in_progress"}
    except PDFComparisonError:
        return {"error": "comparison_failed"}


def preflight():
    from submissions.services import comparison_protocol
    from submissions.services.pdf_comparison_execution import local_pdf_evidence  # noqa: F401
    from submissions.services.rapidocr_comparison.runtime import build_recognizer, ocr_backend
    from submissions.services.rapidocr_comparison.cache import runtime_fingerprint
    recognizer = build_recognizer()
    # CPU RapidOCR sessions are lazy too: readiness must instantiate both models.
    recognizer._load_det_model()
    recognizer._load_rec_model()
    return {"contract": comparison_protocol.contract_id(),
            "backend": ocr_backend(), "runtime": runtime_fingerprint()}


def main():
    from submissions.services.rapidocr_comparison.config import supervisor_seconds
    # The watchdog survives a web-process death and kills nested OCR children too.
    signal.signal(signal.SIGALRM, lambda *_: os.killpg(os.getpgrp(), signal.SIGKILL))
    signal.alarm(supervisor_seconds())
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    from submissions.services.pdf_comparison_worker import resource_limits
    resource_limits()
    try:
        with redirect_stdout(sys.stderr):
            configure(Path(sys.argv[2]), Path(sys.argv[3]))
            if sys.argv[1] == "preflight":
                payload = preflight()
            elif sys.argv[1] == "job":
                payload = execute(*read_pair(sys.stdin.buffer))
            else:
                raise ValueError("Invalid runner mode")
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_RESULT:
                raise ValueError("Result too large")
    except Exception:
        encoded = b'{"error":"comparison_failed"}'
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
