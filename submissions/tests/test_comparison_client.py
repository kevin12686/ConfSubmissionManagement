"""Transport cannot substitute PDFs, bypass bindings or duplicate uncertain work."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from submissions.services import comparison_client as client
from submissions.services.comparison_protocol import contract_id, job_key, page_render_hashes
from submissions.services.pdf_comparison import compare_pdf_bytes
from submissions.services.pdf_comparison_errors import ComparisonInProgress, PDFComparisonError
from submissions.tests.ocr_fixtures import ocr_pages
from submissions.services.rapidocr_comparison.result import build_result
from submissions.tests.test_pdf_comparison import make_pdf


class ComparisonClientTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(override_settings(
            BASE_DIR=self.root, CFM_COMPARISON_TRANSPORT="service",
            CFM_COMPARISON_REMOTE_URL="http://remote:8091", CFM_COMPARISON_REMOTE_TOKEN="r" * 32,
            CFM_COMPARISON_REMOTE_HEADERS="",
            CFM_COMPARISON_LOCAL_URL="http://local:8091", CFM_COMPARISON_LOCAL_TOKEN="l" * 32,
        ))
        self.before = make_pdf(["The results are in Table 2. These independent observations preserve scientific context."])
        self.after = make_pdf(["The results are in Error! Reference source not found. These observations preserve scientific context."])
        self.hashes = [hashlib.sha256(data).hexdigest() for data in (self.before, self.after)]
        self.contract = contract_id()
        self.key = job_key(self.hashes, self.contract)
        pa = ocr_pages("The results are in Table 2. These independent observations preserve scientific context.")
        pb = ocr_pages("The results are in Error! Reference source not found. These observations preserve scientific context.")
        self.result = build_result(pa, pb, [], [], identical_bytes=False)
        self.complete = {
            "protocol": 1, "contract": self.contract, "key": self.key,
            "hashes": self.hashes, "state": "completed", "result": self.result,
            "render_hashes": {"before": page_render_hashes(self.before), "after": page_render_hashes(self.after)},
            "diagnostics": {"pair_hit": True, "backend": "cpu"},
        }
        self.calls = []
        self.jobs = {}
        self.health = {}
        self.post_error = None
        self.transport = self.enterContext(patch.object(client.Endpoint, "call", autospec=True, side_effect=self.call))
        self.enterContext(patch.object(client.time, "sleep"))

    def call(self, endpoint, path, *, body=None, headers=None):
        self.calls.append((endpoint.name, path, body, headers))
        if path == "/v1/health":
            value = self.health.get(endpoint.name, "ready")
            if value == "offline":
                raise client.TransportUnavailable()
            return 200, {"protocol": 1, "contract": self.contract, "state": value, "backend": "cpu"}
        if body is not None:
            from submissions.services.pdf_comparison_worker import read_pair
            self.assertEqual(read_pair(io.BytesIO(body)), [self.before, self.after])
            self.assertEqual(headers["X-CFM-Before-SHA256"], self.hashes[0])
            self.jobs[endpoint.name] = copy.deepcopy(self.complete)
            if self.post_error:
                raise self.post_error
            return 202, self.jobs[endpoint.name]
        if self.health.get(endpoint.name) == "offline":
            raise client.TransportUnavailable()
        return (200, self.jobs[endpoint.name]) if endpoint.name in self.jobs else (404, {"error": "not_found"})

    def compare(self):
        diagnostics = {}
        result = compare_pdf_bytes(self.before, self.after, diagnostics=diagnostics)
        return result, diagnostics

    def test_remote_preferred_and_completed_job_reused(self):
        result, diagnostics = self.compare()
        self.assertEqual(result, self.result)
        self.assertEqual(diagnostics["worker"], "remote")
        self.assertEqual(self.compare()[0], result)
        self.assertEqual(sum(call[2] is not None for call in self.calls), 1)
        self.assertEqual({call[0] for call in self.calls}, {"remote"})

    def test_only_preacceptance_offline_or_not_ready_falls_back(self):
        for state in ("offline", "not_ready"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=Path(root)):
                self.health["remote"] = state
                self.calls.clear()
                self.assertEqual(self.compare()[1]["worker"], "local")
                self.assertFalse(any(c[0] == "remote" and c[2] is not None for c in self.calls))

    def test_busy_remote_is_not_offline(self):
        self.health["remote"] = "busy"
        self.compare()
        self.assertEqual({c[0] for c in self.calls}, {"remote"})

    def test_lost_submission_response_reconciles_same_job(self):
        self.post_error = client.TransportUnavailable()
        with self.assertRaisesRegex(ComparisonInProgress, "same job"):
            self.compare()
        self.post_error = None
        result, diagnostics = self.compare()
        self.assertEqual(result, self.result)
        self.assertTrue(diagnostics["resumed"])
        self.assertEqual(sum(c[2] is not None for c in self.calls), 1)
        self.health["remote"] = "offline"
        with self.assertRaises(ComparisonInProgress):
            self.compare()
        self.assertEqual({c[0] for c in self.calls}, {"remote"})

    def test_configuration_change_cannot_reroute_an_uncertain_job(self):
        self.compare()
        with override_settings(CFM_COMPARISON_REMOTE_TOKEN="x" * 32):
            with self.assertRaisesRegex(PDFComparisonError, "configuration changed"):
                self.compare()

    def test_gateway_credential_rotation_keeps_the_same_job_and_worker(self):
        self.compare()
        with override_settings(CFM_COMPARISON_REMOTE_HEADERS='{"X-API-Key":"new-secret"}'):
            self.assertTrue(self.compare()[1]["resumed"])
        self.assertEqual(sum(c[2] is not None for c in self.calls), 1)
        self.assertEqual({c[0] for c in self.calls}, {"remote"})

    def test_invalid_headers_do_not_select_local_or_submit(self):
        for raw in ('{"Authorization":"secret"}', '{"X-Key":"secret\\n"}', '[]'):
            with self.subTest(raw=raw), override_settings(CFM_COMPARISON_REMOTE_HEADERS=raw):
                with self.assertRaises(PDFComparisonError):
                    self.compare()
        self.assertEqual(self.calls, [])

    def test_custom_headers_are_remote_only_and_missing_url_is_rejected(self):
        with override_settings(CFM_COMPARISON_REMOTE_HEADERS='{"X-API-Key":"secret"}'):
            remote, local = client._endpoints()
            self.assertEqual(remote.custom_headers, {"X-API-Key": "secret"})
            self.assertEqual(local.custom_headers, {})
            with override_settings(CFM_COMPARISON_REMOTE_URL=""):
                with self.assertRaisesRegex(PDFComparisonError, "requires"):
                    client._endpoints()

    def test_route_lease_renews_on_same_endpoint_past_job_ttl(self):
        now = client.time.time()
        with patch.object(client.time, "time", return_value=now):
            self.compare()
        with patch.object(client.time, "time", return_value=now + 2 * 60 * 60 + 60):
            self.assertTrue(self.compare()[1]["resumed"])
        # Filesystem timestamps do not follow the mocked signing clock.
        path = self.root / "data" / "formatting_comparison_previews" / "compute" / f"{self.key}.json"
        client.os.utime(path, (now + 2 * 60 * 60 + 60,) * 2)
        self.health["remote"] = "offline"
        with patch.object(client.time, "time", return_value=now + client.ROUTE_SECONDS + 60):
            with self.assertRaises(ComparisonInProgress):
                self.compare()
        self.assertEqual({c[0] for c in self.calls}, {"remote"})

    def test_idle_route_lease_expires_after_all_possible_old_work(self):
        now = client.time.time()
        with patch.object(client.time, "time", return_value=now):
            self.compare()
        self.health["remote"] = "offline"
        with patch.object(client.time, "time", return_value=now + client.ROUTE_SECONDS + 1):
            self.assertEqual(self.compare()[1]["worker"], "local")

    def test_failed_ocr_never_falls_back_or_becomes_clean(self):
        self.jobs["remote"] = {**self.complete, "state": "failed", "error": "ocr_failed"}
        with self.assertRaisesRegex(PDFComparisonError, "could not complete"):
            self.compare()
        self.assertFalse(any(c[0] == "local" for c in self.calls))

    def test_only_explicit_retry_resubmits_failed_job(self):
        self.jobs["remote"] = {**self.complete, "state": "failed", "error": "comparison_failed"}
        with self.assertRaises(PDFComparisonError):
            self.compare()
        self.assertFalse(any(c[2] is not None for c in self.calls))
        result = client.service_pdf_evidence(self.before, self.after, retry_failed=True)
        self.assertEqual(result, self.result)
        posts = [c for c in self.calls if c[2] is not None]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3]["X-CFM-Retry-Failed"], "1")

    def test_wrong_pair_method_rendering_and_result_rejected(self):
        variants = [
            {"hashes": list(reversed(self.hashes))}, {"key": "f" * 64},
            {"contract": "f" * 64}, {"protocol": 2}, {"state": "unknown"},
            {"render_hashes": {"before": ["0" * 64], "after": ["0" * 64]}},
            {"result": {**self.result, "identical_bytes": True}}, {"result": {}},
        ]
        for changed in variants:
            with self.subTest(changed=next(iter(changed))):
                self.jobs["remote"] = {**copy.deepcopy(self.complete), **changed}
                with self.assertRaises(PDFComparisonError):
                    self.compare()

    def test_queue_wait_is_bounded_and_resumable(self):
        self.jobs["remote"] = {**self.complete, "state": "queued"}
        with patch.object(client, "WAIT_SECONDS", 0):
            with self.assertRaisesRegex(ComparisonInProgress, "queued or running"):
                self.compare()
        self.jobs["remote"] = self.complete
        self.assertTrue(self.compare()[1]["resumed"])

    def test_not_ready_is_distinct_from_invalid_worker_evidence(self):
        self.compare()
        for error in ("not_ready", "invalid_evidence", "incompatible_runtime"):
            expected = ComparisonInProgress if error == "not_ready" else PDFComparisonError
            with self.subTest(error=error), patch.object(client.Endpoint, "call", return_value=(503, {"error": error})):
                with self.assertRaises(expected) as raised:
                    self.compare()
                self.assertNotIn("busy", str(raised.exception))
                if error != "not_ready":
                    self.assertNotIsInstance(raised.exception, ComparisonInProgress)

    def test_missing_endpoints_does_not_silently_run_embedded(self):
        with override_settings(CFM_COMPARISON_LOCAL_URL="", CFM_COMPARISON_REMOTE_URL=""):
            with self.assertRaisesRegex(PDFComparisonError, "configure a shared local worker"):
                self.compare()

    def test_receipt_is_signed_and_contains_no_credentials_or_pdfs(self):
        self.compare()
        path = self.root / "data" / "formatting_comparison_previews" / "compute" / f"{self.key}.json"
        from django.core import signing
        payload = signing.loads(path.read_text(), salt=client.ROUTE_SALT)
        self.assertEqual(set(payload), {"key", "endpoint"})
        path.write_text("tampered")
        with self.assertRaisesRegex(PDFComparisonError, "Invalid compute routing receipt"):
            self.compare()

    def test_url_and_token_validation(self):
        for url in ("file:///tmp/test", "http://user:password@host", "http://host/path", "http://host/?token=bad"):
            with self.subTest(url=url), self.assertRaises(PDFComparisonError):
                client.Endpoint("remote", url, "a" * 32)
        with self.assertRaises(PDFComparisonError):
            client.Endpoint("remote", "http://host:8091", "short")


class ComparisonHTTPTransportTests(SimpleTestCase):
    def test_html_gateway_failures_are_transport_unavailable(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                endpoint = client.Endpoint("remote", "http://host:8091", "t" * 32)
                response = io.BytesIO(b"<html>Upstream unavailable</html>")
                response.status = code
                with patch.object(endpoint.opener, "open", return_value=response):
                    with self.assertRaises(client.TransportUnavailable):
                        endpoint.call("/v1/health")

    def test_redirects_credentials_bad_json_and_size_are_rejected(self):
        for code, body in ((302, b"{}"), (401, b"{}"), (200, b"bad"), (200, b"[]"), (200, b"x" * (client.MAX_REPLY_BYTES + 1))):
            with self.subTest(code=code, size=len(body)):
                endpoint = client.Endpoint("remote", "http://host:8091", "t" * 32)
                response = io.BytesIO(body)
                response.status = code
                with patch.object(endpoint.opener, "open", return_value=response):
                    with self.assertRaises(PDFComparisonError):
                        endpoint.call("/v1/health")
