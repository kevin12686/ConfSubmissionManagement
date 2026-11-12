"""Content-bound compute routing; publication selection stays in CFM.

An uncertain submission remains pinned to its worker. Retrying reconciles the
same idempotent job instead of silently starting another job on a second host.
"""
from contextlib import contextmanager
import hashlib
from http.client import HTTPException
from itertools import islice
import json
import os
from pathlib import Path
import struct
import time
from urllib import error, parse, request
import uuid

from django.conf import settings
from django.core import signing
from django.core.files import locks

from .comparison_protocol import (
    PROTOCOL, MAX_PDF_BYTES, MAX_REPLY_BYTES, contract_id, job_key, page_render_hashes,
)
from conference_final_manager.comparison_config import parse_remote_headers
from conference_final_manager.comparison_http import NoRedirect, worker_headers, http_failure, transport_failure
from .pdf_comparison_errors import ComparisonInProgress, PDFComparisonError, LocalComparisonConfirmationRequired
from .rapidocr_comparison.result import validate_result

ROUTE_SALT = "cfm.compute-route.v1"
# Outlive a two-hour job plus its last bounded attempt/network round trips.
# Renew only on the same endpoint so accepted work cannot fail over at expiry.
ROUTE_SECONDS = 2 * 60 * 60 + 10 * 60
WAIT_SECONDS = 240


class TransportUnavailable(Exception):
    pass


class Endpoint:
    def __init__(self, name, url, token, *, custom_headers=""):
        try:
            self.custom_headers = parse_remote_headers(custom_headers, url=url)
        except ValueError as exc:
            raise PDFComparisonError(str(exc)) from None
        if self.custom_headers and name != "remote":
            raise PDFComparisonError("Custom comparison headers are permitted only for the remote worker.")
        parts = parse.urlsplit(url)
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"} or len(token) < 32
            or any(c.isspace() for c in token)
        ):
            raise PDFComparisonError(f"Comparison unavailable: configure a valid {name} worker URL and dedicated API token (32+ characters).")
        self.name, self.url, self.token = name, url.rstrip("/"), token
        self.identity = hashlib.sha256((self.url + "\n" + token).encode()).hexdigest()
        # Gateway credential rotation must not duplicate an accepted worker job.
        # Route identity remains the worker URL and its per-conference token.
        # Trusted LAN services must not inherit an unrelated machine HTTP proxy.
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def call(self, path, *, body=None, headers=None, timeout=None):
        req = request.Request(self.url + path, data=body, headers={
            **worker_headers(self.token, self.custom_headers),
            **({"Content-Type": "application/octet-stream"} if body is not None else {}),
            **(headers or {}),
        })
        try:
            try:
                response = self.opener.open(req, timeout=timeout if timeout is not None else 30 if body is not None else 5)
            except error.HTTPError as exc:
                response = exc
            with response:
                status = response.status
                response_headers = response.headers
                data = response.read(MAX_REPLY_BYTES + 1)
                if len(data) > MAX_REPLY_BYTES:
                    raise PDFComparisonError(f"HTTP {status}: comparison worker response exceeds the safe limit.")
            if status in {401, 403}:
                raise PDFComparisonError(f"Comparison {self.name} worker: {http_failure(status, data, response_headers, secrets=[self.token, *self.custom_headers.values()])}")
            if status in {301, 302, 303, 307, 308}:
                raise PDFComparisonError(http_failure(status, data, response_headers, secrets=[self.token, *self.custom_headers.values()]))
            if status in {502, 504}:
                # Proxies commonly return HTML when the upstream is unavailable.
                # Selection may fall back; an existing route remains pinned.
                raise TransportUnavailable(http_failure(status, data, response_headers, secrets=[self.token, *self.custom_headers.values()]))
            try:
                value = json.loads(data)
            except (ValueError, RecursionError) as exc:
                if 500 <= status < 600:
                    raise TransportUnavailable(f"HTTP {status}: worker/gateway returned non-JSON server failure.") from None
                raise PDFComparisonError(f"HTTP {status}: comparison worker returned invalid JSON (possibly a gateway/login page).") from None
            if not isinstance(value, dict):
                raise PDFComparisonError(f"HTTP {status}: comparison worker returned an invalid response; expected a JSON object.")
            return status, value
        except (error.URLError, OSError, TimeoutError) as exc:
            raise TransportUnavailable(transport_failure(exc)) from None
        except HTTPException as exc:
            # Invalid/incomplete HTTP is not permission to select a new worker.
            raise PDFComparisonError(transport_failure(exc)) from None


