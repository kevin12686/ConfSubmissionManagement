"""Observability must not become a routing, inference or secret-disclosure input."""
import json
import os
import unittest
from unittest.mock import Mock, patch

from comparison_slave import entrypoint, worker
from submissions.services import comparison_metadata as metadata
from submissions.services.comparison_client import probe_endpoint
from submissions.services.comparison_protocol import contract_id
from submissions.version import APP_VERSION


class ComparisonMetadataTests(unittest.TestCase):
    def test_effective_defaults_and_explicit_overrides_use_runtime_parsers(self):
        with patch.dict(os.environ, {"CFM_SLAVE_ROOT": "/isolated-worker"}, clear=True), \
                patch("os.cpu_count", return_value=8):
            self.assertEqual(metadata.execution_settings(), {"det": "small", "rec": "small", "parallel_pages": 2,
                "threads": 4, "timeout_seconds": 300, "gpu_idle_seconds": 0})
            with patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda"}):
                self.assertEqual(metadata.execution_settings()["parallel_pages"], 4)
                self.assertEqual(metadata.execution_settings()["gpu_idle_seconds"], 1800)
            with patch.dict(os.environ, {"CFM_RAPIDOCR_PAGES": "1", "CFM_RAPIDOCR_THREADS": "2",
                    "CFM_RAPIDOCR_DET_MODEL": "medium", "CFM_RAPIDOCR_REC_MODEL": "medium",
                    "CFM_RAPIDOCR_TIMEOUT": "3600", "CFM_OCR_BACKEND": "cuda", "CFM_GPU_MODEL_IDLE_SECONDS": "0"}):
                self.assertEqual(metadata.execution_settings(), {"det": "medium", "rec": "medium", "parallel_pages": 1,
                    "threads": 2, "timeout_seconds": 3600, "gpu_idle_seconds": 0})

    def test_invalid_settings_are_not_relabelled_as_defaults(self):
        for key, value in (("CFM_RAPIDOCR_PAGES", "9"), ("CFM_RAPIDOCR_THREADS", "secret"),
                           ("CFM_RAPIDOCR_DET_MODEL", "secret"), ("CFM_RAPIDOCR_REC_MODEL", "secret"),
                           ("CFM_RAPIDOCR_TIMEOUT", "29"), ("CFM_GPU_MODEL_IDLE_SECONDS", "86401")):
            with self.subTest(key=key), patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda", key: value}, clear=True):
                with self.assertRaises(ValueError):
                    metadata.execution_settings()

    def test_metadata_is_allowlisted_bounded_and_not_environment_dump(self):
        secret = "PRIVATE-CREDENTIAL-/private/files"
        payload = {"worker_version": APP_VERSION, "token": secret, "settings": {
            "det": "medium", "rec": "small", "threads": 4, "parallel_pages": 2,
            "timeout_seconds": 300, "gpu_idle_seconds": 1800, "headers": secret, "model_path": secret}}
        result = metadata.public_metadata(payload)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(set(result), {"worker_version", "settings"})
        self.assertEqual(len(result["settings"]), 6)
        for value in ([], {}, None, True, "<script>", "1.2.3\n", "1.2.3-" + "x" * 80):
            with self.subTest(value=value):
                self.assertEqual(metadata.public_metadata({"worker_version": value})["worker_version"], "")
        for value in ([], {}, None, True, "4", -1, 999999):
            result = metadata.public_metadata({"settings": {"det": value, "threads": value, "gpu_idle_seconds": value}})
            self.assertEqual(result["settings"], {})

    def test_missing_or_invalid_metadata_cannot_change_compatibility(self):
        contract = contract_id()
        endpoint = Mock()
        base = {"protocol": 1, "contract": contract, "state": "ready", "backend": "cpu"}
        for extra in ({}, {"worker_version": "0.0.1"}, {"worker_version": "bad", "settings": []}):
            endpoint.call.return_value = (200, {**base, **extra})
            self.assertEqual(probe_endpoint(endpoint, contract)["state"], "ready")
        endpoint.call.return_value = (200, {**base, "worker_version": APP_VERSION, "contract": "wrong"})
        result = probe_endpoint(endpoint, contract)
        self.assertEqual(result["state"], "incompatible")
        self.assertEqual(result["worker_version"], APP_VERSION)

    def test_cpu_preflight_observes_only_after_session_validation(self):
        model = Mock()
        events = []
        model._load_det_model.side_effect = lambda: events.append("det")
        model._load_rec_model.side_effect = lambda: events.append("rec")
        with patch("submissions.services.rapidocr_comparison.runtime.build_recognizer", return_value=model), \
                patch("submissions.services.rapidocr_comparison.cache.runtime_fingerprint", return_value="a" * 64), \
                patch.object(metadata, "execution_settings", side_effect=lambda: events.append("settings") or {"threads": 4}):
            self.assertEqual(worker.preflight()["settings"], {"threads": 4})
        self.assertEqual(events, ["det", "rec", "settings"])

    def test_gpu_resident_preflight_reports_same_runtime_settings_without_reloading(self):
        from comparison_slave.runner import Service
        service = Mock(resident=Mock())
        service.resident.probe.return_value = {"runtime": "a" * 64}
        with patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda", "CFM_RAPIDOCR_PAGES": "4",
                                    "CFM_RAPIDOCR_THREADS": "4", "CFM_GPU_MODEL_IDLE_SECONDS": "1800"}, clear=True), \
                patch("os.cpu_count", return_value=8):
            payload = Service.run_process(service, "preflight", "test")
        self.assertEqual(payload["settings"], {"det": "small", "rec": "small", "parallel_pages": 4,
                         "threads": 4, "timeout_seconds": 300, "gpu_idle_seconds": 1800})
        service.resident.probe.assert_called_once_with()
        service._run_isolated.assert_not_called()

    def test_status_output_does_not_initialize_or_load_models_or_expose_arbitrary_health(self):
        import io
        from contextlib import redirect_stdout
        with patch.object(entrypoint, "health_status", return_value={"state": "ready", "backend": "cuda",
                "worker_version": APP_VERSION, "settings": {"threads": 4, "token": "private-secret"}}), \
                patch.object(entrypoint, "preflight") as load, patch.object(entrypoint, "initialize") as initialize, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entrypoint.main(["--status"]), 0)
        self.assertIn(APP_VERSION, output.getvalue())
        self.assertIn('"threads": 4', output.getvalue())
        self.assertNotIn("private-secret", output.getvalue())
        load.assert_not_called()
        initialize.assert_not_called()

    def test_release_display_is_not_compute_contract_or_job_identity(self):
        before = contract_id()
        with patch.object(metadata, "APP_VERSION", "999.0.0"):
            self.assertEqual(metadata.release_metadata({})["worker_version"], "999.0.0")
            self.assertEqual(contract_id(), before)
