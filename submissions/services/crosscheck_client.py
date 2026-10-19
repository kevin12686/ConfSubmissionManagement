"""HTTP boundary for the optional, operator-configured CrossCheck service."""

import base64
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from urllib import error, parse, request

from django.conf import settings


class CrossCheckServiceError(ValueError):
    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def normalize_service_url(value):
    value = str(value or "").strip().rstrip("/")
    parsed = parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Enter an HTTP(S) service URL without credentials, query, or fragment.")
    return value


def secrets_path():
    return Path(settings.BASE_DIR) / "data" / "secrets" / "crosscheck.json"


def read_service_secrets(url):
    path = secrets_path()
    saved = json.loads(path.read_text()) if path.exists() else {}
    if saved.get("url") != normalize_service_url(url):
        saved = {}
    for key in ("web_username", "web_password", "ieee_username", "ieee_password"):
        saved[key] = os.environ.get("SMS_CROSSCHECK_" + key.upper()) or saved.get(key, "")
    return saved


def save_service_secrets(url, values, *, remove=False):
    from submissions.services.audit import audit_requested, audit_success

    path = secrets_path()
    previous = path.read_bytes() if path.exists() else None
    current = json.loads(previous) if previous and not remove else {}
    if current.get("url") != normalize_service_url(url):
        current = {}
    for key in ("web_username", "web_password", "ieee_username", "ieee_password"):
        if values.get(key):
            current[key] = values[key]
    for prefix in ("web", "ieee"):
        if not remove and bool(current.get(prefix + "_username")) != bool(current.get(prefix + "_password")):
            raise ValueError("Provide both username and password for the optional account.")
    current["url"] = normalize_service_url(url)
    audit_requested("crosscheck_service_credentials", "Optional local credential update requested.")
    try:
        if remove:
            path.unlink(missing_ok=True)
        else:
            _write_secret_bytes(path, json.dumps(current).encode())
        audit_success("crosscheck_service_credentials", "Optional local CrossCheck credentials removed." if remove else "Optional local CrossCheck credentials saved.")
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _write_secret_bytes(path, previous)
        raise


def _write_secret_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CrossCheckClient:
    def __init__(self, url):
        self.url = normalize_service_url(url)
        self.credentials = read_service_secrets(url)
        for prefix in ("web", "ieee"):
            if bool(self.credentials.get(prefix + "_username")) != bool(self.credentials.get(prefix + "_password")):
                raise CrossCheckServiceError("Optional account configuration requires both username and password.")

    def _open(self, path, *, method="GET", data=None, headers=None, timeout=30):
        headers = dict(headers or {})
        username = self.credentials.get("web_username")
        if username:
            auth = base64.b64encode((username + ":" + self.credentials["web_password"]).encode()).decode()
            headers["Authorization"] = "Basic " + auth
        req = request.Request(self.url + path, method=method, data=data, headers=headers)
        try:
            return request.build_opener(_NoRedirect()).open(req, timeout=timeout)
        except error.HTTPError as exc:
            labels = {401: "Service login required or rejected.", 403: "Service access rejected.",
                      409: "CrossCheck service is busy, or the result is not ready.",
                      404: "Service job was not found or its results have expired."}
            # Do not echo remote response bodies: they may contain credentials.
            raise CrossCheckServiceError(labels.get(exc.code, f"CrossCheck service returned HTTP {exc.code}."), http_status=exc.code) from None
        except (OSError, error.URLError, TimeoutError):
            raise CrossCheckServiceError("CrossCheck service connection failed or timed out.") from None

    def json(self, path, *, method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        with self._open(path, method=method, data=body, headers={"Content-Type": "application/json"}) as response:
            try:
                result = json.loads(response.read(4 * 1024 * 1024))
            except (ValueError, OSError):
                raise CrossCheckServiceError("Service returned an invalid JSON response.") from None
        if not isinstance(result, dict):
            raise CrossCheckServiceError("Service response must be a JSON object.")
        return result

    def folders(self):
        username = self.credentials.get("ieee_username")
        data = self.json("/api/folders", method="POST", payload={
            "username": username, "password": self.credentials["ieee_password"],
        }) if username else self.json("/api/folders")
        if not isinstance(data.get("folders"), list) or not all(isinstance(x, str) for x in data["folders"]):
            raise CrossCheckServiceError("Service returned an invalid folder list.")
        return data

    def upload(self, job, inputs, *, before_send=None):
        boundary = "CFM" + uuid.uuid4().hex
        with tempfile.TemporaryFile() as body:
            fields = {"folder": job.folder, "postfix": "_" + job.token, "wait_minutes": str(job.wait_minutes)}
            if self.credentials.get("ieee_username"):
                fields.update(username=self.credentials["ieee_username"], password=self.credentials["ieee_password"])
            for key, value in fields.items():
                body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            for filename, path, expected_hash in inputs:
                body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        body.write(chunk)
                if digest.hexdigest() != expected_hash:
                    raise CrossCheckServiceError("Prepared PDF changed while building the upload.")
                body.write(b"\r\n")
            body.write(f"--{boundary}--\r\n".encode())
            length = body.tell()
            body.seek(0)
            if before_send:
                before_send()
            with self._open("/api/upload-and-download", method="POST",
                            data=iter(lambda: body.read(1024 * 1024), b""),
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(length)},
                            timeout=120) as response:
                try:
                    return json.loads(response.read(4 * 1024 * 1024))
                except ValueError:
                    raise CrossCheckServiceError("Upload response is invalid; check service jobs before retrying.") from None

    def download(self, remote_id, destination):
        with self._open(f"/api/jobs/{remote_id}/download", timeout=120) as response, destination.open("xb") as output:
            total = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                total += len(chunk)
                if total > 2 * 1024 ** 3:
                    raise CrossCheckServiceError("Service result ZIP exceeds the 2 GB safety limit.")
                output.write(chunk)