def _endpoints():
    endpoints = []
    raw_headers = getattr(settings, "CFM_COMPARISON_REMOTE_HEADERS", "")
    try:
        parse_remote_headers(raw_headers, url=getattr(settings, "CFM_COMPARISON_REMOTE_URL", ""))
    except ValueError as exc:
        raise PDFComparisonError(str(exc)) from None
    for name in ("remote", "local"):
        url = getattr(settings, f"CFM_COMPARISON_{name.upper()}_URL", "").strip()
        token = getattr(settings, f"CFM_COMPARISON_{name.upper()}_TOKEN", "").strip()
        if token and not url:
            raise PDFComparisonError(f"{name.title()} worker token is configured without its URL.")
        if url:
            endpoints.append(Endpoint(name, url, token, custom_headers=raw_headers if name == "remote" else ""))
    if not endpoints:
        raise PDFComparisonError("Comparison unavailable: configure a shared local worker before using service mode.")
    return endpoints


def probe_endpoint(endpoint, contract, *, timeout=None):
    """One health request; diagnostics never imply permission to submit a job."""
    try:
        status, health = endpoint.call("/v1/health", **({"timeout": timeout} if timeout is not None else {}))
    except TransportUnavailable as exc:
        return {"state": "unreachable", "backend": "", "message": str(exc) or "Worker connection unavailable."}
    backend = health.get("backend") if health.get("backend") in ("cpu", "cuda") else ""
    if health.get("protocol") != PROTOCOL or health.get("contract") != contract:
        return {"state": "incompatible", "backend": backend, "message": "Worker and CFM comparison versions are incompatible."}
    if status in {200, 503} and health.get("state") in ("ready", "busy"):
        message = ""
        models = health.get("gpu_models")
        if backend == "cuda" and isinstance(models, dict):
            states = {"warm": "loaded", "cold": "unloaded; loads when new OCR is needed",
                      "loading": "loading", "busy": "in use", "unavailable": "unavailable"}
            idle = models.get("idle_seconds")
            model_state = models.get("state")
            if isinstance(model_state, str) and model_state in states and type(idle) is int and 1 <= idle <= 86400:
                message = f"GPU models: {states[model_state]}. Idle unload: {idle} seconds."
        return {"state": health["state"], "backend": backend, "message": message}
    known = {"preflight_pending", "configuration_unavailable", "storage_or_scheduler_unavailable"}
    category = health.get("error")
    detail = f" ({category})" if isinstance(category, str) and category in known else ""
    return {"state": "not_ready", "backend": backend, "message": f"Worker is not ready{detail}. HTTP {status}."}


def _completed_result(payload, key, hashes, contract, before, after):
    _validate_identity(payload, key, hashes, contract)
    result = validate_result(payload.get("result"))
    if payload.get("state") != "completed" or result["identical_bytes"] != (before == after):
        raise PDFComparisonError("Invalid completed comparison identity.")
    if payload.get("render_hashes") != {"before": page_render_hashes(before), "after": page_render_hashes(after)}:
        raise PDFComparisonError("Comparison unavailable: worker and viewer render these PDFs differently. No potentially misplaced overlays were accepted. Align the rendering runtime and retry.")
    return result


