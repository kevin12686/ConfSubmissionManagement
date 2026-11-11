"""Cache reuse must preserve evidence and never bypass model/version safety."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import Mock, patch

import cv2
import fitz
import numpy as np
from django.core import signing
from django.test import SimpleTestCase, override_settings

from submissions.services import pdf_comparison as service, pdf_comparison_cache as cache
from submissions.services import pdf_comparison_worker as worker
from submissions.services.rapidocr_comparison import runtime
from submissions.services.rapidocr_comparison import cache as evidence_cache
from submissions.tests.ocr_fixtures import ocr_pages


def document(data):
    text = "Independent scientific observations preserve reliable context and the measured dose was "
    pages = ocr_pages(text + ("23 units." if data == b"before" else "99 units."))
    pages[0].update(pdf_width=595, pdf_height=842)
    return pages, [{"page": 1, "words": []}]


def in_process_worker(command, *, input, **kwargs):
    import io
    before, after = worker.read_pair(io.BytesIO(input))
    directory = Path(command[-1]) if "--cache-inputs" in command else None
    result = worker.inspect_pair(before, after, directory, cache_only="--cache-only" in command)
    return subprocess.CompletedProcess(command, 0, json.dumps(result).encode())


class PDFComparisonCacheTests(SimpleTestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(BASE_DIR=Path(self.temporary)))
        self.engine = self.enterContext(patch.object(runtime, "build_recognizer", return_value=object()))
        self.extract = self.enterContext(patch.object(runtime, "extract_document", side_effect=lambda data, engine: document(data)))
        self.fingerprint = self.enterContext(patch.object(evidence_cache, "runtime_fingerprint", return_value="a" * 64))
        self.process = self.enterContext(patch("submissions.services.pdf_comparison_execution.subprocess.run", side_effect=in_process_worker))

    def compare(self, before=b"before", after=b"after"):
        diagnostics = {}
        result = service.compare_pdf_bytes(before, after, diagnostics=diagnostics)
        return result, diagnostics

    def test_pair_hit_is_exact_and_still_checks_runtime(self):
        first, cold = self.compare()
        second, warm = self.compare()
        self.assertEqual(second, first)
        self.assertFalse(cold["pair_hit"])
        self.assertTrue(warm["pair_hit"])
        self.assertEqual(self.extract.call_count, 2)
        self.assertEqual(self.engine.call_count, 2)
        self.assertEqual(self.process.call_args.kwargs["timeout"], 240)

    def test_full_cache_precedes_local_confirmation_but_miss_partial_and_runtime_change_never_compute(self):
        from submissions.services.pdf_comparison_errors import LocalComparisonConfirmationRequired
        expected,_=self.compare()
        count=self.extract.call_count
        with override_settings(CFM_COMPARISON_REMOTE_URL='http://remote.example'):
            self.assertEqual(self.compare()[0],expected)
            self.assertEqual(self.extract.call_count,count)
            with self.assertRaises(LocalComparisonConfirmationRequired):self.compare(after=b'new-correction')
            self.assertEqual(self.extract.call_count,count)
            self.fingerprint.return_value='b'*64
            with self.assertRaises(LocalComparisonConfirmationRequired):self.compare()
            self.assertEqual(self.extract.call_count,count)

    def test_corrupt_full_cache_never_starts_ocr_before_confirmation(self):
        from submissions.services.pdf_comparison_errors import LocalComparisonConfirmationRequired
        self.compare()
        for path in cache._root().joinpath('entries').glob('p-*.json'):path.write_text('corrupt')
        count=self.extract.call_count
        with override_settings(CFM_COMPARISON_REMOTE_URL='http://remote.example'):
            with self.assertRaises(LocalComparisonConfirmationRequired):self.compare()
        self.assertEqual(self.extract.call_count,count)

    def test_new_corrected_bytes_reuse_only_the_unchanged_document(self):
        self.compare()
        result, stats = self.compare(after=b"another correction")
        self.assertEqual(stats["document_hits"], ["before"])
        self.assertFalse(stats["pair_hit"])
        self.assertEqual([call.args[0] for call in self.extract.call_args_list], [b"before", b"after", b"another correction"])
        self.assertGreater(result["content_count"], 0)

    def test_runtime_change_invalidates_documents_and_pair(self):
        first, _ = self.compare()
        self.fingerprint.return_value = "b" * 64
        second, stats = self.compare()
        self.assertEqual(second, first)
        self.assertFalse(stats["pair_hit"])
        self.assertEqual(stats["document_hits"], [])
        self.assertEqual(self.extract.call_count, 4)

    def test_missing_models_cannot_be_bypassed_by_a_warm_pair(self):
        self.compare()
        self.engine.side_effect = ValueError("model unavailable")
        with self.assertRaisesRegex(service.PDFComparisonError, "models or dependencies"):
            self.compare()

    def test_corrupt_signatures_are_misses_not_clean_comparisons(self):
        first, _ = self.compare()
        for path in cache._root().joinpath("entries").iterdir():
            path.write_text("corrupt cache")
        second, stats = self.compare()
        self.assertEqual(second, first)
        self.assertFalse(stats["pair_hit"])
        self.assertEqual(self.extract.call_count, 4)

    def test_completed_original_survives_a_failed_corrected_extraction(self):
        def failing(data, engine):
            if data == b"after":
                raise RuntimeError("failed recognition")
            return document(data)
        self.extract.side_effect = failing
        with self.assertRaisesRegex(service.PDFComparisonError, "could not complete"):
            self.compare()
        self.assertFalse(list(cache._root().joinpath("entries").glob("p-*")))
        self.extract.reset_mock(side_effect=True)
        self.extract.side_effect = lambda data, engine: document(data)
        _, stats = self.compare()
        self.assertEqual(stats["document_hits"], ["before"])
        self.assertEqual([call.args[0] for call in self.extract.call_args_list], [b"after"])

    def test_identical_pdf_bytes_do_not_lose_blank_page_warnings(self):
        pages = ocr_pages("", count=2)
        for page in pages:
            page.update(pdf_width=595, pdf_height=842)
        self.extract.side_effect = lambda data, engine: (copy.deepcopy(pages), [{"page": n, "words": []} for n in (1, 2)])
        result, _ = self.compare(b"blank", b"blank")
        self.assertEqual(self.extract.call_count, 1)
        self.assertEqual(len(result["before_pages"]), 2)
        self.assertEqual(len(result["after_pages"]), 2)
        self.assertEqual(result["coverage_status"], "limited")
        self.assertGreater(result["uncertain_count"], 0)

    def test_reversed_pair_cannot_reuse_forward_findings(self):
        forward, _ = self.compare()
        reverse, stats = self.compare(b"after", b"before")
        self.assertFalse(stats["pair_hit"])
        self.assertEqual(set(stats["document_hits"]), {"before", "after"})
        self.assertNotEqual(forward["findings"][0]["before_text"], reverse["findings"][0]["before_text"])

    def test_busy_pair_uses_the_shared_in_progress_error(self):
        with cache.comparison_cache_job(b"before", b"after"):
            with self.assertRaises(service.ComparisonInProgress):
                self.compare()

    def test_unwritable_cache_does_not_fail_fresh_comparison(self):
        with patch.object(cache, "_root", side_effect=OSError("read-only")):
            result, stats = self.compare()
        self.assertEqual(stats, {"transport": "embedded", "backend": "cpu"})
        self.assertGreater(result["content_count"], 0)

    def test_unavailable_private_job_directory_uses_the_original_worker_protocol(self):
        with patch.object(cache.tempfile, "TemporaryDirectory", side_effect=OSError("temp unavailable")):
            result, stats = self.compare()
        self.assertGreater(result["content_count"], 0)
        self.assertEqual(stats, {"transport": "embedded", "backend": "cpu"})
        self.assertNotIn("--cache-inputs", self.process.call_args.args[0])

    def test_private_job_cleanup_failure_cannot_replace_success(self):
        directory = tempfile.TemporaryDirectory(dir=self.temporary)
        try:
            with (
                patch.object(cache.tempfile, "TemporaryDirectory", return_value=directory),
                patch.object(directory, "cleanup", side_effect=OSError("cleanup unavailable")),
            ):
                result, _ = self.compare()
            self.assertGreater(result["content_count"], 0)
        finally:
            directory.cleanup()
        with cache.comparison_cache_job(b"before", b"after"):
            pass

    def test_cache_write_failure_does_not_replace_success_or_failure(self):
        with patch.object(cache, "_store", side_effect=OSError("disk full")):
            result, _ = self.compare()
        self.assertGreater(result["content_count"], 0)
        self.extract.side_effect = RuntimeError("OCR failed")
        with patch.object(cache, "_store", side_effect=OSError("disk full")):
            with self.assertRaises(service.PDFComparisonError):
                self.compare(after=b"different")

    def test_expired_entries_are_recomputed_and_storage_is_bounded(self):
        first, _ = self.compare()
        for path in cache._root().joinpath("entries").iterdir():
            os.utime(path, (0, 0))
        second, stats = self.compare()
        self.assertEqual(second, first)
        self.assertFalse(stats["pair_hit"])
        self.assertEqual(self.extract.call_count, 4)
        with patch.object(cache, "MAX_DISK_BYTES", 1):
            cache._prune(cache._root())
        self.assertFalse(list(cache._root().joinpath("entries").iterdir()))

    def test_signed_but_invalid_document_evidence_is_rejected(self):
        self.compare()
        root = cache._root() / "entries"
        for path in root.glob("p-*"):
            path.unlink()
        digest = hashlib.sha256(b"before").hexdigest()
        path = root / f"d-{digest}.json"
        value = signing.loads(path.read_text(), salt=cache.SALT)
        value["value"]["native"][0]["words"] = [{"text": "forbidden native text"}]
        cache._store(path, value)
        _, stats = self.compare()
        self.assertEqual(stats["document_hits"], ["after"])
        self.assertEqual(self.extract.call_count, 3)

    def test_symbolic_cache_root_is_never_followed(self):
        root = Path(self.temporary) / "data"
        root.mkdir()
        outside = Path(self.temporary) / "outside"
        outside.mkdir()
        (root / "pdf_comparison_cache").symlink_to(outside, target_is_directory=True)
        result, stats = self.compare()
        self.assertGreater(result["content_count"], 0)
        self.assertFalse(list(outside.iterdir()))
        self.assertEqual(stats, {"transport": "embedded", "backend": "cpu"})

    def test_signed_wrong_hash_and_invalid_pair_are_not_reused(self):
        expected, _ = self.compare()
        for path in (cache._root() / "entries").iterdir():
            payload = signing.loads(path.read_text(), salt=cache.SALT)
            if payload["kind"] == "document":
                payload["hashes"] = ["0" * 64]
            else:
                payload["value"] = {"identical_bytes": False}
            cache._store(path, payload)
        result, stats = self.compare()
        self.assertEqual(result, expected)
        self.assertFalse(stats["pair_hit"])
        self.assertEqual(stats["document_hits"], [])
        self.assertEqual(self.extract.call_count, 4)

    def test_timeout_never_publishes_pair_and_releases_lock(self):
        self.process.side_effect = subprocess.TimeoutExpired("worker", 240)
        with self.assertRaises(service.PDFComparisonError):
            self.compare()
        self.assertFalse(list((cache._root() / "entries").glob("p-*")))
        self.process.side_effect = in_process_worker
        result, stats = self.compare()
        self.assertGreater(result["content_count"], 0)
        self.assertFalse(stats["pair_hit"])

    def test_fingerprint_failure_disables_cache_without_disabling_comparison(self):
        self.fingerprint.side_effect = OSError("runtime fingerprint unavailable")
        result, stats = self.compare()
        self.assertGreater(result["content_count"], 0)
        self.assertEqual(stats, {"transport": "embedded", "backend": "cpu"})
        self.assertFalse(list((cache._root() / "entries").iterdir()))

    def test_timeout_retains_only_a_completed_document_for_retry(self):
        def timed_out(command, **kwargs):
            directory = Path(command[-1])
            pages, native = document(b"before")
            evidence_cache.write_artifact(directory, "runtime.json", {
                "runtime": "a" * 64, "pair_hit": False, "document_hits": [],
            }, 16384)
            evidence_cache.write_artifact(directory, "before.completed.json", {
                "format": evidence_cache.FORMAT, "kind": "document", "runtime": "a" * 64,
                "hashes": [hashlib.sha256(b"before").hexdigest()],
                "value": {"pages": pages, "native": native},
            }, evidence_cache.MAX_DOCUMENT_BYTES)
            (directory / "after.completed.json").write_text('{"value":')
            raise subprocess.TimeoutExpired("worker", 240)
        self.process.side_effect = timed_out
        with self.assertRaises(service.PDFComparisonError):
            self.compare()
        self.assertEqual(len(list((cache._root() / "entries").iterdir())), 1)
        self.process.side_effect = in_process_worker
        _, stats = self.compare()
        self.assertEqual(stats["document_hits"], ["before"])
        self.assertFalse(stats["pair_hit"])
        self.assertEqual([call.args[0] for call in self.extract.call_args_list], [b"after"])

    def test_pruning_never_unlinks_an_active_pair_lock(self):
        from django.core.files import locks
        root = cache._root()
        active = root / "locks" / ("a" * 64 + ".lock")
        idle = root / "locks" / ("b" * 64 + ".lock")
        with cache._open_lock(active) as handle:
            self.assertTrue(locks.lock(handle, locks.LOCK_EX | locks.LOCK_NB))
            idle.touch()
            for path in (active, idle):
                os.utime(path, (0, 0))
            with cache._index_lock(root):
                cache._prune_locks(root)
            self.assertTrue(active.exists())
            self.assertFalse(idle.exists())
            locks.unlock(handle)

    def test_private_artifact_bounds_and_symlinks(self):
        directory = Path(self.temporary)
        (directory / "bad.json").write_text("{" * 20)
        self.assertIsNone(evidence_cache.read_artifact(directory, "bad.json", 10))
        self.assertIsNone(evidence_cache.read_artifact(directory, "bad.json", 100))
        (directory / "linked.json").symlink_to(directory / "bad.json")
        self.assertIsNone(evidence_cache.read_artifact(directory, "linked.json", 100))
        evidence_cache.write_artifact(directory, "oversized.json", {"value": "x" * 100}, 10)
        self.assertFalse((directory / "oversized.json").exists())


class OCRPixelInputTests(SimpleTestCase):
    def test_direct_bgr_matches_the_previous_png_decoder_for_color_blank_and_rotation(self):
        from rapidocr.utils.load_image import LoadImage
        for rotation in (0, 90, 180, 270):
            for colored in (False, True):
                with self.subTest(rotation=rotation, colored=colored), fitz.open() as document:
                    page = document.new_page(width=100.25, height=150.5)
                    if colored:
                        page.draw_rect((5, 5, 90, 90), color=(1, 0, 0), fill=(0, 0.3, 1))
                    page.set_rotation(rotation)
                    pix = page.get_pixmap(dpi=300, colorspace=fitz.csRGB, alpha=False)
                    original = LoadImage()(pix.tobytes("png"))
                    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
                    direct = LoadImage()(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                    self.assertEqual(original.dtype, direct.dtype)
                    self.assertEqual(original.strides, direct.strides)
                    self.assertTrue(np.array_equal(original, direct))
