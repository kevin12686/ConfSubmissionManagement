"""Standard-library HTTP identity and secret-safe worker diagnostics."""
import json
from http.client import HTTPException
import re
import socket
import ssl
from urllib import request


# This is the compute transport generation, not an impersonated browser or an
# inference/cache identity. It need not change with each application release.
USER_AGENT = "ConferenceFinalManager/1"


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def worker_headers(token, custom=None):
    return {
        "User-Agent": USER_AGENT, "Accept": "application/json",
        **(custom or {}), "Authorization": "Bearer " + token,
    }


def http_failure(status, data, headers, *, secrets=()):
    """Only emit allowlisted codes and a bounded Ray ID, never remote prose."""
    try:
        payload = json.loads(data)
    except (ValueError, RecursionError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("error_code")
    cloudflare = payload.get("cloudflare_error") is True
    ray = headers.get("cf-ray", "")
    if not ray and cloudflare:
        ray = payload.get("ray_id", "")
    safe_ray = (isinstance(ray, str) and re.fullmatch(r"[a-fA-F0-9]{16,32}(?:-[A-Z]{3})?", ray)
                and not any(secret and secret in ray for secret in secrets))
    suffix = f" Ray ID: {ray}." if safe_ray else ""
    if cloudflare and type(code) is int and 1000 <= code <= 1999:
        reason = (
            "Browser signature/User-Agent was blocked; check the hostname's Browser Integrity Check or browser-signature rules."
            if code == 1010 else "The gateway rejected this request; inspect Cloudflare events using the Ray ID."
        )
        return f"HTTP {status}: Cloudflare error {code}. {reason}{suffix}"
    if status == 401 and payload.get("protocol") == 1 and payload.get("error") == "unauthorized":
        return f"HTTP 401: worker rejected its API token. Check this conference's worker credential.{suffix}"
    if status in {401, 403}:
        return f"HTTP {status}: worker or access gateway denied access. Check worker token, remote custom headers, Service Auth and gateway security rules.{suffix}"
    if status in {301, 302, 303, 307, 308}:
        return f"HTTP {status}: redirects are not permitted. Use the direct worker API URL and machine authentication, not a login page.{suffix}"
    return f"HTTP {status}: worker/gateway returned an unexpected response.{suffix}"


def transport_failure(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return "Worker DNS lookup failed."
    if isinstance(reason, ssl.SSLError):
        return "Worker TLS certificate or handshake validation failed."
    if isinstance(reason, TimeoutError):
        return "Worker request timed out."
    if isinstance(reason, ConnectionRefusedError):
        return "Worker connection was refused."
    if isinstance(reason, HTTPException):
        return "Worker HTTP response was incomplete or malformed."
    return "Worker network connection failed."
