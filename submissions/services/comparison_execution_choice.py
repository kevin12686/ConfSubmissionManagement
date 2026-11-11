"""Short-lived consent and safe display metadata, outside inference identity."""
import hashlib
import json

from django.conf import settings
from django.core import signing

from .pdf_comparison_errors import PDFComparisonError

SALT = "cfm.comparison-local-consent.v1"
MAX_AGE = 300


def configuration_identity():
    values = {key: getattr(settings, key, "") for key in (
        "CFM_COMPARISON_TRANSPORT", "CFM_COMPARISON_LOCAL_URL", "CFM_COMPARISON_LOCAL_TOKEN",
        "CFM_COMPARISON_REMOTE_URL", "CFM_COMPARISON_REMOTE_TOKEN", "CFM_COMPARISON_REMOTE_HEADERS",
    )}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def confirm_local_token(binding):
    return signing.dumps({"binding": binding, "configuration": configuration_identity()}, salt=SALT, compress=True)


def validate_local_token(token, binding):
    if not token:
        return False
    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError
        payload = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        if payload != {"binding": binding, "configuration": configuration_identity()}:
            raise ValueError
    except (ValueError, TypeError, signing.BadSignature) as exc:
        raise PDFComparisonError("Local CPU confirmation expired or the files/configuration changed. Recheck this comparison and confirm again; no new computation was authorized.") from exc
    return True


def execution_display(execution):
    if not isinstance(execution, dict):
        return {"label": "Worker not recorded", "worker": "", "backend": ""}
    transport = execution.get("transport")
    worker = execution.get("worker") if transport == "service" else "embedded" if transport == "embedded" else ""
    details = execution.get("worker_execution") or {}
    backend = details.get("backend", execution.get("backend")) if transport == "service" and isinstance(details, dict) else execution.get("backend")
    if backend not in ("cpu", "cuda"):
        backend = ""
    if worker not in ("remote", "local", "embedded"):
        worker = ""
    location = {"remote": "Remote", "local": "Local", "embedded": "Embedded"}.get(worker, "Worker not recorded")
    label = location + (" · GPU" if backend == "cuda" else " · CPU" if backend == "cpu" else "")
    return {"label": label, "worker": worker, "backend": backend}
