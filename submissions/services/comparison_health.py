"""Bounded service observations for Formatting, never job-routing authority."""
import os
import threading

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .comparison_client import Endpoint, probe_endpoint
from .comparison_execution_choice import configuration_identity
from .comparison_protocol import contract_id
from .comparison_metadata import execution_settings, release_metadata
from .pdf_comparison_errors import PDFComparisonError

_lock = threading.Lock()
CACHE_SECONDS = 5
POLL_SECONDS = 10


def comparison_service_status():
    identity = configuration_identity() + contract_id()
    key = "cfm-comparison-health:" + identity
    cached = cache.get(key)
    if cached:
        return cached
    if not _lock.acquire(blocking=False):
        return {"checking": True, "poll_seconds": POLL_SECONDS}
    try:
        cached = cache.get(key)
        if cached:
            return cached
        transport = getattr(settings, "CFM_COMPARISON_TRANSPORT", "embedded")
        result = {"transport": transport, "poll_seconds": POLL_SECONDS, "checked_at": timezone.now().isoformat(),
                  "app_version": settings.APP_VERSION}
        for name in ("remote", "local"):
            url = getattr(settings, f"CFM_COMPARISON_{name.upper()}_URL", "").strip()
            token = getattr(settings, f"CFM_COMPARISON_{name.upper()}_TOKEN", "").strip()
            item = {"configured": bool(url), "state": "not_configured", "backend": "", "message": "",
                    "worker_version": "", "settings": {}}
            if transport == "embedded":
                item.update(state="unused" if name == "remote" and url else "embedded" if name == "local" else "not_configured",
                            message="Embedded mode does not use the configured remote worker." if name == "remote" and url else "")
                if name == "local":
                    backend = os.environ.get("CFM_OCR_BACKEND", "cpu")
                    item.update(backend=backend if backend in ("cpu", "cuda") else "", backend_configured=True,
                                message="In-process execution is configured, not an independently probed service.")
                    try:
                        item.update(release_metadata(execution_settings()))
                    except ValueError:
                        item.update(release_metadata({}))
            elif transport != "service":
                item.update(state="configuration_error", message="Choose embedded or service comparison transport.")
            elif token and not url:
                item.update(state="configuration_error", message="Worker token is configured without its URL.")
            elif url:
                try:
                    endpoint = Endpoint(name, url, token, custom_headers=getattr(settings, "CFM_COMPARISON_REMOTE_HEADERS", "") if name == "remote" else "")
                    item.update(probe_endpoint(endpoint, contract_id(), timeout=2))
                except PDFComparisonError as exc:
                    item.update(state="error", message=str(exc))
                except (OSError, ValueError, TypeError):
                    item.update(state="configuration_error", message="Worker health could not be checked safely; check its deployment configuration.")
            backend_key = key + ":backend:" + name
            if item["backend"]:
                cache.set(backend_key, item["backend"], timeout=600)
            else:
                item["last_backend"] = cache.get(backend_key, "")
            metadata_key = key + ":metadata:" + name
            if item["worker_version"] or item["settings"]:
                cache.set(metadata_key, {"worker_version": item["worker_version"], "settings": item["settings"]}, timeout=600)
            elif item["state"] in {"unreachable", "error", "configuration_error"}:
                item["last_metadata"] = cache.get(metadata_key, {})
            result[name] = item
        cache.set(key, result, timeout=CACHE_SECONDS)
        return result
    finally:
        _lock.release()
