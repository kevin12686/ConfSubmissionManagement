"""Standalone worker contract tests: temporary storage, synthetic PDFs, no models/DB."""

import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

import fitz

from comparison_slave.runner import Service
from comparison_slave.server import Application
from comparison_slave.storage import MAX_PDF, MAX_RESPONSE, clients_from_file, namespace_path, private_directory
from comparison_slave import worker
from submissions.services import comparison_protocol as protocol
from submissions.services.pdf_comparison_errors import ComparisonInProgress, PDFComparisonError
from submissions.services.rapidocr_comparison.result import build_result
from submissions.tests.ocr_fixtures import ocr_pages


def pdf(text):
    with fitz.open() as document:
        page = document.new_page(width=100, height=100)
        page.insert_text((10, 20), text, fontsize=8)
        return document.tobytes()


def private_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class ComparisonSlaveTests(unittest.TestCase):
    def test_verified_runtime_change_invalidates_only_obsolete_completed_cache(self):
        self.service.ledger.submit("conference-a", self.key, self.contract, self.hashes, b"request")
        row = self.service.ledger.claim()
        old = {**self.evidence, "runtime": self.service.runtime}
        self.service.ledger.finish(row, output=json.dumps(old).encode())
        self.service.ledger.invalidate_runtime(self.contract, self.service.runtime)
        self.assertEqual(self.service.ledger.get("conference-a", self.key)["state"], "completed")
        self.service.ledger.invalidate_runtime(self.contract, "d" * 64)
        record = self.service.ledger.get("conference-a", self.key)
        self.assertEqual((record["state"], record["error"], record["output"]), ("failed", "configuration_changed", None))
        self.assertFalse(self.service.ledger.submit("conference-a", self.key, self.contract, self.hashes, b"request"))
        self.assertTrue(self.service.ledger.submit("conference-a", self.key, self.contract, self.hashes, b"request", retry_failed=True))

    def test_failed_runtime_invalidation_is_retried_before_serving_completed_cache(self):
        self.complete()
        previous = self.service.ledger.get("conference-a", self.key)
        self.service.preflight_at = 0
        self.service.probe_requested.set()
        self.compute.reset_mock()
        self.compute.side_effect = lambda *args: {
            "contract": self.contract, "backend": "cpu", "runtime": "d" * 64,
        }
        invalidate = self.service.ledger.invalidate_runtime
        attempts = []

        def fail_once(contract, runtime):
            attempts.append(runtime)
            if len(attempts) == 1:
                raise OSError("temporary ledger failure")
            return invalidate(contract, runtime)

        with patch.object(self.service.ledger, "invalidate_runtime", side_effect=fail_once):
            with self.assertLogs("comparison_slave.runner", level="WARNING"):
                self.service.tick()
            self.assertFalse(self.service.ready)
            self.assertEqual(self.request()[0], 503)
            self.assertEqual(self.service.ledger.get("conference-a", self.key), previous)
            self.service.preflight_at = 0
            self.service.probe_requested.set()
            self.service.tick()

        self.assertEqual(attempts, ["d" * 64, "d" * 64])
        self.assertTrue(self.service.ready)
        record = self.service.ledger.get("conference-a", self.key)
        self.assertEqual((record["state"], record["error"], record["output"]),
                         ("failed", "configuration_changed", None))
        self.assertEqual(record["created"], previous["created"])
        self.assertTrue(all(call.args[0] == "preflight" for call in self.compute.call_args_list))
        self.assertEqual(self.request("POST")[1]["state"], "failed")
        self.assertEqual(self.request("POST", headers={"HTTP_X_CFM_RETRY_FAILED": "1"})[0], 202)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "worker"
        self.clients_path = self.base / "clients.json"
        self.tokens = {"conference-a": "a" * 40, "conference-b": "b" * 40}
        private_file(self.clients_path, json.dumps(self.tokens).encode())
        self.before, self.after = pdf("Before"), pdf("After")
        self.contract = protocol.contract_id()
        self.hashes = [hashlib.sha256(v).hexdigest() for v in (self.before, self.after)]
        self.key = protocol.job_key(self.hashes, self.contract)
        pages = ocr_pages("Independent scientific observations preserve reliable context and measured dose 23 units.")
        result = build_result(pages, copy.deepcopy(pages), identical_bytes=False)
        self.evidence = {"result": result, "diagnostics": {"runtime": "c" * 64},
                         "render_hashes": {"before": protocol.page_render_hashes(self.before),
                                           "after": protocol.page_render_hashes(self.after)}}
        self.compute = Mock(side_effect=self.fake_process)
        self.service = Service(self.root, self.clients_path, start=False, process=self.compute)
        self.addCleanup(lambda: self.service.close())
        self.service.tick()
        self.app = Application(self.service)

    def fake_process(self, mode, namespace, body=None):
        if mode == "preflight":
            return {"contract": self.contract, "backend": "cpu", "runtime": "c" * 64}
        self.assertEqual(worker.read_pair(io.BytesIO(body)), [self.before, self.after])
        return copy.deepcopy(self.evidence)

    def body(self):
        return struct.pack(">QQ", len(self.before), len(self.after)) + self.before + self.after

    def request(self, method="GET", path=None, *, token=None, body=None, headers=None):
        data = self.body() if body is None else body
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path or f"/v1/jobs/{self.key}",
                   "HTTP_AUTHORIZATION": "Bearer " + (self.tokens["conference-a"] if token is None else token),
                   "HTTP_X_CFM_CONTRACT": self.contract, "HTTP_X_CFM_BEFORE_SHA256": self.hashes[0],
                   "HTTP_X_CFM_AFTER_SHA256": self.hashes[1], "CONTENT_LENGTH": str(len(data)),
                   "CONTENT_TYPE": "application/octet-stream", "wsgi.input": io.BytesIO(data)}
        environ.update(headers or {})
        response = []
        encoded = b"".join(self.app(environ, lambda status, hdrs: response.append((status, hdrs))))
        self.assertLessEqual(len(encoded), MAX_RESPONSE)
        self.assertEqual(dict(response[0][1])["Cache-Control"], "no-store")
        return int(response[0][0].split()[0]), json.loads(encoded)

    def complete(self):
        self.assertEqual(self.request("POST")[0], 202)
        self.service.tick()
        return self.request()

    def test_authentication_and_namespace_isolation(self):
        for token in ("", "wrong", "x" * 1000):
            status, payload = self.request(token=token)
            self.assertEqual(status, 401)
            self.assertEqual(payload["error"], "unauthorized")
        self.assertEqual(self.request("POST")[0], 202)
        self.assertEqual(self.request(token=self.tokens["conference-b"])[0], 404)
        self.assertEqual(self.request("POST", token=self.tokens["conference-b"])[0], 202)
        self.assertNotEqual(namespace_path(self.root, "conference-a"), namespace_path(self.root, "conference-b"))

    def test_post_is_idempotent_and_never_computes_in_request(self):
        self.compute.reset_mock()
        self.assertEqual(self.request("POST")[0], 202)
        self.assertEqual(self.request("POST")[0], 202)
        self.compute.assert_not_called()
        self.service.tick()
        self.assertEqual(self.compute.call_count, 1)
        status, payload = self.request("POST")
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "completed")
        self.assertEqual(payload["result"], self.evidence["result"])
        self.assertEqual(payload["render_hashes"], self.evidence["render_hashes"])
        self.assertEqual(payload["hashes"], self.hashes)
        with self.service.ledger.transaction() as db:
            row = db.execute("SELECT input,attempts FROM jobs").fetchone()
            self.assertIsNone(row["input"])
            self.assertEqual(row["attempts"], 1)

    def test_failure_is_safe_and_terminal_without_explicit_retry(self):
        self.compute.side_effect = RuntimeError("private token and PDF contents")
        self.assertEqual(self.request("POST")[0], 202)
        with self.assertLogs("comparison_slave.runner", level="WARNING") as logs:
            self.service.tick()
        self.assertNotIn("private token", " ".join(logs.output))
        self.assertEqual(self.request()[1]["state"], "failed")
        self.assertEqual(self.request("POST")[1]["attempts"], 1)
        self.service.tick()
        self.assertEqual(self.compute.call_count, 2)  # startup preflight plus one job

    def test_explicit_retry_only_failed_and_maximum_three_attempts(self):
        self.compute.side_effect = lambda *args: {"error": "comparison_failed"}
        self.request("POST")
        for attempt in (1, 2, 3):
            self.service.tick()
            self.assertEqual(self.request()[1]["attempts"], attempt)
            status, reply = self.request("POST", headers={"HTTP_X_CFM_RETRY_FAILED": "1"})
            if attempt < 3:
                self.assertEqual(status, 202)
                self.assertEqual(reply["attempts"], attempt + 1)
                self.assertEqual(self.request("POST", headers={"HTTP_X_CFM_RETRY_FAILED": "1"})[1]["attempts"], attempt + 1)
            else:
                self.assertEqual((status, reply["error"]), (409, "retry_limit"))

    def test_retry_never_requeues_completed_or_running(self):
        self.complete()
        status, reply = self.request("POST", headers={"HTTP_X_CFM_RETRY_FAILED": "1"})
        self.assertEqual((status, reply["state"], reply["attempts"]), (200, "completed", 1))

    def test_concurrent_retries_are_atomic(self):
        self.request("POST")
        row = self.service.ledger.claim()
        self.service.ledger.finish(row)
        barrier = threading.Barrier(3)
        results = []
        def retry():
            barrier.wait()
            results.append(self.service.ledger.submit("conference-a", self.key, self.contract, self.hashes,
                                                       self.body(), retry_failed=True))
        threads = [threading.Thread(target=retry) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.request()[1]["attempts"], 2)

    def test_restart_marks_running_failed_and_preserves_queued(self):
        self.request("POST")
        self.request("POST", token=self.tokens["conference-b"])
        self.service.ledger.claim()
        self.service.close()
        self.service = Service(self.root, self.clients_path, start=False, process=self.compute)
        self.app = Application(self.service)
        self.assertEqual(self.request()[1]["error"], "interrupted")
        self.assertEqual(self.request(token=self.tokens["conference-b"])[1]["state"], "queued")

    def test_second_process_service_for_root_is_rejected(self):
        with self.assertRaises(BlockingIOError):
            Service(self.root, self.clients_path, start=False, process=self.compute)

    def test_fair_queue_and_per_client_capacity(self):
        ledger = self.service.ledger
        for index in range(4):
            hashes = [hashlib.sha256(str(index).encode()).hexdigest(), self.hashes[1]]
            ledger.submit("conference-a", protocol.job_key(hashes, self.contract), self.contract, hashes, self.body())
        self.assertEqual(self.request("POST")[0], 429)
        self.assertEqual(self.request("POST", token=self.tokens["conference-b"])[0], 202)
        first = ledger.claim()
        ledger.finish(first)
        second = ledger.claim()
        self.assertEqual([first["namespace"], second["namespace"]], ["conference-a", "conference-b"])

    def test_total_queue_disk_and_retained_job_limits(self):
        for attribute, value in (("max_pending", 0), ("max_bytes", 1), ("max_jobs", 0)):
            with self.subTest(attribute=attribute), patch.object(self.service.ledger, attribute, value):
                self.assertEqual(self.request("POST")[0], 429)
        self.assertEqual(self.request()[0], 404)

    def test_ttl_prunes_inputs_outputs_and_transition_events(self):
        self.complete()
        with self.service.ledger.transaction() as db:
            db.execute("UPDATE jobs SET created=?", (time.time() - 7201,))
        self.assertEqual(self.request()[0], 404)
        self.service.ledger.prune()
        with self.service.ledger.transaction() as db:
            for table in ("jobs", "events", "clients"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_running_job_past_ttl_remains_pollable_and_post_is_idempotent(self):
        self.request("POST")
        running = self.service.ledger.claim()
        with self.service.ledger.transaction() as db:
            db.execute("UPDATE jobs SET created=?", (time.time() - 7201,))
        self.service.ledger.prune()
        self.service.ready = False
        for headers in ({}, {"HTTP_X_CFM_RETRY_FAILED": "1"}):
            status, reply = self.request("POST", headers=headers)
            self.assertEqual((status, reply["state"], reply["attempts"]), (202, "running", 1))
        self.assertEqual(self.request()[1]["state"], "running")
        self.assertIsNone(self.service.ledger.claim())
        self.service.ledger.finish(running)
        self.assertEqual(self.request()[0], 404)
        self.service.ledger.prune()
        self.assertIsNone(self.service.ledger.get("conference-a", self.key))

    def test_expired_queued_job_is_never_dispatched_between_cleanup_ticks(self):
        self.request("POST")
        with self.service.ledger.transaction() as db:
            db.execute("UPDATE jobs SET created=?", (time.time() - 7201,))
        self.assertIsNone(self.service.ledger.claim())
        self.assertEqual(self.request()[0], 404)

    def test_explicit_failed_retry_preserves_original_ttl(self):
        self.request("POST")
        running = self.service.ledger.claim()
        self.service.ledger.finish(running)
        created = time.time() - 7190
        with self.service.ledger.transaction() as db:
            db.execute("UPDATE jobs SET created=?", (created,))
        self.assertEqual(self.request("POST", headers={"HTTP_X_CFM_RETRY_FAILED": "1"})[1]["attempts"], 2)
        row = self.service.ledger.get("conference-a", self.key)
        self.assertEqual(row["created"], created)
        self.assertTrue(self.service.ledger.expired(row, now=created + 7201))

    def test_malformed_bounds_hashes_contract_and_keys(self):
        cases = [({"CONTENT_LENGTH": ""}, 411), ({"CONTENT_LENGTH": "-2"}, 400),
                 ({"CONTENT_LENGTH": str(16 + 2 * MAX_PDF + 1)}, 413),
                 ({"HTTP_X_CFM_CONTRACT": "wrong"}, 409),
                 ({"HTTP_X_CFM_BEFORE_SHA256": "../"}, 400),
                 ({"HTTP_TRANSFER_ENCODING": "chunked"}, 400),
                 ({"CONTENT_TYPE": "text/plain"}, 415), ({"HTTP_X_CFM_RETRY_FAILED": "yes"}, 400)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", headers=headers)[0], expected)
        for body in (self.body()[:-1], self.body() + b"x", struct.pack(">QQ", 0, 2) + b"xx",
                     struct.pack(">QQ", MAX_PDF + 1, 1) + b"xx"):
            self.assertEqual(self.request("POST", body=body)[0], 400)
        changed = self.body()[:-1] + b"!"
        self.assertEqual(self.request("POST", body=changed)[1]["error"], "hash_mismatch")
        self.assertEqual(self.request("POST", path="/v1/jobs/" + "0" * 64)[1]["error"], "invalid_key")
        self.assertEqual(self.request(path="/v1/jobs/../../secret")[0], 404)

    def test_health_is_fast_async_and_idle_preflight_does_not_repeat(self):
        self.compute.reset_mock()
        self.service.preflight_at = 0
        self.service.tick()
        self.compute.assert_not_called()
        status, payload = self.request(path="/v1/health")
        self.assertEqual((status, payload["state"]), (200, "not_ready"))
        self.compute.assert_not_called()
        self.service.tick()
        self.assertEqual(self.compute.call_count, 1)
        self.assertEqual(self.request(path="/v1/health")[1]["state"], "ready")
        self.request("POST")
        self.assertEqual(self.request(path="/v1/health")[1]["state"], "busy")

    def test_preflight_failure_is_safe_cached_and_does_not_accept_work(self):
        self.service.preflight_at = 0
        self.service.probe_requested.set()
        self.compute.side_effect = RuntimeError("secret model path")
        self.service.tick()
        calls = self.compute.call_count
        for _ in range(3):
            payload = self.request(path="/v1/health")[1]
            self.assertEqual(payload["error"], "configuration_unavailable")
            self.service.tick()
        self.assertEqual(self.compute.call_count, calls)
        self.assertEqual(self.request("POST")[0], 503)

    def test_queued_work_does_not_hide_failed_readiness_but_running_work_is_busy(self):
        self.request("POST")
        self.service.ready = False
        self.service.health_error = "configuration_unavailable"
        health = self.request(path="/v1/health")[1]
        self.assertEqual((health["state"], health["error"]), ("not_ready", "configuration_unavailable"))
        self.assertEqual(self.request()[1]["state"], "queued")
        self.assertEqual(self.request("POST")[1]["state"], "queued")
        self.service.ledger.claim()
        health = self.request(path="/v1/health")[1]
        self.assertEqual((health["state"], health["error"]), ("busy", ""))

    def test_active_execution_is_busy_even_with_expired_readiness(self):
        self.service.active = True
        self.service.preflight_at = 0
        self.assertEqual(self.request(path="/v1/health")[1]["state"], "busy")

    def test_long_success_refreshes_readiness(self):
        def slow_success(mode, namespace, body=None):
            self.service.preflight_at = time.monotonic() - 1
            return copy.deepcopy(self.evidence)
        self.compute.side_effect = slow_success
        self.assertEqual(self.complete()[1]["state"], "completed")
        self.assertGreater(self.service.preflight_at, time.monotonic())

    def test_stale_or_changed_runtime_never_serves_completed_evidence(self):
        self.complete()
        self.service.runtime = "d" * 64
        self.assertEqual(self.request()[1]["error"], "incompatible_runtime")
        self.service.preflight_at = 0
        self.assertEqual(self.request()[1]["error"], "not_ready")

    def test_invalid_or_oversized_worker_output_is_failed(self):
        for change in ({"render_hashes": {"before": [], "after": []}}, {"diagnostics": {"huge": "x" * MAX_RESPONSE}},
                       {"result": {}}, {"diagnostics": {"runtime": "d" * 64}}):
            with self.subTest(change=list(change)):
                with self.service.ledger.transaction() as db:
                    db.execute("DELETE FROM jobs")
                evidence = copy.deepcopy(self.evidence)
                evidence.update(change)
                self.compute.side_effect = lambda *args: evidence
                self.request("POST")
                self.service.tick()
                self.assertEqual(self.request()[1]["state"], "failed")

    def test_signing_secret_is_persistent_private_and_namespace_bound(self):
        path = self.service.secret_file
        secret = path.read_bytes()
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.service.close()
        self.service = Service(self.root, self.clients_path, start=False, process=self.compute)
        self.assertEqual(self.service.secret_file.read_bytes(), secret)

    def test_private_config_rejects_symlinks_duplicates_and_shared_tokens(self):
        link = self.base / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            private_directory(link)
        for value in ({"a": "short"}, {"a": "a" * 40, "b": "a" * 40}, {"../a": "a" * 40}):
            private_file(self.clients_path, json.dumps(value).encode())
            with self.assertRaises(ValueError):
                clients_from_file(self.clients_path)
        private_file(self.clients_path, ('{"a":"' + "a" * 40 + '","a":"' + "b" * 40 + '"}').encode())
        with self.assertRaises(ValueError):
            clients_from_file(self.clients_path)

    def test_expired_dormant_cache_cleanup_and_orphan_temporary_cleanup(self):
        namespace = namespace_path(self.root, "conference-b")
        entries = private_directory(namespace / "data" / "pdf_comparison_cache" / "entries")
        expired = entries / ("d-" + "e" * 64 + ".json")
        private_file(expired, b"old")
        os.utime(expired, (time.time() - 8 * 86400,) * 2)
        outside = self.base / "keep"
        private_file(outside, b"keep")
        (entries / ("d-" + "f" * 64 + ".json")).symlink_to(outside)
        temporary = private_directory(namespace / "tmp" / "cfm-ocr-job-orphan")
        private_file(temporary / "runtime.json", b"{}")
        native_scratch = namespace / "tmp" / "native-library-scratch"
        private_file(native_scratch, b"scratch")
        (namespace / "tmp" / "scratch-link").symlink_to(outside)
        self.service.maintenance_at = 0
        self.service.tick()
        self.assertFalse(expired.exists())
        self.assertFalse(temporary.exists())
        self.assertFalse(native_scratch.exists())
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_dormant_cache_limits_and_interrupted_cache_writes_are_pruned(self):
        namespace = namespace_path(self.root, "conference-a")
        entries = private_directory(namespace / "data" / "pdf_comparison_cache" / "entries")
        for letter in ("a", "b", "c"):
            private_file(entries / ("d-" + letter * 64 + ".json"), b"1234")
        temporary = entries / ("d" * 32 + ".tmp")
        private_file(temporary, b"interrupted write")
        with patch("comparison_slave.runner.CACHE_BYTES", 6):
            self.service.prune_caches()
        self.assertFalse(temporary.exists())
        self.assertLessEqual(sum(path.stat().st_size for path in entries.iterdir()), 6)

    def test_worker_uses_shared_execution_and_safe_error_mapping(self):
        with patch("submissions.services.pdf_comparison_execution.local_pdf_evidence", return_value=self.evidence["result"]) as compute:
            result = worker.execute(self.before, self.after)
            self.assertEqual(result["render_hashes"], self.evidence["render_hashes"])
            compute.assert_called_once_with(self.before, self.after, diagnostics={})
        for error, expected in ((ComparisonInProgress("private"), "comparison_in_progress"),
                                (PDFComparisonError("private"), "comparison_failed")):
            with patch("submissions.services.pdf_comparison_execution.local_pdf_evidence", side_effect=error):
                self.assertEqual(worker.execute(self.before, self.after), {"error": expected})

    def test_standalone_import_and_minimal_settings_load_no_application_models(self):
        code = """
import sys
from pathlib import Path
from comparison_slave.server import application
from comparison_slave.worker import configure
configure(Path(sys.argv[1]), Path(sys.argv[2]))
from submissions.services.pdf_comparison_execution import local_pdf_evidence
from django.conf import settings
assert settings.INSTALLED_APPS == [] and settings.DATABASES == {}
assert not any(n == 'submissions.models' or n.startswith('submissions.models.') for n in sys.modules)
assert 'conference_final_manager.settings' not in sys.modules
assert 'rapidocr' not in sys.modules and 'onnxruntime' not in sys.modules
print(settings.SECRET_KEY)
"""
        keys = []
        for namespace in self.tokens:
            command = [sys.executable, "-c", code, str(namespace_path(self.root, namespace)), str(self.service.secret_file)]
            result = subprocess.run(command, capture_output=True, timeout=20, check=True)
            keys.append(result.stdout)
        self.assertNotEqual(*keys)

    def test_real_http_wsgi_auth_isolation_idempotency_and_completion(self):
        server = make_server("127.0.0.1", 0, self.app, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            def send(method, path, token, body=None, extra=None):
                headers = {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream",
                           "X-CFM-Contract": self.contract, "X-CFM-Before-SHA256": self.hashes[0],
                           "X-CFM-After-SHA256": self.hashes[1]}
                headers.update(extra or {})
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read(MAX_RESPONSE + 1))
            path = f"/v1/jobs/{self.key}"
            self.assertEqual(send("GET", "/v1/health", "bad")[0], 401)
            for _ in range(2):
                self.assertEqual(send("POST", path, self.tokens["conference-a"], self.body())[0], 202)
            self.assertEqual(send("GET", path, self.tokens["conference-b"])[0], 404)
            self.service.tick()
            status, payload = send("GET", path, self.tokens["conference-a"])
            self.assertEqual((status, payload["state"]), (200, "completed"))
            self.assertEqual(payload["render_hashes"], self.evidence["render_hashes"])
            second_token = self.tokens["conference-b"]
            with patch.object(self.service.ledger, "max_pending", 0):
                self.assertEqual(send("POST", path, second_token, self.body())[0], 429)
            self.assertEqual(send("POST", path, second_token, self.body(),
                                  {"Content-Length": str(2 * MAX_PDF + 17)})[0], 413)
            self.assertEqual(send("POST", path, second_token, self.body())[0], 202)
            self.compute.side_effect = ValueError("private details")
            self.service.tick()
            self.assertEqual(send("GET", path, second_token)[1]["state"], "failed")
            self.assertEqual(send("POST", path, second_token, self.body())[1]["state"], "failed")
            self.assertEqual(send("POST", path, second_token, self.body(), {"X-CFM-Retry-Failed": "1"})[0], 202)
            self.service.ledger.claim()
            self.service.close()
            self.service = Service(self.root, self.clients_path, start=False, process=self.compute)
            self.app = Application(self.service)
            server.set_app(self.app)
            self.assertEqual(send("GET", path, second_token)[1]["error"], "interrupted")
            connection.close()
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_scheduler_serializes_compute_while_http_remains_responsive(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(mode, namespace, body=None):
            entered.set()
            release.wait(5)
            return copy.deepcopy(self.evidence)
        self.compute.reset_mock()
        self.compute.side_effect = blocking
        self.service.thread = threading.Thread(target=self.service.loop, daemon=True)
        self.service.thread.start()
        try:
            self.assertEqual(self.request("POST")[0], 202)
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            self.assertEqual(self.request(path="/v1/health")[1]["state"], "busy")
            self.assertEqual(self.request()[1]["state"], "running")
            self.assertEqual(self.request("POST", headers={"HTTP_X_CFM_RETRY_FAILED": "1"})[1]["attempts"], 1)
            self.assertEqual(self.request("POST", token=self.tokens["conference-b"])[0], 202)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(self.compute.call_count, 1)
        finally:
            release.set()
            self.service.close()

    def test_process_timeout_kills_group_and_has_finite_reap_deadline(self):
        child = Mock(pid=123456789, returncode=0)
        child.wait.side_effect = [subprocess.TimeoutExpired("worker", 270), 0]
        with patch("comparison_slave.runner.subprocess.Popen", return_value=child) as popen, \
                patch("comparison_slave.runner.os.killpg") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.service.run_process("job", "conference-a", self.body())
        self.assertEqual(child.wait.call_args_list[0].kwargs["timeout"], 330)
        self.assertEqual(child.wait.call_args_list[1].kwargs["timeout"], 5)
        kill.assert_called_once_with(child.pid, signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (self.service.guard.fileno(),))
        self.assertEqual(popen.call_args.kwargs["env"]["CFM_SLAVE_ROOT"], str(self.root))
        self.assertNotIn("DJANGO_SETTINGS_MODULE", popen.call_args.kwargs["env"])

    def test_actual_isolated_preflight_fails_safely_without_models(self):
        missing = self.base / "missing-models"
        with patch.dict(os.environ, {"CFM_RAPIDOCR_MODEL_DIR": str(missing)}):
            result = self.service.run_process("preflight", "conference-a")
        self.assertEqual(result, {"error": "comparison_failed"})
        self.service.clean_temporary_jobs()


if __name__ == "__main__":
    unittest.main()
