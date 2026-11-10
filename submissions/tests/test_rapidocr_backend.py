"""Provisioning and worker execution boundaries, independent of recognition quality."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from submissions.services import pdf_comparison, pdf_comparison_worker
from submissions.services.rapidocr_comparison import runtime
from submissions.services.rapidocr_comparison.result import build_result
from submissions.tests.ocr_fixtures import ocr_pages


class OCRProvisioningTests(SimpleTestCase):
    def test_thread_configuration_is_bounded_by_runtime_capacity(self):
        with patch.object(runtime.os, "cpu_count", return_value=8):
            for value in ("1", "4", "6", "8"):
                with self.subTest(value=value), patch.dict(os.environ, {runtime.THREAD_ENV: value}):
                    self.assertEqual(runtime.ocr_thread_count(), int(value))
            for value in ("0", "-1", "9", "6.5", "auto", ""):
                with self.subTest(value=value), patch.dict(os.environ, {runtime.THREAD_ENV: value}):
                    with self.assertRaises(ValueError):
                        runtime.ocr_thread_count()
        with patch.object(runtime.os, "cpu_count", return_value=4), patch.dict(os.environ, {runtime.THREAD_ENV: "8"}):
            with self.assertRaises(ValueError):
                runtime.ocr_thread_count()
        with patch.object(runtime.os, "cpu_count", return_value=2), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime.ocr_thread_count(), 2)

    def test_models_are_explicit_and_hash_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "model.onnx"
            data = b"local model test fixture"
            path.write_bytes(data)
            with patch.dict(runtime.MODEL_HASHES, {path.name: hashlib.sha256(data).hexdigest()}, clear=True):
                self.assertEqual(runtime.validate_models(root), root)
                path.write_bytes(b"different bytes")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    runtime.validate_models(root)

    def test_missing_relative_and_symlink_models_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "absolute"):
                runtime.validate_models()
        with self.assertRaisesRegex(ValueError, "absolute"):
            runtime.validate_models("relative")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "missing"):
                runtime.validate_models(root)
            (root / "data").write_bytes(b"model")
            (root / "model.onnx").symlink_to(root / "data")
            with patch.dict(runtime.MODEL_HASHES, {"model.onnx": hashlib.sha256(b"model").hexdigest()}, clear=True):
                with self.assertRaisesRegex(ValueError, "invalid"):
                    runtime.validate_models(root)

    def test_prepare_defaults_to_durable_runtime_and_never_accepts_bad_models(self):
        with tempfile.TemporaryDirectory() as temporary, override_settings(BASE_DIR=temporary):
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            data = b"approved fixture"
            (source / "model.onnx").write_bytes(data)
            with patch.dict(runtime.MODEL_HASHES, {"model.onnx": hashlib.sha256(data).hexdigest()}, clear=True):
                call_command("prepare_rapidocr_models", source=str(source), stdout=io.StringIO())
                self.assertEqual((root / "runtime/rapidocr-models/model.onnx").read_bytes(), data)
                (source / "model.onnx").write_bytes(b"bad")
                with self.assertRaises(CommandError):
                    call_command("prepare_rapidocr_models", source=str(source), stdout=io.StringIO())
                self.assertEqual((root / "runtime/rapidocr-models/model.onnx").read_bytes(), data)

    def test_recognizer_uses_only_pinned_local_models_and_disables_telemetry(self):
        recognizer = Mock()
        disable = Mock()
        with patch.dict(os.environ, {runtime.THREAD_ENV: "6"}), patch.object(runtime.os, "cpu_count", return_value=8), patch.dict("sys.modules", {"rapidocr": SimpleNamespace(RapidOCR=recognizer),
                                       "onnxruntime": SimpleNamespace(disable_telemetry_events=disable)}):
            with patch.object(runtime, "validate_models", return_value=Path("/local/models")), patch.object(
                runtime.importlib.metadata, "version", side_effect=lambda name: {"rapidocr": "3.10.0", "onnxruntime": "1.31.1"}[name]
            ):
                runtime.build_recognizer()
        disable.assert_called_once_with()
        params = recognizer.call_args.kwargs["params"]
        self.assertFalse(params["Global.use_cls"])
        self.assertEqual(params["Global.text_score"], 0.0)
        self.assertEqual(params["Det.limit_side_len"], 2560)
        self.assertEqual(params["EngineConfig.onnxruntime.intra_op_num_threads"], 6)
        self.assertEqual(params["Rec.model_path"], "/local/models/PP-OCRv6_rec_small.onnx")

    def test_wrong_runtime_version_is_configuration_failure(self):
        with patch.object(runtime, "validate_models", return_value=Path("/local/models")), patch.object(
            runtime.importlib.metadata, "version", return_value="0.0"
        ):
            with self.assertRaisesRegex(ValueError, "requires rapidocr==3.10.0"):
                runtime.build_recognizer()


class OCRWorkerRuntimeTests(SimpleTestCase):
    def test_worker_framing_rejects_empty_trailing_and_short_requests(self):
        self.assertEqual(pdf_comparison_worker.read_pair(io.BytesIO(struct.pack(">QQ", 1, 1) + b"ab")), [b"a", b"b"])
        for data in (b"", struct.pack(">QQ", 0, 1) + b"a", struct.pack(">QQ", 1, 1) + b"a", struct.pack(">QQ", 1, 1) + b"abc"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                pdf_comparison_worker.read_pair(io.BytesIO(data))

    def test_no_ocr_fallback_for_failed_recognition_or_model_loading(self):
        with patch.object(runtime, "build_recognizer", side_effect=ValueError("missing model")):
            self.assertEqual(pdf_comparison_worker.inspect_pair(b"a", b"b"), {"version": 7, "error": "configuration"})
        with patch.object(runtime, "build_recognizer", return_value=object()), patch.object(
            runtime, "extract_document", side_effect=RuntimeError("recognizer failed")
        ):
            self.assertEqual(pdf_comparison_worker.inspect_pair(b"a", b"b"), {"version": 7, "error": "ocr_failed"})

    def test_no_result_and_incomplete_ocr_arrays_fail_not_blank_success(self):
        for response in (None, SimpleNamespace(txts=["word"], scores=[], boxes=[])):
            with self.subTest(response=response), self.assertRaises(ValueError):
                runtime.recognize_page(Mock(return_value=response), b"png", 100, 100)
        empty = SimpleNamespace(txts=None)
        self.assertEqual(runtime.recognize_page(Mock(return_value=empty), b"png", 100, 100), {"status": "completed", "lines": []})

    def test_low_confidence_ocr_is_retained(self):
        response = SimpleNamespace(txts=["visible word"], scores=[0.01], boxes=[[[1, 1], [90, 1], [90, 20], [1, 20]]], word_results=[[]])
        result = runtime.recognize_page(Mock(return_value=response), b"png", 100, 100)
        self.assertEqual(result["lines"][0]["text"], "visible word")
        self.assertEqual(result["lines"][0]["score"], 0.01)

    def test_unlocatable_ocr_boxes_are_not_silent_success(self):
        for box in ([10, 10, 10, 20], [110, 10, 120, 20], [10, 10, 20, 10]):
            with self.subTest(box=box):
                response = SimpleNamespace(txts=["visible word"], scores=[0.9], boxes=[box], word_results=[[]])
                with self.assertRaisesRegex(ValueError, "no visible location"):
                    runtime.recognize_page(Mock(return_value=response), b"png", 100, 100)

    def test_default_model_path_and_timeout_are_bound_to_parent(self):
        result = build_result(ocr_pages("Identical recognized text."), ocr_pages("Identical recognized text."), identical_bytes=True)
        response = subprocess.CompletedProcess([], 0, json.dumps({"version": 7, "result": result}).encode())
        with tempfile.TemporaryDirectory() as temporary, override_settings(BASE_DIR=temporary), patch.dict(os.environ, {}, clear=True):
            with patch.object(pdf_comparison.subprocess, "run", return_value=response) as run:
                pdf_comparison.compare_pdf_bytes(b"same", b"same")
            self.assertEqual(run.call_args.kwargs["timeout"], 240)
            self.assertEqual(run.call_args.kwargs["env"][runtime.MODEL_ENV], str(Path(temporary) / "runtime/rapidocr-models"))

    def test_runtime_override_must_be_absolute(self):
        with patch.dict(os.environ, {"CFM_RAPIDOCR_PYTHON": "python"}):
            with self.assertRaisesRegex(pdf_comparison.PDFComparisonError, "absolute"):
                pdf_comparison.compare_pdf_bytes(b"a", b"b")

    def test_cpu_limit_accounts_for_all_supported_threads_and_linux_memory_is_bounded(self):
        resource = SimpleNamespace(RLIMIT_CPU=0, RLIMIT_AS=1, setrlimit=Mock())
        with patch.dict("sys.modules", {"resource": resource}), patch.object(pdf_comparison_worker.sys, "platform", "linux"):
            pdf_comparison_worker.resource_limits()
        budget = runtime.MAX_THREADS * runtime.WALL_SECONDS
        self.assertEqual(resource.setrlimit.call_args_list[0].args, (0, (budget, budget + 20)))
        self.assertEqual(resource.setrlimit.call_args_list[1].args, (1, (6 * 1024 ** 3, 6 * 1024 ** 3)))