def _find_completed(endpoints, contract, pair):
    before, after, key, hashes = pair
    for endpoint in endpoints:
        try:
            status, payload = endpoint.call(f"/v1/jobs/{key}")
        except TransportUnavailable:
            continue
        # Authentication/redirect failures are not permission to bypass the
        # configured gateway, even during cache-only discovery.
        try:
            if status == 200 and payload.get("state") == "completed":
                result = _completed_result(payload, key, hashes, contract, before, after)
                return endpoint, result, payload
        except (PDFComparisonError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return None


def _record_execution(diagnostics, endpoint, key, resumed, payload):
    if diagnostics is not None:
        diagnostics.update({"transport":"service", "worker":endpoint.name,"job_id":key,"resumed":resumed})
        details = payload.get("diagnostics", {})
        if isinstance(details, dict):
            diagnostics["worker_execution"] = {k:details[k] for k in ("runtime","pair_hit","document_hits","backend") if k in details}


def _select(endpoints, contract, *, allow_local=False, pair=None):
    if pair is not None:
        cached = _find_completed(endpoints, contract, pair)
        if cached:
            return cached[0]
    remote = next((endpoint for endpoint in endpoints if endpoint.name == "remote"), None)
    local = next((endpoint for endpoint in endpoints if endpoint.name == "local"), None)
    if remote:
        health = probe_endpoint(remote, contract)
        if health["state"] in {"ready", "busy"} and not allow_local:
            return remote
        if not allow_local:
            raise LocalComparisonConfirmationRequired(
                f"Remote comparison has not started. {health['message']} Local CPU has not been started.",
                can_confirm=local is not None,
            )
    for endpoint in endpoints:
        if endpoint.name == "remote":
            continue
        health = probe_endpoint(endpoint, contract)
        if health["state"] in {"ready", "busy"}:
            return endpoint
    raise PDFComparisonError("Comparison unavailable: no compatible worker is ready. Check worker models, connection and deployment versions.")


@contextmanager
def _route_lock():
    root = Path(settings.BASE_DIR) / "data" / "formatting_comparison_previews" / "compute"
    for path in (root.parent.parent, root.parent, root):
        if path.is_symlink():
            raise PDFComparisonError("Invalid compute request storage.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "routes.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as handle:
        if not locks.lock(handle, locks.LOCK_EX):
            raise ComparisonInProgress("Compute routing is busy. Retry shortly.")
        try:
            yield root
        finally:
            locks.unlock(handle)


def _route(key, endpoints, contract, *, allow_local=False, pair=None):
    with _route_lock() as root:
        routes = list(islice(root.glob("*.json"), 513))
        for old in routes:
            if not old.is_symlink() and time.time() - old.stat().st_mtime > ROUTE_SECONDS:
                old.unlink(missing_ok=True)
        path = root / f"{key}.json"
        endpoint = None
        resumed = False
        if path.is_symlink():
            raise PDFComparisonError("Invalid compute request storage.")
        if path.exists():
            try:
                if path.stat().st_size > 4096:
                    raise ValueError
                pinned = signing.loads(path.read_text(), salt=ROUTE_SALT, max_age=ROUTE_SECONDS)
                if pinned["key"] != key:
                    raise ValueError
                endpoint = next((e for e in endpoints if e.identity == pinned["endpoint"]), None)
                if endpoint is None:
                    raise PDFComparisonError("The worker configuration changed during this comparison. Restore the previous configuration or wait for the routing lease to expire (130 minutes after its last use).")
                resumed = True
            except signing.SignatureExpired:
                path.unlink()
            except (signing.BadSignature, KeyError, ValueError) as exc:
                if isinstance(exc, PDFComparisonError):
                    raise
                raise PDFComparisonError("Invalid compute routing receipt; no work was resubmitted.") from exc
        if endpoint is None:
            if sum(1 for _ in islice(root.glob("*.json"), 512)) >= 512:
                raise ComparisonInProgress("Too many recent comparison requests. Let current jobs finish and older requests expire.")
            endpoint = _select(endpoints, contract, allow_local=allow_local, pair=pair)
        # Persist intent before POST, so a lost response can be reconciled safely.
        temporary = root / (uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(signing.dumps({"key": key, "endpoint": endpoint.identity}, salt=ROUTE_SALT))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return endpoint, resumed


def _validate_identity(payload, key, hashes, contract):
    if (
        payload.get("protocol") != PROTOCOL or payload.get("contract") != contract
        or payload.get("key") != key or payload.get("hashes") != hashes
        or payload.get("state") not in {"queued", "running", "completed", "failed"}
    ):
        raise PDFComparisonError("The worker result does not match this PDF pair and comparison method. No result was accepted.")


def service_pdf_evidence(before, after, *, diagnostics=None, retry_failed=False, allow_local=False):
    if not all(0 < len(data) <= MAX_PDF_BYTES for data in (before, after)):
        raise PDFComparisonError("Comparison requires two non-empty PDFs of at most 64 MiB each.")
    contract = contract_id()
    hashes = [hashlib.sha256(data).hexdigest() for data in (before, after)]
    key = job_key(hashes, contract)
    endpoints = _endpoints()
    endpoint, resumed = _route(key, endpoints, contract, allow_local=allow_local, pair=(before, after, key, hashes))
    if resumed:
        # Reusing completed evidence on another host is not rerouting accepted
        # computation: the signed lease stays on its original endpoint.
        ordered = [endpoint] + [item for item in endpoints if item is not endpoint]
        cached = _find_completed(ordered, contract, (before, after, key, hashes))
        if cached:
            owner, result, payload = cached
            _record_execution(diagnostics, owner, key, resumed, payload)
            return result
    deadline = time.monotonic() + WAIT_SECONDS
    try:
        status, payload = endpoint.call(f"/v1/jobs/{key}")
        if endpoint.name == "local" and any(item.name == "remote" for item in endpoints) and not allow_local:
            # Old CPU routes are not permission to submit/retry or silently wait
            # for more local computation. Completed evidence is safe to reuse.
            if status in {200, 202}:
                _validate_identity(payload, key, hashes, contract)
            if status == 404 or (status in {200, 202} and payload.get("state") != "completed"):
                raise LocalComparisonConfirmationRequired(
                    "This PDF pair is already bound to the local worker. Confirm local CPU use to continue; no remote duplicate will be started.",
                    existing_local=True,
                )
        if status == 404 or (status == 200 and payload.get("state") == "failed" and retry_failed):
            if status == 200:
                _validate_identity(payload, key, hashes, contract)
            status, payload = endpoint.call(f"/v1/jobs/{key}",
                body=struct.pack(">QQ", len(before), len(after)) + before + after,
                headers={"X-CFM-Contract": contract, "X-CFM-Before-SHA256": hashes[0], "X-CFM-After-SHA256": hashes[1],
                         **({"X-CFM-Retry-Failed": "1"} if retry_failed else {})},
            )
        while True:
            if status == 429:
                raise ComparisonInProgress("The comparison worker is busy. Retry shortly; no second worker was started.")
            if status == 503:
                if payload.get("error") == "not_ready":
                    raise ComparisonInProgress("The selected worker is checking its models and runtime. Retry shortly to check the same job; saved files are unchanged.")
                raise PDFComparisonError("Comparison unavailable: the selected worker cannot provide valid evidence for its current runtime. Check worker diagnostics; no result was accepted.")
            if status not in {200, 202}:
                if status == 409 and payload.get("error") == "retry_limit":
                    raise PDFComparisonError("The worker retry limit for this PDF pair was reached. Check worker diagnostics and review the PDFs manually; retries are available after this job expires.")
                raise PDFComparisonError("The comparison worker could not accept this request. The saved PDF files are unchanged.")
            _validate_identity(payload, key, hashes, contract)
            state = payload["state"]
            if state == "failed":
                raise PDFComparisonError("Comparison unavailable: the worker could not complete this PDF pair safely. Review both PDFs manually; no complete comparison is available.")
            if state == "completed":
                break
            if time.monotonic() >= deadline:
                raise ComparisonInProgress("The comparison is still queued or running. Retry to retrieve the same job; no duplicate was started.")
            time.sleep(1)
            status, payload = endpoint.call(f"/v1/jobs/{key}")
        result = _completed_result(payload, key, hashes, contract, before, after)
        _record_execution(diagnostics, endpoint, key, resumed, payload)
        return result
    except TransportUnavailable as exc:
        raise ComparisonInProgress("The connection to the selected worker was interrupted. Retry to check the same job; no local duplicate was started and the saved files are unchanged.") from exc
    except PDFComparisonError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise PDFComparisonError("Comparison unavailable: invalid worker evidence. No result was accepted.") from exc
