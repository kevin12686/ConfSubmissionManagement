"""HTTP identity and error diagnostics never expose gateway response secrets."""
import json
from http.client import IncompleteRead
import socket
import ssl
import unittest
from urllib.error import URLError

from conference_final_manager.comparison_http import USER_AGENT, http_failure, transport_failure, worker_headers


class WorkerHTTPTests(unittest.TestCase):
    def test_shared_user_agent_is_descriptive_and_preserves_worker_authentication(self):
        self.assertTrue(USER_AGENT.startswith("ConferenceFinalManager/"))
        self.assertNotIn("Python-urllib", USER_AGENT)
        self.assertEqual(worker_headers("worker-token")["Authorization"], "Bearer worker-token")
        self.assertEqual(worker_headers("token", {"User-Agent": "Operator/2"})["User-Agent"], "Operator/2")

    def test_cloudflare_errors_are_code_specific_but_never_echo_remote_prose(self):
        data = json.dumps({"cloudflare_error": True, "error_code": 1010,
                           "title": "private-secret", "detail": "private-secret",
                           "ray_id": "a58b4d0c5fa70c43"}).encode()
        text = http_failure(403, data, {})
        for expected in ("HTTP 403", "1010", "User-Agent", "a58b4d0c5fa70c43"):
            self.assertIn(expected, text)
        self.assertNotIn("private-secret", text)
        text = http_failure(403, b'{"cloudflare_error":true,"error_code":1020}', {})
        self.assertIn("1020", text)
        self.assertNotIn("User-Agent", text)

    def test_ray_id_is_bounded_and_cannot_echo_a_request_secret(self):
        for ray in ("private-secret", "https://login/?token=private-secret", "a" * 5000, "a" * 32):
            text = http_failure(403, b"{}", {"cf-ray": ray}, secrets=["a" * 32])
            self.assertNotIn(ray, text)
            self.assertNotIn("private-secret", text)

    def test_worker_auth_gateway_denial_and_redirect_are_distinct(self):
        text = http_failure(401, b'{"protocol":1,"error":"unauthorized"}', {})
        self.assertIn("worker rejected its API token", text)
        self.assertNotIn("Service Auth", text)
        self.assertIn("Service Auth", http_failure(403, b"{}", {}))
        for code in (301, 302, 303, 307, 308):
            text = http_failure(code, b"{}", {"Location": "https://login/?token=private-secret"})
            self.assertIn(f"HTTP {code}", text)
            self.assertIn("redirects", text)
            self.assertNotIn("private-secret", text)

    def test_invalid_remote_payloads_use_only_safe_fallback_messages(self):
        for data in (b"<html>private-secret</html>", b"[]", b"null", b"{", b"[" * 1200):
            self.assertNotIn("private-secret", http_failure(403, data, {}))

    def test_network_errors_are_specific_without_echoing_urls_or_tokens(self):
        cases = [(socket.gaierror("private-secret"), "DNS"),
                 (ssl.SSLError("private-secret"), "TLS"),
                 (TimeoutError("private-secret"), "timed out"),
                 (ConnectionRefusedError("private-secret"), "refused"),
                 (IncompleteRead(b"private-secret", 1), "incomplete"),
                 (OSError("private-secret"), "network")]
        for reason, expected in cases:
            for exc in (reason, URLError(reason)):
                text = transport_failure(exc)
                self.assertIn(expected, text)
                self.assertNotIn("private-secret", text)
