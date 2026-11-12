"""WSGI entrypoint: gunicorn --workers 1 --threads 4 comparison_slave.server:application."""

import atexit
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import threading
import time
from http import HTTPStatus

from .runner import Service
from .storage import HEX, MAX_PDF, MAX_RESPONSE, QueueFull, RetryLimit

logger = logging.getLogger(__name__)


class HTTPError(Exception):
    def __init__(self, status, error):
        self.status, self.error = status, error


def read_exact(stream, length):
    chunks = []
    remaining = length
    while remaining:
        chunk = stream.read(min(remaining, 1024 * 1024))
        if not chunk or len(chunk) > remaining:
            raise HTTPError(400, "invalid_body")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class Application:
    def __init__(self, service):
        self.service = service
        self.upload_lock = threading.Lock()

    def authenticate(self, environ):
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(authorization, str) or len(authorization) > 520 or not authorization.startswith("Bearer "):
            raise HTTPError(401, "unauthorized")
        token = authorization[7:].encode("utf-8")
        namespace = None
        for name, expected in self.service.clients.items():
            if hmac.compare_digest(token, expected.encode("ascii")):
                namespace = name
        if namespace is None:
            raise HTTPError(401, "unauthorized")
        return namespace

    def job_reply(self, row, contract):
        if row is None or self.service.ledger.expired(row):
            raise HTTPError(404, "not_found")
        reply = {"protocol": 1, "contract": row["contract"], "key": row["key"],
                 "hashes": [row["before_hash"], row["after_hash"]],
                 "state": row["state"], "error": row["error"], "attempts": row["attempts"]}
        if row["state"] == "completed":
            if row["contract"] != contract or not self.service.runtime_ready():
                self.service.probe_requested.set()
                self.service.wake.set()
                raise HTTPError(503, "not_ready")
            encoded = row["output"]
            if not encoded or len(encoded) > MAX_RESPONSE:
                raise HTTPError(503, "invalid_evidence")
            payload = json.loads(encoded)
            if payload.pop("runtime", None) != self.service.runtime:
                raise HTTPError(503, "incompatible_runtime")
            reply.update(payload)
        return reply

    def dispatch(self, environ):
        namespace = self.authenticate(environ)
        from submissions.services import comparison_protocol
        contract = comparison_protocol.contract_id()
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "")
        if path == "/v1/health":
            if method != "GET":
                raise HTTPError(405, "method_not_allowed")
            ready = self.service.runtime_ready()
            busy = self.service.active or self.service.ledger.busy(running_only=not ready)
            if not ready:
                self.service.probe_requested.set()
                self.service.wake.set()
            state = "busy" if busy else "ready" if ready else "not_ready"
            payload = {"protocol": 1, "contract": contract, "state": state, "backend": self.service.backend,
                       "error": "" if ready or busy else self.service.health_error or "preflight_pending"}
            from submissions.services.comparison_metadata import release_metadata
            payload.update(release_metadata(self.service.public_settings))
            if self.service.resident:
                payload["gpu_models"] = self.service.resident.status()
            return 200, payload
        match = re.fullmatch(r"/v1/jobs/([a-f0-9]{64})", path)
        if not match:
            raise HTTPError(404, "not_found")
        key = match[1]
        if method == "GET":
            return 200, self.job_reply(self.service.ledger.get(namespace, key), contract)
        if method != "POST":
            raise HTTPError(405, "method_not_allowed")
        retry = environ.get("HTTP_X_CFM_RETRY_FAILED", "")
        if retry not in {"", "1"}:
            raise HTTPError(400, "invalid_retry")
        if environ.get("HTTP_X_CFM_CONTRACT") != contract:
            raise HTTPError(409, "incompatible_contract")
        hashes = [environ.get(f"HTTP_X_CFM_{side}_SHA256", "") for side in ("BEFORE", "AFTER")]
        if any(not isinstance(h, str) or not HEX.fullmatch(h) for h in hashes):
            raise HTTPError(400, "invalid_hashes")
        if comparison_protocol.job_key(hashes, contract) != key:
            raise HTTPError(400, "invalid_key")
        if environ.get("HTTP_TRANSFER_ENCODING"):
            raise HTTPError(400, "invalid_framing")
        raw_length = environ.get("CONTENT_LENGTH", "")
        if not raw_length:
            raise HTTPError(411, "length_required")
        if not re.fullmatch(r"[0-9]{1,10}", raw_length):
            raise HTTPError(400, "invalid_length")
        length = int(raw_length)
        if not 18 <= length <= 16 + 2 * MAX_PDF:
            raise HTTPError(413, "input_limit")
        if environ.get("CONTENT_TYPE", "application/octet-stream") != "application/octet-stream":
            raise HTTPError(415, "unsupported_media_type")
        if not self.upload_lock.acquire(blocking=False):
            raise HTTPError(429, "upload_busy")
        try:
            stream = environ["wsgi.input"]
            header = read_exact(stream, 16)
            lengths = struct.unpack(">QQ", header)
            if any(not 0 < n <= MAX_PDF for n in lengths) or sum(lengths) + 16 != length:
                raise HTTPError(400, "invalid_body")
            documents = [read_exact(stream, n) for n in lengths]
            if [hashlib.sha256(data).hexdigest() for data in documents] != hashes:
                raise HTTPError(400, "hash_mismatch")
            existing = self.service.ledger.get(namespace, key)
            if existing and not self.service.ledger.expired(existing) and not (retry and existing["state"] == "failed"):
                reply = self.job_reply(existing, contract)
                return (202 if reply["state"] in {"queued", "running"} else 200), reply
            if not self.service.runtime_ready():
                self.service.probe_requested.set()
                self.service.wake.set()
                raise HTTPError(503, "not_ready")
            self.service.ledger.prune()
            self.service.ledger.submit(namespace, key, contract, hashes, header + b"".join(documents), retry_failed=bool(retry))
            self.service.wake.set()
            return 202, self.job_reply(self.service.ledger.get(namespace, key), contract)
        finally:
            self.upload_lock.release()

    def __call__(self, environ, start_response):
        headers = [("Content-Type", "application/json"), ("Cache-Control", "no-store")]
        try:
            status, payload = self.dispatch(environ)
            body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            if len(body) > MAX_RESPONSE:
                raise HTTPError(503, "response_limit")
        except (HTTPError, QueueFull, RetryLimit) as exc:
            status, error = ((exc.status, exc.error) if isinstance(exc, HTTPError)
                             else (409, "retry_limit") if isinstance(exc, RetryLimit) else (429, "queue_full"))
            body = json.dumps({"protocol": 1, "error": error}).encode()
        except Exception:
            status, body = 503, b'{"protocol":1,"error":"unavailable"}'
        if status == 401:
            headers.append(("WWW-Authenticate", "Bearer"))
        if status in {429, 503}:
            headers.append(("Retry-After", "5"))
        headers.append(("Content-Length", str(len(body))))
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]


_application = None
_initialization = threading.Lock()
_retry_at = 0.0


def application(environ, start_response):
    global _application, _retry_at
    if _application is None:
        with _initialization:
            if _application is None and time.monotonic() >= _retry_at:
                try:
                    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
                    service = Service(os.environ["CFM_SLAVE_ROOT"], os.environ["CFM_SLAVE_CLIENTS_FILE"],
                                      secret_file=os.environ.get("CFM_SLAVE_SECRET"))
                    _application = Application(service)
                    atexit.register(service.close)
                except Exception as exc:
                    logger.warning("comparison_slave startup_configuration_unavailable type=%s", type(exc).__name__)
                    _retry_at = time.monotonic() + 30
        if _application is None:
            body = b'{"protocol":1,"error":"unavailable"}'
            start_response("503 Service Unavailable", [("Content-Type", "application/json"),
                           ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), ("Retry-After", "5")])
            return [body]
    return _application(environ, start_response)
