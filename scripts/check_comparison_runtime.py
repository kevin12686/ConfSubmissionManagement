#!/usr/bin/env python3
"""Deployment-only preflight. Never load conference settings, models or data."""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PreflightError(RuntimeError):
    pass


def embedded_probe():
    from submissions.services.pdf_comparison_worker import resource_limits
    from submissions.services.rapidocr_comparison.runtime import build_recognizer, ocr_backend
    resource_limits()
    with redirect_stdout(sys.stderr):
        recognizer = build_recognizer()
        recognizer._load_det_model()
        recognizer._load_rec_model()
    return {"ready": True, "backend": ocr_backend()}


def check_embedded():
    environment = os.environ.copy()
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.setdefault("CFM_RAPIDOCR_MODEL_DIR", str(ROOT / "runtime" / "rapidocr-models"))
    executable = environment.get("CFM_RAPIDOCR_PYTHON", sys.executable)
    if not Path(executable).is_absolute():
        raise PreflightError("CFM_RAPIDOCR_PYTHON must be an absolute Python executable path.")
    try:
        with tempfile.TemporaryFile() as errors:
            result = subprocess.run([executable, str(Path(__file__).resolve()), "--embedded-probe"],
                env=environment, cwd=ROOT, stdout=subprocess.PIPE, stderr=errors, timeout=65, check=False)
        payload = json.loads(result.stdout) if len(result.stdout) <= 4096 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PreflightError("OCR runtime could not start within 65 seconds. Check the Python executable, installed requirements and model directory.") from exc
    if result.returncode or not isinstance(payload, dict) or payload.get("ready") is not True:
        reason = payload.get("error", "OCR native runtime failed") if isinstance(payload, dict) else "Invalid probe response"
        raise PreflightError(f"{reason}. Install requirements.txt and provision both approved models with prepare_rapidocr_models; check CFM_RAPIDOCR_MODEL_DIR and CFM_RAPIDOCR_THREADS.")
    return [f"Comparison preflight OK: embedded {payload['backend']} models and sessions loaded."]


def check_service(wait_seconds=90):
    from submissions.services.comparison_client import Endpoint, TransportUnavailable
    from submissions.services.comparison_protocol import PROTOCOL, contract_id
    from submissions.services.pdf_comparison_errors import PDFComparisonError
    endpoints = {}
    for name in ("local", "remote"):
        url = os.environ.get(f"CFM_COMPARISON_{name.upper()}_URL", "").strip()
        token = os.environ.get(f"CFM_COMPARISON_{name.upper()}_TOKEN", "").strip()
        if token and not url:
            raise PreflightError(f"{name.title()} worker token is configured without its URL.")
        if url:
            try:
                endpoints[name] = Endpoint(name, url, token)
            except (PDFComparisonError, ValueError) as exc:
                raise PreflightError(f"Invalid {name} worker URL/token. Configure CFM_COMPARISON_{name.upper()}_URL and its dedicated TOKEN (32+ characters).") from exc
    if not endpoints:
        raise PreflightError("Service transport needs a worker URL and dedicated token. Start the shared worker and configure CFM_COMPARISON_LOCAL_URL/TOKEN.")
    expected = contract_id()
    results = {}
    deadline = time.monotonic() + wait_seconds
    while True:
        for name, endpoint in endpoints.items():
            try:
                status, health = endpoint.call("/v1/health")
            except TransportUnavailable:
                results[name] = "unreachable"
                continue
            except PDFComparisonError as exc:
                # Authentication/redirect/invalid API errors cannot be hidden by fallback.
                raise PreflightError(f"{name.title()} worker rejected the health check. Check its token and direct API URL; redirects are not supported.") from exc
            if health.get("protocol") != PROTOCOL or health.get("contract") != expected:
                results[name] = "incompatible; update worker and CFM from the same checkout"
            elif status == 200 and health.get("state") in {"ready", "busy"}:
                results[name] = "ready"
            else:
                results[name] = "not ready; check worker models, backend and logs"
        required = "local" if "local" in endpoints else "remote"
        if results[required] == "ready":
            messages = [f"Comparison preflight OK: {required} worker authenticated, compatible and ready."]
            if "remote" in endpoints and required == "local":
                messages.append("Preferred remote worker ready." if results["remote"] == "ready"
                                else f"Remote worker {results['remote']}; validated local fallback will be used.")
            return messages
        if time.monotonic() >= deadline:
            raise PreflightError(f"Required {required} worker {results[required]}. No conference startup/cutover was authorized. Start the shared worker, validate models and set this conference's token.")
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedded-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-seconds", type=int, default=90)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 120:
        parser.error("--wait-seconds must be between 0 and 120")
    if sys.version_info < (3, 12):
        print("Preflight failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    if args.embedded_probe:
        try:
            print(json.dumps(embedded_probe()))
            return 0
        except Exception as exc:
            # No worker credentials enter the embedded path or its diagnostics.
            print(json.dumps({"ready": False, "error": str(exc)[:1000]}))
            return 1
    try:
        transport = os.environ.get("CFM_COMPARISON_TRANSPORT", "embedded")
        if transport == "embedded":
            messages = check_embedded()
        elif transport == "service":
            messages = check_service(args.wait_seconds)
        else:
            raise PreflightError("CFM_COMPARISON_TRANSPORT must be embedded or service.")
        for message in messages:
            print(message)
        return 0
    except (PreflightError, ImportError, OSError, ValueError) as exc:
        if isinstance(exc, ImportError):
            print("Preflight failed: Python dependencies are missing. Install requirements.txt or rebuild the Docker image.", file=sys.stderr)
        else:
            print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
