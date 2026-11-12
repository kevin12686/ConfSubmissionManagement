"""Private GPU lifecycle/IPC boundaries; fake inference is explicit, never a provider fallback."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from comparison_slave.resident import ResidentGPU
from submissions.services.rapidocr_comparison import config, resident_protocol as ipc
from submissions.services.rapidocr_comparison import resident_worker
from submissions.services.pdf_comparison_worker import inspect_pair
from submissions.services.rapidocr_comparison.result import build_result, VERSION
from submissions.tests.ocr_fixtures import ocr_pages


def document():
    pages = ocr_pages("Independent scientific evidence preserves reliable context and measured dose 23 units.")
    pages[0].update(pdf_width=595, pdf_height=842)
    return {"pages": pages, "native": [{"page": 1, "words": []}]}


class FakeActors:
    def __init__(self):
        self.instances = []
        self.metadata = {"runtime": "a" * 64, "backend": {"backend": "cuda"}}
        self.probes = 0
        self.effect = None

    def __call__(self, environment, guard_fd):
        actor = self.Actor(self)
        self.instances.append(actor)
        return actor

    def cold_probe(self, previous, environment, guard_fd, deadline, cancelled):
        self.probes += 1
        return copy.deepcopy(self.metadata)

    class Actor:
        def __init__(self, factory):
            self.factory = factory
            self.metadata = copy.deepcopy(factory.metadata)
            self.closed = False
            self.extractions = 0

        def start(self, deadline, cancelled):
            self.cancelled = cancelled

        def call(self, request, *, data=None, deadline, cancelled):
            if request["op"] == "probe":
                return copy.deepcopy(self.factory.metadata)
            self.extractions += 1
            if self.factory.effect:
                return self.factory.effect(request, data, cancelled)
            return {"runtime": self.metadata["runtime"], "sha256": hashlib.sha256(data).hexdigest(), "document": document()}

        def close(self):
            self.closed = True

        def alive(self):
            return not self.closed


class GPUResidencyTests(unittest.TestCase):
    def setUp(self):
        self.guard = self.enterContext(tempfile.TemporaryFile())
        self.clock = [100.0]
        self.factory = FakeActors()
        self.manager = ResidentGPU({"CFM_OCR_BACKEND": "cuda", "CFM_SLAVE_ROOT": "/worker-data",
            "SMS_SECRET_KEY": "never-forward", "CFM_COMPARISON_REMOTE_TOKEN": "never-forward"},
            self.guard.fileno(), 10, actor_type=self.factory, clock=lambda: self.clock[0])
        self.addCleanup(self.manager.close)
        self.meta = self.manager.probe()
        self.before, self.after = b"bound-before-pdf", b"bound-after-pdf"

    def body(self):
        return struct.pack(">QQ", len(self.before), len(self.after)) + self.before + self.after

    def client(self, environment):
        return ipc.ResidentClient(*(environment[key] for key in ipc.ENV_KEYS))

    def test_warm_reuse_isolated_tokens_and_exact_document_binding(self):
        with self.manager.job(self.body()) as env:
            old = self.client(env)
            self.assertEqual(old.extract(self.before), (document()["pages"], document()["native"]))
        with self.manager.job(self.body()) as env:
            self.assertNotEqual(old.token, env[ipc.ENV_KEYS[1]])
            with self.assertRaises((EOFError, ConnectionError)):
                old.extract(self.before)
            current = self.client(env)
            with self.assertRaises((EOFError, ConnectionError)):
                current.extract(b"a different conference PDF")
            self.assertEqual(current.extract(self.after)[0], document()["pages"])
        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(self.factory.instances[0].extractions, 2)
        self.assertNotIn("SMS_SECRET_KEY", self.manager.environment)
        self.assertNotIn("CFM_COMPARISON_REMOTE_TOKEN", self.manager.environment)

    def test_health_and_cache_probe_do_not_extend_idle_or_reload_cold_models(self):
        self.clock[0] = 109
        self.manager.probe()
        self.assertEqual(self.manager.last_used, 100)
        self.assertEqual(self.manager.status()["idle_remaining_seconds"], 1)
        self.clock[0] = 110
        self.manager.expire()
        self.assertTrue(self.factory.instances[0].closed)
        self.assertEqual(self.manager.status()["state"], "cold")
        self.manager.probe()
        with self.manager.job(self.body()) as env:
            self.client(env)
        self.assertEqual(len(self.factory.instances), 1)
        self.assertGreaterEqual(self.factory.probes, 2)
        with self.manager.job(self.body()) as env:
            self.client(env).extract(self.before)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertEqual(self.manager.last_used, 110)

    def test_idle_never_unloads_a_job_and_timer_starts_at_inference_completion(self):
        with self.manager.job(self.body()) as env:
            self.clock[0] = 999
            self.manager.expire()
            self.assertFalse(self.factory.instances[0].closed)
            self.client(env).extract(self.before)
            self.assertEqual(self.manager.last_used, 999)
        self.clock[0] = 1008
        self.manager.expire()
        self.assertFalse(self.factory.instances[0].closed)
        self.clock[0] = 1009
        self.manager.expire()
        self.assertTrue(self.factory.instances[0].closed)

    def test_actual_idle_monitor_unloads_without_request_or_health_poll(self):
        self.clock[0] = 111
        deadline = time.monotonic() + 2
        while self.manager.actor is not None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIsNone(self.manager.actor)

    def test_idle_actor_crash_is_detected_without_waiting_for_idle_timeout(self):
        self.factory.instances[0].closed = True
        self.manager.expire()
        self.assertIsNone(self.manager.actor)
        self.assertIsNone(self.manager.metadata)
        self.assertEqual(self.manager.status()["state"], "unavailable")

    def test_failed_or_wrong_identity_evidence_closes_actor_before_retry(self):
        for wrong in ("exception", "hash", "runtime", "document"):
            with self.subTest(wrong=wrong):
                self.manager.probe()
                def effect(request, data, cancelled):
                    if wrong == "exception":
                        raise ValueError("simulated inference failure")
                    response = {"runtime": self.meta["runtime"], "sha256": hashlib.sha256(data).hexdigest(), "document": document()}
                    if wrong == "hash":
                        response["sha256"] = "f" * 64
                    elif wrong == "runtime":
                        response["runtime"] = "f" * 64
                    else:
                        response["document"]["pages"] = []
                    return response
                self.factory.effect = effect
                actor = self.manager.actor
                with self.manager.job(self.body()) as env:
                    with self.assertRaises((EOFError, ConnectionError)):
                        self.client(env).extract(self.before)
                self.assertTrue(actor.closed)
                self.assertIsNone(self.manager.metadata)
                self.factory.effect = None

    def test_cancellation_unblocks_inference_and_shutdown_reaps_actor(self):
        entered = threading.Event()
        def block(request, data, cancelled):
            entered.set()
            while not cancelled():
                time.sleep(.01)
            raise TimeoutError("cancelled")
        self.factory.effect = block
        errors = []
        with self.manager.job(self.body()) as env:
            client = self.client(env)
            def extract():
                try:
                    client.extract(self.before)
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=extract)
            thread.start()
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            self.manager.invalidate()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(errors)
        self.assertTrue(self.factory.instances[0].closed)
        path = self.manager.socket_path.parent
        self.manager.close()
        self.assertFalse(path.exists())

    def test_expired_job_and_nested_job_fail_without_partial_success(self):
        with self.manager.job(self.body()) as env:
            with self.assertRaises(RuntimeError):
                with self.manager.job(self.body()):
                    pass
            self.manager.job_state.deadline = time.monotonic() - 1
            with self.assertRaises((EOFError, ConnectionError)):
                self.client(env).extract(self.before)

    def test_changed_runtime_reloads_and_cannot_relabel_current_job(self):
        with self.manager.job(self.body()) as env:
            self.factory.metadata["runtime"] = "b" * 64
            with self.assertRaises((EOFError, ConnectionError)):
                self.client(env)
        self.assertIsNone(self.manager.metadata)
        self.assertTrue(all(actor.closed for actor in self.factory.instances))
        self.assertEqual(self.manager.probe()["runtime"], "b" * 64)

    def test_unreaped_actor_blocks_replacement_and_is_not_reported_warm(self):
        actor = self.manager.actor
        with patch.object(actor, "close", side_effect=TimeoutError("not reaped")):
            with self.assertRaises(TimeoutError):
                self.manager.invalidate()
            self.assertIsNone(self.manager.metadata)
            self.assertEqual(self.manager.status()["state"], "unavailable")
            with self.assertRaises(TimeoutError):
                self.manager.probe()
            self.assertEqual(len(self.factory.instances), 1)


class GPUIPCAndConfigurationTests(unittest.TestCase):
    def test_warm_probe_rechecks_device_and_libraries_not_just_stored_identity(self):
        from submissions.services.rapidocr_comparison import cache, runtime
        backend = {"backend": "cuda", "device": {"uuid": "device"}, "libraries": {"nvidia": "1"}}
        with patch.object(runtime, "validate_models"), patch.object(runtime, "backend_identity", return_value=backend), \
                patch.object(runtime, "cuda_device_identity", return_value=backend["device"]) as device, \
                patch.object(runtime, "cuda_library_versions", return_value=backend["libraries"]) as libraries, \
                patch.object(cache, "runtime_fingerprint", return_value="a" * 64):
            self.assertEqual(resident_worker.metadata()["backend"], backend)
            libraries.return_value = {"nvidia": "2"}
            with self.assertRaises(ValueError):
                resident_worker.metadata()
            libraries.return_value = backend["libraries"]
            device.return_value = {"uuid": "changed"}
            with self.assertRaises(ValueError):
                resident_worker.metadata()

    def test_existing_health_endpoint_reports_models_without_loading_or_refreshing(self):
        from comparison_slave.runner import Service
        from comparison_slave.server import Application
        manager = Mock()
        manager.failed = False
        manager.status.return_value = {"state": "cold", "idle_seconds": 1800, "idle_remaining_seconds": None}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda", config.GPU_IDLE_ENV: "1800"}), \
                patch("comparison_slave.resident.ResidentGPU", return_value=manager):
            root = Path(temporary).resolve()
            clients = root / "clients.json"
            clients.write_text(json.dumps({"a": "a" * 32}))
            clients.chmod(0o600)
            service = Service(root, clients, start=False)
            try:
                service.ready, service.preflight_at = True, time.monotonic() + 60
                status, payload = Application(service).dispatch({"PATH_INFO": "/v1/health", "REQUEST_METHOD": "GET",
                    "HTTP_AUTHORIZATION": "Bearer " + "a" * 32})
                self.assertEqual((status, payload["state"]), (200, "ready"))
                self.assertEqual(payload["gpu_models"]["state"], "cold")
                manager.probe.assert_not_called()
                manager.failed = True
                manager.status.return_value["state"] = "unavailable"
                self.assertFalse(service.runtime_ready())
                self.assertEqual(Application(service).dispatch({"PATH_INFO": "/v1/health", "REQUEST_METHOD": "GET",
                    "HTTP_AUTHORIZATION": "Bearer " + "a" * 32})[1]["state"], "not_ready")
            finally:
                service.close()

    def test_gpu_failure_revalidates_before_retry_without_replaying_job(self):
        from comparison_slave.runner import Service
        from submissions.services.comparison_protocol import contract_id, job_key
        manager = Mock(failed=False)
        contract = contract_id()
        hashes = [hashlib.sha256(value).hexdigest() for value in (b"a", b"b")]
        key = job_key(hashes, contract)
        calls = []
        def process(mode, namespace, body=None):
            calls.append(mode)
            if mode == "preflight":
                manager.failed = False
                return {"runtime": "a" * 64, "backend": "cuda", "contract": contract}
            manager.failed = True
            return {"error": "comparison_failed"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda"}), \
                patch("comparison_slave.resident.ResidentGPU", return_value=manager):
            root = Path(directory).resolve()
            clients = root / "clients.json"
            clients.write_text(json.dumps({"a": "a" * 32}))
            clients.chmod(0o600)
            service = Service(root, clients, start=False, process=process)
            try:
                service.tick()
                service.ledger.submit("a", key, contract, hashes, b"validated test body")
                service.tick()
                self.assertFalse(service.runtime_ready())
                self.assertEqual(service.ledger.get("a", key)["state"], "failed")
                service.tick()
                self.assertTrue(service.runtime_ready())
                self.assertEqual(calls, ["preflight", "job", "preflight"])
                self.assertEqual(service.ledger.get("a", key)["state"], "failed")
            finally:
                service.close()

    def test_watchdog_terminates_a_native_call_without_python_signal_handler(self):
        if sys.platform == "win32":
            self.skipTest("GPU watchdog requires POSIX/Linux")
        command = [sys.executable, "-c", "import ctypes,time; from submissions.services.rapidocr_comparison.resident_worker import watchdog; watchdog(time.monotonic()+.15); ctypes.CDLL(None).sleep(10)"]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, timeout=3)
        import signal
        self.assertEqual(result.returncode, -signal.SIGALRM)
        self.assertLess(time.monotonic() - started, 3)

    def test_explicit_inference_interpreter_is_preserved_and_relative_rejected(self):
        from comparison_slave.resident import executable
        self.assertEqual(executable({}), sys.executable)
        self.assertEqual(executable({"CFM_RAPIDOCR_PYTHON": "/explicit/python"}), "/explicit/python")
        with self.assertRaises(ValueError):
            executable({"CFM_RAPIDOCR_PYTHON": "python"})

    def test_per_job_cpu_budget_resets_without_rebuilding_model_pool(self):
        from submissions.services.rapidocr_comparison import runtime
        meta = {"runtime": "a" * 64, "backend": {"backend": "cuda"}}
        left, right = socket.socketpair()
        errors = []
        def run():
            try:
                resident_worker.serve(right)
            except EOFError:
                pass
            except BaseException as exc:
                errors.append(exc)
        with left, right, patch.object(resident_worker, "metadata", return_value=meta), \
                patch.object(resident_worker, "watchdog"), \
                patch("submissions.services.pdf_comparison_worker.resource_limits") as limits, \
                patch.object(runtime, "build_recognizer") as build, \
                patch.object(runtime, "extract_document", return_value=(document()["pages"], document()["native"])), \
                patch.object(resident_worker.resource, "getrusage", side_effect=[Mock(ru_utime=5, ru_stime=0), Mock(ru_utime=10, ru_stime=0)]), \
                patch.object(resident_worker.resource, "setrlimit") as set_limit:
            thread = threading.Thread(target=run)
            thread.start()
            self.assertEqual(ipc.receive(left), meta)
            for job in ("first", "first", "second"):
                data = b"test-pdf"
                ipc.send(left, {"op": "extract", "runtime": meta["runtime"], "sha256": hashlib.sha256(data).hexdigest(), "job": job,
                    "deadline": time.monotonic() + 5})
                ipc.send(left, data, binary=True)
                self.assertEqual(ipc.receive(left)["document"], document())
            left.shutdown(socket.SHUT_RDWR)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            build.assert_called_once()
            limits.assert_called_once_with(persistent=True)
            self.assertEqual(set_limit.call_count, 2)
            self.assertEqual(set_limit.call_args_list[1].args[1][0] - set_limit.call_args_list[0].args[1][0], 5)

    def test_cpu_and_explicit_gpu_zero_do_not_create_a_resident_broker(self):
        from comparison_slave.runner import Service
        for backend, value in (("cpu", "invalid"), ("cuda", "0")):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"CFM_OCR_BACKEND": backend, config.GPU_IDLE_ENV: value}), \
                    patch("comparison_slave.resident.ResidentGPU") as create:
                root = Path(directory).resolve()
                clients = root / "clients.json"
                clients.write_text(json.dumps({"a": "a" * 32}))
                clients.chmod(0o600)
                service = Service(root, clients, start=False)
                try:
                    self.assertIsNone(service.resident)
                    create.assert_not_called()
                finally:
                    service.close()

    def test_health_sanitizes_gpu_model_state_without_remote_prose(self):
        from submissions.services.comparison_client import probe_endpoint
        endpoint = Mock()
        health = {"protocol": 1, "contract": "expected", "state": "ready", "backend": "cuda",
            "gpu_models": {"state": "cold", "idle_seconds": 1800}}
        endpoint.call.return_value = (200, health)
        self.assertIn("unloaded", probe_endpoint(endpoint, "expected")["message"])
        health["gpu_models"]["state"] = "untrusted remote prose"
        self.assertEqual(probe_endpoint(endpoint, "expected")["message"], "")
        health["gpu_models"] = {"state": "warm", "idle_seconds": "<script>"}
        self.assertEqual(probe_endpoint(endpoint, "expected")["message"], "")
        health["gpu_models"] = {"state": [], "idle_seconds": 1800}
        self.assertEqual(probe_endpoint(endpoint, "expected")["message"], "")

    def test_defaults_disabled_and_invalid_settings_do_not_change_cpu(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.gpu_idle_seconds(), 0)
            with patch.dict(os.environ, {config.GPU_IDLE_ENV: "invalid"}):
                self.assertEqual(config.gpu_idle_seconds(), 0)
            with patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda"}):
                self.assertEqual(config.gpu_idle_seconds(), 1800)
                for value in ("0", "1", "1800", "86400"):
                    with patch.dict(os.environ, {config.GPU_IDLE_ENV: value}):
                        self.assertEqual(config.gpu_idle_seconds(), int(value))
                for value in ("-1", "86401", "", "30m", "1.5"):
                    with patch.dict(os.environ, {config.GPU_IDLE_ENV: value}), self.assertRaises(ValueError):
                        config.gpu_idle_seconds()

    def test_ipc_bounds_closed_peers_and_cancelled_deadlines(self):
        for length in (0, ipc.MAX_META + 1):
            left, right = socket.socketpair()
            with left, right:
                left.sendall(struct.pack(">Q", length))
                with self.assertRaises(ValueError):
                    ipc.receive(right)
        left, right = socket.socketpair()
        with left, right:
            left.close()
            with self.assertRaises(EOFError):
                ipc.receive(right)
        left, right = socket.socketpair()
        with left, right:
            with self.assertRaises(TimeoutError):
                ipc.receive(right, deadline=time.monotonic() - 1)
            with self.assertRaises(TimeoutError):
                ipc.receive(right, cancelled=lambda: True)

    def test_partial_or_cpu_resident_environment_cannot_fall_back(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ipc.ResidentClient.from_environment())
            with patch.dict(os.environ, {ipc.ENV_KEYS[0]: "/tmp/missing"}):
                with self.assertRaises(ValueError):
                    ipc.ResidentClient.from_environment()

    def test_valid_pair_cache_does_not_request_inference_or_load_models(self):
        from submissions.services.rapidocr_comparison.cache import FORMAT, write_artifact, MAX_RESULT_BYTES
        before, after = b"before", b"after"
        doc = document()
        result = build_result(doc["pages"], doc["pages"], doc["native"], doc["native"])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            write_artifact(directory, "pair.input.json", {"format": FORMAT, "kind": "pair", "runtime": "a" * 64,
                "hashes": [hashlib.sha256(data).hexdigest() for data in (before, after)], "value": result}, MAX_RESULT_BYTES + 65536)
            with patch.object(ipc.ResidentClient, "from_environment") as create, \
                    patch("submissions.services.rapidocr_comparison.runtime.build_recognizer") as build:
                create.return_value.fingerprint = "a" * 64
                evidence = inspect_pair(before, after, directory, cache_only=True)
                self.assertEqual(evidence, {"version": VERSION, "result": result})
                build.assert_not_called()
                create.return_value.extract.assert_not_called()

    def test_cold_probe_validates_models_dependencies_device_and_libraries(self):
        from submissions.services.rapidocr_comparison import runtime, cache
        backend = {"backend": "cuda", "device": {"uuid": "real-device"}, "libraries": {"nvidia": "version"},
                   "sessions": {k: {"providers": ["CUDAExecutionProvider"]} for k in ("det", "rec")}}
        with patch.object(runtime, "validate_models") as models, patch.object(runtime, "validate_dependencies") as deps, \
                patch.object(runtime, "ocr_backend", return_value="cuda"), \
                patch.object(runtime, "cuda_device_identity", return_value=backend["device"]) as device, \
                patch.object(runtime, "cuda_library_versions", return_value=backend["libraries"]), \
                patch.object(runtime, "build_recognizer") as build, \
                patch.object(cache, "runtime_fingerprint", return_value="a" * 64) as fingerprint:
            self.assertEqual(resident_worker.cold_probe({"backend": backend})["runtime"], "a" * 64)
            models.assert_called_once()
            deps.assert_called_once()
            fingerprint.assert_called_once_with(backend_identity=backend)
            build.assert_not_called()
            device.return_value = {"uuid": "a-different-device"}
            with self.assertRaises(ValueError):
                resident_worker.cold_probe({"backend": backend})
