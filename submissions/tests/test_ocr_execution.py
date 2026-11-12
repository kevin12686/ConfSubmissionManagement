"""Execution-only configuration, ordering and exact CTC reduction contracts."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import onnxruntime as ort
from django.test import SimpleTestCase

from submissions.services.rapidocr_comparison import cache, config, gpu_reduction as gpu, runtime
from submissions.services.rapidocr_comparison.scheduling import RecognizerPool


class ExecutionConfigTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_defaults_cpu_and_gpu_and_explicit_overrides(self):
        self.assertEqual(config.execution_identity(), {"parallel_pages": 1, "det": "small", "rec": "small", "dpi": 300, "batch": 6})
        self.assertEqual(config.timeout_seconds(), 300)
        self.assertEqual(config.supervisor_seconds(), 330)
        os.environ["CFM_OCR_BACKEND"] = "cuda"
        self.assertEqual(config.parallel_pages(), 4)
        with patch.dict(os.environ, {config.PAGE_ENV: "5", config.DETECTION_ENV: "medium", config.RECOGNITION_ENV: "small", config.TIMEOUT_ENV: "600"}):
            self.assertEqual(config.parallel_pages(), 5)
            self.assertEqual(config.supervisor_seconds(), 630)
            self.assertEqual(set(runtime.selected_model_hashes()), {"PP-OCRv6_det_medium.onnx", "PP-OCRv6_rec_small.onnx"})

    def test_invalid_and_boundary_settings(self):
        for name, read, good, bad in (
            (config.PAGE_ENV, config.parallel_pages, ("1", "8"), ("0", "9", "", "1.5", "auto")),
            (config.TIMEOUT_ENV, config.timeout_seconds, ("30", "3600"), ("0", "29", "3601", "", "None")),
        ):
            for value in good:
                with patch.dict(os.environ, {name: value}):
                    self.assertEqual(read(), int(value))
            for value in bad:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                    read()
        for kind, name in (("det", config.DETECTION_ENV), ("rec", config.RECOGNITION_ENV)):
            for value in ("", "tiny", "Medium", "../small"):
                with patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                    config.model_size(kind)

    def test_cpu_single_page_never_installs_gpu_adapter_or_pool(self):
        engine = object()
        with patch.object(runtime, "_build_recognizer", return_value=engine) as build, patch.object(gpu, "ReductionPool") as reduction:
            self.assertIs(runtime.build_recognizer(), engine)
        build.assert_called_once_with()
        reduction.assert_not_called()

    def test_gpu_pool_has_independent_engines_and_one_shared_reduction(self):
        engines = [object() for _ in range(4)]
        with patch.dict(os.environ, {"CFM_OCR_BACKEND": "cuda"}), patch.object(runtime, "_build_recognizer", side_effect=engines), patch.object(gpu, "ReductionPool") as reduction, patch.object(gpu, "attach") as attach:
            pool = runtime.build_recognizer()
        self.assertEqual(pool.engines, engines)
        reduction.assert_called_once_with()
        self.assertEqual([call.args for call in attach.call_args_list], [(engine, reduction.return_value) for engine in engines])

    def test_each_model_and_parallelism_invalidate_runtime_but_timeout_does_not(self):
        before = cache.runtime_fingerprint()
        for name, value in ((config.PAGE_ENV, "2"), (config.DETECTION_ENV, "medium"), (config.RECOGNITION_ENV, "medium")):
            with patch.dict(os.environ, {name: value}):
                self.assertNotEqual(cache.runtime_fingerprint(), before)
        with patch.dict(os.environ, {config.TIMEOUT_ENV: "600"}):
            self.assertEqual(cache.runtime_fingerprint(), before)

    def test_selected_model_checksum_never_falls_back_to_small(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {config.RECOGNITION_ENV: "medium"}):
            root = Path(temporary)
            (root / "PP-OCRv6_rec_small.onnx").write_bytes(b"small is not medium")
            with self.assertRaisesRegex(ValueError, "missing"):
                runtime.validate_models(root)
            selected = runtime.selected_model_hashes()
            with patch.object(runtime, "selected_model_hashes", return_value={"medium.onnx": hashlib.sha256(b"medium").hexdigest()}):
                (root / "medium.onnx").write_bytes(b"small is not medium")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    runtime.validate_models(root)
            self.assertIn("PP-OCRv6_rec_medium.onnx", selected)


class DynamicDispatchTests(SimpleTestCase):
    def test_shared_mutable_recognizer_cannot_be_used_by_multiple_lanes(self):
        engine = object()
        for engines in ([], [engine, engine]):
            with self.assertRaisesRegex(ValueError, "independent"):
                RecognizerPool(engines)

    def test_out_of_order_completion_preserves_identity_and_one_call_per_engine(self):
        for count in (1, 3, 4, 5, 8):
            finished, active = [], set()
            lock = threading.Lock()
            owner = threading.get_ident()
            engines = [object() for _ in range(count)]
            def frames(data):
                self.assertEqual(data, b"bound-pdf-bytes")
                for number in range(1, 13):
                    self.assertEqual(threading.get_ident(), owner)
                    yield {"page": number, "width": 100, "height": 100}, {"page": number, "words": []}, number
            def recognize(engine, number, width, height):
                with lock:
                    self.assertNotIn(engine, active)
                    active.add(engine)
                try:
                    time.sleep(0.012 if number % 4 == 1 else 0.001)
                    finished.append(number)
                    return {"status": "completed", "lines": [{"text": str(number)}]}
                finally:
                    with lock:
                        active.remove(engine)
            with patch.object(runtime, "rendered_pages", side_effect=frames), patch.object(runtime, "recognize_page", side_effect=recognize):
                pages, native = RecognizerPool(engines).extract(b"bound-pdf-bytes")
            self.assertEqual([page["page"] for page in pages], list(range(1, 13)))
            self.assertEqual([page["ocr"]["lines"][0]["text"] for page in pages], list(map(str, range(1, 13))))
            self.assertEqual([page["page"] for page in native], list(range(1, 13)))
            self.assertEqual(sorted(finished), list(range(1, 13)))
            if count > 1:
                self.assertNotEqual(finished, sorted(finished))
            self.assertFalse(active)

    def test_page_failure_and_render_failure_never_return_partial_document(self):
        for render_fail in (False, True):
            def frames(data):
                for number in range(1, 6):
                    if render_fail and number == 3:
                        raise ValueError("render failed")
                    yield {"page": number, "width": 1, "height": 1}, {"page": number, "words": []}, number
            def recognize(engine, number, width, height):
                if not render_fail and number == 3:
                    raise ValueError("OCR failed")
                return {"status": "completed", "lines": []}
            with patch.object(runtime, "rendered_pages", side_effect=frames), patch.object(runtime, "recognize_page", side_effect=recognize):
                with self.assertRaisesRegex(ValueError, "failed"):
                    RecognizerPool([object() for _ in range(3)]).extract(b"pdf")

    def test_empty_bad_order_and_character_limit_fail_closed(self):
        for numbers in ([], [2], [1, 1]):
            def frames(data):
                for number in numbers:
                    yield {"page": number, "width": 1, "height": 1}, {"page": number}, None
            with patch.object(runtime, "rendered_pages", side_effect=frames), patch.object(runtime, "recognize_page", return_value={"lines": []}):
                with self.assertRaises(ValueError):
                    RecognizerPool([object()]).extract(b"pdf")
        def frames(data):
            for number in (1, 2):
                yield {"page": number, "width": 1, "height": 1}, {"page": number}, None
        with patch.object(runtime, "MAX_CHARACTERS", 5), patch.object(runtime, "rendered_pages", side_effect=frames), patch.object(runtime, "recognize_page", return_value={"lines": [{"text": "123"}]}):
            with self.assertRaisesRegex(ValueError, "character"):
                RecognizerPool([object(), object()]).extract(b"pdf")


class ReductionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Exercise the exact embedded graph on CPU too; CUDA acceptance is separate.
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        cls.session = ort.InferenceSession(gpu.GRAPH, sess_options=options, providers=["CPUExecutionProvider"])

    def test_graph_and_boundary_indices_and_unrounded_scores(self):
        self.assertEqual(hashlib.sha256(gpu.GRAPH).hexdigest(), gpu.GRAPH_SHA256)
        rng = np.random.default_rng(42)
        arrays = [rng.uniform(-1, 1, (2, 7, size)).astype(np.float32) for size in (1, 31, 32, 33, 1023, 1024, 1025, 18710)]
        arrays += [np.array(values, np.float32).reshape(1, 1, -1) for values in (
            [1, 2, 2], [-0.0, 0.0], [np.nan, 1], [1, np.nan], [np.inf, np.inf], [-np.inf, -np.inf],
            [np.nextafter(np.float32(0), np.float32(1)), 0],
        )]
        pool = gpu.ReductionPool.__new__(gpu.ReductionPool)
        pool.session, pool.lock, pool.run_options = self.session, threading.Lock(), ort.RunOptions()
        for array in arrays:
            with self.subTest(shape=array.shape):
                result = pool.convert(ort.OrtValue.ortvalue_from_numpy(array))
                if isinstance(result, gpu.ReducedPrediction):
                    indices, scores = result.indices, result.scores
                else:
                    indices, scores = result.argmax(axis=2), result.max(axis=2)
                np.testing.assert_array_equal(indices, array.argmax(axis=2))
                np.testing.assert_array_equal(scores.view(np.uint32), array.max(axis=2).view(np.uint32))

    def test_unchanged_decoder_preserves_word_geometry_and_exception_path(self):
        from rapidocr.ch_ppocr_rec.utils import CTCLabelDecode
        decoder = CTCLabelDecode(character=["a", "b", "c"])
        logits = np.array([[[0.9, 0.1, 0, 0, 0], [0, 0.91, 0, 0, 0], [0, 0.91, 0, 0, 0], [0, 0, 0.92, 0, 0]]], np.float32)
        adapter = gpu.DecodeAdapter(decoder)
        reduced = gpu.ReducedPrediction(logits.argmax(axis=2), logits.max(axis=2))
        for words in (False, True):
            old = decoder(logits, words, wh_ratio_list=[0.7], max_wh_ratio=1.2)
            new = adapter(reduced, words, wh_ratio_list=[0.7], max_wh_ratio=1.2)
            self.assertEqual(str(old), str(new))
            self.assertEqual(str(adapter(logits, words)), str(decoder(logits, words)))
