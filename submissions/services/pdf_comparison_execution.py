"""Local isolated inference shared by embedded CFM and standalone compute jobs.

Only minimal Django settings are needed for the signed cache. This module must
not import models, publication helpers or conference workflow services.
"""
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from contextlib import nullcontext

from django.conf import settings

from .comparison_protocol import MAX_PDF_BYTES
from .pdf_comparison_cache import ComparisonCacheBusy, comparison_cache_job
from .pdf_comparison_errors import ComparisonInProgress, PDFComparisonError


def local_pdf_evidence(before, after, *, diagnostics=None, cache_only=False, ephemeral=False):
    from .rapidocr_comparison.result import VERSION, validate_result
    from .rapidocr_comparison.config import timeout_seconds

    if not all(0 < len(data) <= MAX_PDF_BYTES for data in (before, after)):
        raise PDFComparisonError("Comparison requires two non-empty PDFs of at most 64 MiB each.")
    environment = os.environ.copy()
    if ephemeral:
        from .rapidocr_comparison.resident_protocol import ENV_KEYS
        for key in (*ENV_KEYS, "CFM_SLAVE_ROOT"):
            environment.pop(key, None)
        environment.update(CFM_OCR_BACKEND="cpu", PYTHONDONTWRITEBYTECODE="1")
    if diagnostics is not None:
        diagnostics.update({"transport": "embedded", "backend": environment.get("CFM_OCR_BACKEND", "cpu")})
    environment.setdefault("CFM_RAPIDOCR_MODEL_DIR", str(Path(settings.BASE_DIR) / "runtime" / "rapidocr-models"))
    executable = environment.get("CFM_RAPIDOCR_PYTHON", sys.executable)
    if not Path(executable).is_absolute():
        raise PDFComparisonError("Comparison unavailable: CFM_RAPIDOCR_PYTHON must be an absolute executable path.")
    try:
        wall_seconds = timeout_seconds()
        with (nullcontext() if ephemeral else comparison_cache_job(before, after)) as job:
            result = None
            try:
                command = [executable, "-B", str(Path(__file__).with_name("pdf_comparison_worker.py"))]
                if cache_only:
                    command.append("--cache-only")
                if job:
                    command.extend(["--cache-inputs", str(job.directory)])
                with (nullcontext(subprocess.DEVNULL) if ephemeral else tempfile.TemporaryFile()) as stderr:
                    response = subprocess.run(
                        command, input=struct.pack(">QQ", len(before), len(after)) + before + after,
                        stdout=subprocess.PIPE, stderr=stderr, timeout=wall_seconds, check=True,
                        env=environment,
                    )
                if len(response.stdout) > 8 * 1024 * 1024:
                    raise ValueError("OCR response exceeds the safe output limit")
                payload = json.loads(response.stdout)
                if not isinstance(payload, dict) or payload.get("version") != VERSION:
                    raise ValueError("Invalid OCR response version")
                if cache_only and payload.get("cache_miss") is True:
                    return None
                if "error" in payload:
                    errors = {
                        "configuration": "Local OCR models or dependencies are unavailable. Run prepare_rapidocr_models and install requirements.txt.",
                        "ocr_failed": "OCR could not complete safely. Review both PDFs manually; no native-text fallback was used.",
                    }
                    raise PDFComparisonError("Comparison unavailable: " + errors.get(payload["error"], "OCR worker failed."))
                evidence = validate_result(payload.get("result"))
                if evidence["identical_bytes"] != (before == after):
                    raise ValueError("Incorrect snapshot identity")
                result = evidence
                return result
            finally:
                if job:
                    try:
                        job.collect(result)
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                        logging.getLogger(__name__).warning("OCR cache update skipped; comparison outcome unchanged.", exc_info=True)
                    if diagnostics is not None:
                        diagnostics.update(job.stats)
    except ComparisonCacheBusy as exc:
        raise ComparisonInProgress(str(exc)) from exc
    except PDFComparisonError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        raise PDFComparisonError(
            "Comparison unavailable: OCR failed, exceeded its configured runtime or input limits, "
            "or returned invalid evidence. No complete comparison is available; review both PDFs manually."
        ) from exc
