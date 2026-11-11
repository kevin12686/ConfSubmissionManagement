"""Real loopback HTTP exercises gateway authentication without conference data."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import tempfile
import threading
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from scripts import check_comparison_runtime as preflight
from submissions.services import comparison_client as client
from submissions.services.comparison_protocol import contract_id, page_render_hashes
from submissions.services.pdf_comparison_errors import PDFComparisonError
from submissions.services.rapidocr_comparison.result import build_result
from submissions.tests.ocr_fixtures import ocr_pages
from submissions.tests.test_pdf_comparison import make_pdf


class GatewayHTTPTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.token = "worker-test-" * 4
        self.headers = {"CF-Access-Client-Id": "client", "CF-Access-Client-Secret": "secret$#'\\\""}
        self.seen, self.jobs, self.uploads = [], {}, []
        self.denial = None
        self.before = make_pdf(["The original text preserves the scientific context in this paragraph."])
        self.after = make_pdf(["The corrected text preserves the scientific context in this paragraph."])
        self.hashes = [hashlib.sha256(pdf).hexdigest() for pdf in (self.before, self.after)]
        self.result = build_result(
            ocr_pages("The original text preserves the scientific context in this paragraph."),
            ocr_pages("The corrected text preserves the scientific context in this paragraph."),
            [], [], identical_bytes=False,
        )
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                owner.seen.append((self.command, self.path, dict(self.headers)))
                if owner.denial:
                    code = owner.denial
                    self.send_response(code)
                    self.send_header("Location", owner.url + "/must-not-follow")
                    self.end_headers()
                    self.wfile.write(b"<html>Login required</html>")
                    return
                if (self.headers.get("Authorization") != "Bearer " + owner.token
                        or any(self.headers.get(k) != v for k, v in owner.headers.items())):
                    code, payload = 403, {"error": "gateway_auth"}
                elif self.path == "/v1/health":
                    code, payload = 200, {"protocol": 1, "contract": contract_id(), "state": "ready"}
                elif self.command == "POST":
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    owner.uploads.append(body)
                    key = self.path.rsplit("/", 1)[-1]
                    owner.jobs[self.path] = {
                        "protocol": 1, "contract": self.headers["X-CFM-Contract"], "key": key,
                        "hashes": [self.headers["X-CFM-Before-SHA256"], self.headers["X-CFM-After-SHA256"]],
                        "state": "completed", "result": owner.result,
                        "render_hashes": {"before": page_render_hashes(owner.before), "after": page_render_hashes(owner.after)},
                    }
                    code, payload = 202, {**owner.jobs[self.path], "state": "running"}
                elif self.path in owner.jobs:
                    code, payload = 200, owner.jobs[self.path]
                else:
                    code, payload = 404, {"error": "not_found"}
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            do_GET = do_POST = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(close)
        self.enterContext(override_settings(BASE_DIR=self.root,
            CFM_COMPARISON_REMOTE_URL=self.url, CFM_COMPARISON_REMOTE_TOKEN=self.token,
            CFM_COMPARISON_REMOTE_HEADERS=json.dumps(self.headers), CFM_COMPARISON_LOCAL_URL=""))

    def test_preflight_submit_poll_and_rotated_credentials_use_same_authenticated_job(self):
        import os
        with patch.dict(os.environ, {
            "CFM_COMPARISON_LOCAL_URL": "", "CFM_COMPARISON_LOCAL_TOKEN": "",
            "CFM_COMPARISON_REMOTE_URL": self.url, "CFM_COMPARISON_REMOTE_TOKEN": self.token,
            "CFM_COMPARISON_REMOTE_HEADERS": json.dumps(self.headers),
        }):
            self.assertIn("authenticated", preflight.check_service(0)[0])
        with patch.object(client.time, "sleep"):
            result = client.service_pdf_evidence(self.before, self.after)
        self.assertEqual(result, self.result)
        self.assertEqual(self.uploads, [struct.pack(">QQ", len(self.before), len(self.after)) + self.before + self.after])
        for method, path, headers in self.seen:
            headers = {key.lower(): value for key, value in headers.items()}
            self.assertEqual(headers["authorization"], "Bearer " + self.token)
            for key, value in self.headers.items():
                self.assertEqual(headers[key.lower()], value)
        self.headers["CF-Access-Client-Secret"] = "rotated-secret"
        with override_settings(CFM_COMPARISON_REMOTE_HEADERS=json.dumps(self.headers)):
            self.assertEqual(client.service_pdf_evidence(self.before, self.after), result)
        self.assertEqual(len(self.uploads), 1)
        receipts = list((self.root / "data/formatting_comparison_previews/compute").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertNotIn("rotated-secret", receipts[0].read_text())
        path = next(iter(self.jobs))
        self.jobs[path] = {**self.jobs[path], "state": "failed", "error": "comparison_failed"}
        with override_settings(CFM_COMPARISON_REMOTE_HEADERS=json.dumps(self.headers)), patch.object(client.time, "sleep"):
            self.assertEqual(client.service_pdf_evidence(self.before, self.after, retry_failed=True), result)
        self.assertEqual(len(self.uploads), 2)
        retry = [headers for method, url, headers in self.seen if method == "POST"][-1]
        retry = {key.lower(): value for key, value in retry.items()}
        self.assertEqual(retry["x-cfm-retry-failed"], "1")
        self.assertEqual(retry["cf-access-client-secret"], "rotated-secret")

    def test_access_denial_or_login_redirect_cannot_fall_back_or_follow_redirect(self):
        for code in (401, 403, 302):
            self.denial = code
            self.seen.clear()
            with self.subTest(code=code), override_settings(
                CFM_COMPARISON_LOCAL_URL="http://must-not-contact.invalid", CFM_COMPARISON_LOCAL_TOKEN="l" * 40,
            ), self.assertRaises(PDFComparisonError) as raised:
                client.service_pdf_evidence(self.before, self.after)
            self.assertNotIn(self.headers["CF-Access-Client-Secret"], str(raised.exception))
            self.assertEqual([(m, p) for m, p, h in self.seen], [("GET", "/v1/health")])
            self.assertEqual(self.uploads, [])

    def test_local_endpoint_never_receives_remote_gateway_headers(self):
        self.headers = {}
        local = client.Endpoint("local", self.url, self.token)
        self.assertEqual(local.call("/v1/health")[0], 200)
        for _, _, headers in self.seen:
            self.assertFalse(any(key.lower().startswith("cf-access-") for key in headers))
        with self.assertRaises(PDFComparisonError):
            client.Endpoint("local", self.url, self.token, custom_headers='{"X-Key":"secret"}')
