"""Allowlisted observational metadata; never a compute or routing identity."""

import re

from submissions.version import APP_VERSION


def execution_settings():
    """Call in the actual execution environment, after runtime validation."""
    from .rapidocr_comparison.config import execution_identity, gpu_idle_seconds, timeout_seconds
    from .rapidocr_comparison.runtime import ocr_thread_count

    identity = execution_identity()
    return {"det": identity["det"], "rec": identity["rec"],
            "parallel_pages": identity["parallel_pages"], "threads": ocr_thread_count(),
            "timeout_seconds": timeout_seconds(), "gpu_idle_seconds": gpu_idle_seconds()}


def public_metadata(payload):
    """Never forward unknown fields, arbitrary peer prose, paths or credentials."""
    version = payload.get("worker_version")
    result = {"worker_version": version if isinstance(version, str) and len(version) <= 40
              and re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.-]{1,24})?", version) else "",
              "settings": {}}
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return result
    for key in ("det", "rec"):
        value = settings.get(key)
        if isinstance(value, str) and value in {"small", "medium"}:
            result["settings"][key] = value
    for key, low, high in (("parallel_pages", 1, 8), ("threads", 1, 8),
                           ("timeout_seconds", 30, 3600), ("gpu_idle_seconds", 0, 86400)):
        value = settings.get(key)
        if type(value) is int and low <= value <= high:
            result["settings"][key] = value
    return result


def release_metadata(settings):
    return public_metadata({"worker_version": APP_VERSION, "settings": settings})
