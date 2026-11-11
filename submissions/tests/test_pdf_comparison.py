import json
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fitz
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse

from submissions.models import FinalSubmission
from submissions.services.checks import publication_readiness_rows
from submissions.services.formatting_history import capture_existing_formatting
from submissions.services.pdf_comparison import (
    COMPARISON_VERSION, PDFComparisonError, comparison_page_png,
    comparison_pdf_bytes, comparison_root, create_pdf_comparison, load_pdf_comparison, comparison_detail_png,
)
from submissions.services.publication_read import publication_database_signature
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase
from submissions.tests.ocr_fixtures import synthetic_ocr_worker


def make_pdf(pages, *, rotation=0, image_only=False):
    with fitz.open() as document:
        for text in pages:
            page = document.new_page(width=595, height=842)
            if text:
                page.insert_textbox((55, 65, 540, 760), text, fontsize=12)
            if image_only:
                page.draw_rect((80, 90, 250, 180), color=(0, 0, 0), fill=(0.7, 0.8, 0.9))
            page.set_rotation(rotation)
        return document.tobytes()


def make_positioned_pdf(pages):
    with fitz.open() as document:
        for lines in pages:
            page = document.new_page(width=595, height=842)
            for y, text in lines:
                page.insert_text((55, y), text, fontsize=10)
        return document.tobytes()


def reflow_pair(*, header="2 N. Silvis-Cividjian", extra_pages=0):
    first = "The initial study evaluated several independent approaches and recorded detailed outcomes for every participant."
    crossing = "Medical physicists involved in the accidents described the hero who"
    last = "discovered the failure mechanism and prevented further losses through careful independent technical investigation."
    before = make_positioned_pdf([
        [(100, "A study (Journal-First)"), (200, first)],
        [(65, "2 N. Silvis-Cividjian"), (130, crossing), (200, last)],
    ])
    after = make_positioned_pdf([
        [(100, "A study"), (200, first), (250, crossing)],
        [(65, header), (200, last)],
    ] + [[] for _ in range(extra_pages)])
    return before, after


class PDFComparisonTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("submissions.services.pdf_comparison_execution.subprocess.run", side_effect=synthetic_ocr_worker))
        self.enterContext(override_settings(BASE_DIR=self.root.resolve()))
        self.make_master_paper()
        self.submission = self.make_final_submission()
        self.before = make_pdf(["The results are shown in Table 2.\nParticipation was voluntary. All students could withdraw."])
        self.after = make_pdf(["The results are shown in Error! Reference source not found.\nThe final results were promising."])
        self.submission.pdf_file.save("original.pdf", ContentFile(self.before))
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(self.after))
        self.url = reverse("submissions:formatting_pdf_comparison", args=[self.submission.pk])

    def test_detail_renders_exact_snapshot_without_changing_evidence_or_state(self):
        payload = create_pdf_comparison(self.submission.pk)
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        readiness = publication_readiness_rows(strict_hash=True)
        original_preview = comparison_page_png(self.submission.pk, payload['token'], 'after', 1)
        saved = (comparison_root() / payload['token'] / 'payload.json').read_bytes()
        for side, pdf in [('before', self.before), ('after', self.after)]:
            url = reverse('submissions:formatting_comparison_detail', args=[self.submission.pk, payload['token'], side, 1])
            for box in ['0,0,0.5,0.5', '0.5,0.5,1,1', '0,0,1,1']:
                response = self.client.get(url, {'box': box, 'width': 1600, 'height': 800})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Cache-Control'], 'no-store')
                x0, y0, x1, y1 = map(float, box.split(','))
                with fitz.open(stream=pdf, filetype='pdf') as doc:
                    page = doc[0]
                    clip = fitz.Rect(x0 * page.rect.width, y0 * page.rect.height, x1 * page.rect.width, y1 * page.rect.height)
                    scale = min(1600 / clip.width, 800 / clip.height, 8)
                    expected = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False).tobytes('png')
                self.assertEqual(response.content, expected)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)
        self.assertEqual((comparison_root() / payload['token'] / 'payload.json').read_bytes(), saved)
        self.assertEqual(comparison_page_png(self.submission.pk, payload['token'], 'after', 1), original_preview)

    def test_detail_rejects_invalid_regions_and_stale_or_wrong_snapshot(self):
        payload = create_pdf_comparison(self.submission.pk)
        url = reverse('submissions:formatting_comparison_detail', args=[self.submission.pk, payload['token'], 'after', 1])
        for params in [dict(box=box, width=800, height=300) for box in ['nan,0,1,1', '0,0,inf,1', '-.1,0,1,1', '0,0,0,1', '1,0,0,1', '0,0,1,2', '0,1', 'x,0,1,1']] + [dict(box='0,0,1,1', width=w, height=h) for w,h in [(0,300),(3073,1),(3000,3000),('nan',300),('1.5',300)]]:
            with self.subTest(params=params):
                self.assertEqual(self.client.get(url, params).status_code, 409)
        for side, page in [('unknown',1),('after',2),('after',0)]:
            with self.assertRaises(PDFComparisonError):
                comparison_detail_png(self.submission.pk, payload['token'], side, page, box=(0,0,1,1), width=800, height=300)
        params = {'box':'0,0,1,1','width':800,'height':300}
        wrong_owner = reverse('submissions:formatting_comparison_detail', args=[self.submission.pk + 1, payload['token'], 'after', 1])
        self.assertEqual(self.client.get(wrong_owner, params).status_code,409)
        with patch('django.core.signing.time.time', return_value=time.time() + 10800):
            self.assertEqual(self.client.get(url, params).status_code,409)
        self.assertEqual(self.client.post(url, params).status_code,405)
        path = comparison_root() / payload['token'] / 'after.pdf'
        old = path.read_bytes(); path.write_bytes(b'corrupt')
        self.assertEqual(self.client.get(url, {'box':'0,0,1,1','width':800,'height':300}).status_code,409)
        path.write_bytes(old)
        self.submission.formatted_pdf_file.save('changed.pdf', ContentFile(make_pdf(['changed'])))
        self.assertEqual(self.client.get(url, {'box':'0,0,1,1','width':800,'height':300}).status_code,409)

    def test_detail_rotation_and_density_cap(self):
        self.submission.formatted_pdf_file.save('rotated.pdf', ContentFile(make_pdf(['Rotated text'], rotation=90)))
        payload = create_pdf_comparison(self.submission.pk)
        for box in [(0,0,1,1),(.999,.999,1,1)]:
            image = comparison_detail_png(self.submission.pk, payload['token'], 'after', 1, box=box, width=3072, height=1000)
            pix = fitz.Pixmap(image)
            self.assertGreater(pix.width,0); self.assertGreater(pix.height,0)
            self.assertLessEqual(pix.width,3074); self.assertLessEqual(pix.height,1002)

    def test_build_is_audited_and_cannot_change_publication_or_review_state(self):
        self.mark_submission_publication_ready(self.submission)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        readiness = publication_readiness_rows(strict_hash=True)
        payload = create_pdf_comparison(self.submission.pk)
        self.assertTrue(any(item["category"] == "reference" for item in payload["result"]["findings"]))
        self.assertTrue(any(item["category"] == "text" for item in payload["result"]["findings"]))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)
        self.assertEqual(Path(self.submission.pdf_file.path).read_bytes(), self.before)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), self.after)
        self.assertIn('"action": "formatting_pdf_compare"', (self.audit_root / "audit.log").read_text())


    def test_current_corrected_missing_never_falls_back_to_original_or_history(self):
        capture_existing_formatting(self.submission.pk)
        Path(self.submission.formatted_pdf_file.path).unlink()
        with self.assertRaisesRegex(PDFComparisonError, "missing"):
            create_pdf_comparison(self.submission.pk)
        self.assertFalse(comparison_root().exists())

    def test_missing_original_requires_an_explicit_revision_baseline(self):
        revision = capture_existing_formatting(self.submission.pk)
        Path(self.submission.pdf_file.path).unlink()
        with self.assertRaisesRegex(PDFComparisonError, "missing"):
            create_pdf_comparison(self.submission.pk)
        payload = create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}")
        self.assertTrue(payload["result"]["identical_bytes"])

    def test_decision_required_or_inactive_scope_is_never_silently_widened(self):
        from submissions.models import InitialPaper
        InitialPaper.objects.filter(paper_id="P001").update(publication_decision_status="decision_required")
        with self.assertRaisesRegex(PDFComparisonError, "no longer"):
            create_pdf_comparison(self.submission.pk)
        InitialPaper.objects.filter(paper_id="P001").update(publication_decision_status="publishing")
        FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=False)
        with self.assertRaisesRegex(PDFComparisonError, "no longer"):
            create_pdf_comparison(self.submission.pk)

    def test_ambiguous_active_versions_cannot_produce_a_publication_comparison(self):
        self.make_final_submission(final_submission_id="101")
        with self.assertRaisesRegex(PDFComparisonError, "Multiple active"):
            create_pdf_comparison(self.submission.pk)

    def test_revision_baseline_is_scoped_and_integrity_verified(self):
        revision = capture_existing_formatting(self.submission.pk)
        self.submission.formatted_pdf_file.save("next.pdf", ContentFile(make_pdf(["Latest corrected version"])))
        payload = create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}")
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "before"), self.after)
        self.assertNotEqual(payload["before_sha256"], payload["after_sha256"])
        other = self.make_final_submission(final_submission_id="OTHER", active_version=False)
        other.formatted_pdf_file.save("other.pdf", ContentFile(self.after))
        other_revision = capture_existing_formatting(other.pk)
        with self.assertRaisesRegex(PDFComparisonError, "does not belong"):
            create_pdf_comparison(self.submission.pk, f"revision-{other_revision.pk}")
        Path(revision.pdf_file.path).write_bytes(b"corrupt")
        with self.assertRaisesRegex(PDFComparisonError, "integrity"):
            create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}")

    def test_stale_scope_hash_and_expiry_are_rejected_for_page_and_pdf(self):
        payload = create_pdf_comparison(self.submission.pk)
        self.assertTrue(comparison_page_png(self.submission.pk, payload["token"], "after", 1).startswith(b"\x89PNG"))
        Path(self.submission.formatted_pdf_file.path).write_bytes(make_pdf(["Changed outside upload workflow"]))
        with self.assertRaisesRegex(PDFComparisonError, "changed after"):
            load_pdf_comparison(self.submission.pk, payload["token"])
        self.submission.formatted_pdf_file.save("restored.pdf", ContentFile(self.after))
        payload = create_pdf_comparison(self.submission.pk)
        FinalSubmission.objects.filter(pk=self.submission.pk).update(discarded=True, active_version=False)
        with self.assertRaisesRegex(PDFComparisonError, "no longer"):
            comparison_page_png(self.submission.pk, payload["token"], "after", 1)
        FinalSubmission.objects.filter(pk=self.submission.pk).update(discarded=False, active_version=True)
        with patch("django.core.signing.time.time", return_value=time.time() + timedelta(hours=3).total_seconds()):
            with self.assertRaisesRegex(PDFComparisonError, "expired"):
                load_pdf_comparison(self.submission.pk, payload["token"])

    def test_snapshot_corruption_and_token_traversal_are_rejected(self):
        payload = create_pdf_comparison(self.submission.pk)
        directory = comparison_root() / payload["token"]
        (directory / "after.pdf").write_bytes(b"bad")
        with self.assertRaisesRegex(PDFComparisonError, "integrity"):
            comparison_page_png(self.submission.pk, payload["token"], "after", 1)
        for token in ("../outside", "x" * 32, ""):
            with self.subTest(token=token), self.assertRaises(PDFComparisonError):
                load_pdf_comparison(self.submission.pk, token)
        wrapper = json.loads((directory / "payload.json").read_text())
        wrapper["signed"] += "corrupt"
        (directory / "payload.json").write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(PDFComparisonError, "unavailable"):
            load_pdf_comparison(self.submission.pk, payload["token"])

    def test_old_method_requires_explicit_rebuild_without_rewriting_saved_comparison(self):
        from django.core import signing
        payload = create_pdf_comparison(self.submission.pk)
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        path = comparison_root() / payload["token"] / "payload.json"
        wrapper = json.loads(path.read_text())
        payload["version"] = COMPARISON_VERSION - 1
        wrapper["signed"] = signing.dumps(payload, salt="submissions.pdf-comparison.v1", compress=True)
        path.write_text(json.dumps(wrapper))
        old_bytes = path.read_bytes()
        response = self.client.get(self.url, {"comparison": payload["token"], "baseline": "original"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "older comparison method", status_code=409)
        self.assertContains(response, "Build comparison", status_code=409)
        self.assertEqual(path.read_bytes(), old_bytes)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)
        self.assertEqual(Path(self.submission.pdf_file.path).read_bytes(), self.before)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), self.after)

    def test_scope_and_current_bytes_are_bound_but_notes_and_review_are_not(self):
        payload = create_pdf_comparison(self.submission.pk)
        self.submission.format_notes = "An unrelated editorial note"
        self.submission.format_status = "needs_edit"
        self.submission.save()
        self.assertEqual(load_pdf_comparison(self.submission.pk, payload["token"])["token"], payload["token"])
        with self.assertRaisesRegex(PDFComparisonError, "does not belong"):
            load_pdf_comparison(self.submission.pk + 1, payload["token"])

    def test_new_comparison_purges_only_expired_staging(self):
        from django.utils import timezone
        old = create_pdf_comparison(self.submission.pk)
        directory = comparison_root() / old["token"]
        old_time = timezone.now() - timedelta(hours=3)
        wrapper = json.loads((directory / "payload.json").read_text())
        wrapper["created_at"] = old_time.isoformat()
        (directory / "payload.json").write_text(json.dumps(wrapper))
        recent = create_pdf_comparison(self.submission.pk)
        self.assertFalse(directory.exists())
        self.assertTrue((comparison_root() / recent["token"]).exists())

    def test_selected_pdf_paths_cannot_be_overridden_by_post_fields(self):
        response = self.client.post(self.url, {"baseline": "original", "pdf_path": "/etc/passwd", "target": "original"})
        page = self.client.get(response.url)
        target = self.client.get(page.context["comparison_data"]["pdf_urls"]["after"])
        self.assertEqual(b"".join(target.streaming_content), self.after)
        target.close()

    def test_compare_detects_concurrent_file_change_without_publishing_stale_result(self):
        from submissions.services.pdf_comparison import compare_pdf_bytes as compare
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        def compare_then_replace(*args, **kwargs):
            result = compare(*args, **kwargs)
            Path(self.submission.formatted_pdf_file.path).write_bytes(make_pdf(["Concurrent bytes"]))
            return result
        with patch("submissions.services.pdf_comparison.compare_pdf_bytes", side_effect=compare_then_replace):
            with self.assertRaisesRegex(PDFComparisonError, "changed during"):
                create_pdf_comparison(self.submission.pk)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertFalse(comparison_root().exists())

    def test_failed_audit_and_invalid_pdf_leave_no_partial_comparison(self):
        with patch("submissions.services.pdf_comparison.audit_success", side_effect=OSError("audit unavailable")):
            with self.assertRaisesRegex(PDFComparisonError, "audit unavailable"):
                create_pdf_comparison(self.submission.pk)
        self.assertEqual(list(comparison_root().iterdir()), [])
        self.submission.formatted_pdf_file.save("invalid.pdf", ContentFile(b"not a PDF"))
        with self.assertRaises(PDFComparisonError):
            create_pdf_comparison(self.submission.pk)
        self.assertEqual(list(comparison_root().iterdir()), [])

    def test_failed_staging_creation_never_deletes_an_existing_comparison(self):
        from types import SimpleNamespace
        payload = create_pdf_comparison(self.submission.pk)
        with patch("submissions.services.pdf_comparison.uuid.uuid4", return_value=SimpleNamespace(hex=payload["token"])):
            with self.assertRaises(PDFComparisonError):
                create_pdf_comparison(self.submission.pk)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "after"), self.after)


    def test_views_are_read_only_and_sources_open_exact_snapshot_bytes(self):
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        response = self.client.get(self.url)
        self.assertContains(response, "Build comparison")
        self.assertFalse(comparison_root().exists())
        response = self.client.post(self.url, {"baseline": "original", "next": "https://example.com/"})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.url)
        self.assertContains(response, "OCR text changes")
        self.assertContains(response, "Browse all pages")
        self.assertNotIn("example.com", response.context["back_url"])
        payload = response.context["comparison_data"]
        original = self.client.get(payload["pdf_urls"]["before"])
        self.assertEqual(b"".join(original.streaming_content), self.before)
        original.close()
        page = self.client.get(payload["page_urls"]["after"].replace("/0.png", "/1.png"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "image/png")
        self.assertEqual(page["Cache-Control"], "no-store")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

    def test_comparison_entry_is_shared_by_list_single_and_focus_modes(self):
        for params in ({"filter": "all"}, {"mode": "single", "filter": "all"}, {"mode": "focus", "submission": self.submission.pk}):
            with self.subTest(params=params):
                page = self.client.get(reverse("submissions:formatting"), params, follow=True)
                self.assertContains(page, f'href="{self.url}?next=')
                self.assertContains(page, 'target="_blank" rel="noopener">Compare PDF</a>')

    def test_content_only_presentation_preserves_comparison_results_and_review_state(self):
        before, after = reflow_pair()
        self.submission.pdf_file.save("before-reflow.pdf", ContentFile(before))
        self.submission.formatted_pdf_file.save("after-reflow.pdf", ContentFile(after))
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        payload = create_pdf_comparison(self.submission.pk)
        response = self.client.get(self.url, {"comparison": payload["token"]})
        self.assertContains(response, 'data-comparison-level="warning"')
        self.assertContains(response, 'data-finding-group="ignored_spacing"')
        self.assertContains(response, 'data-finding-group="wrap_hyphen"')
        self.assertContains(response, 'data-finding-group="tolerated"')
        self.assertEqual(payload["result"]["info_count"], 0)
        self.assertContains(response, 'data-finding-group="uncertain"')
        self.assertContains(response, '<option value="removed">Removed text</option>')
        self.assertNotContains(response, 'Same text / sequence position')
        self.assertEqual(response.context["comparison_data"]["findings"], payload["result"]["findings"])
        self.assertEqual(response.context["comparison_data"]["page_map"], payload["result"]["page_map"])
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)

    def test_final_and_crosscheck_remain_byte_identical_after_comparison(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import export_publication_package
        original, publication_after = reflow_pair(extra_pages=6)
        self.submission.pdf_file.save("reflow-original.pdf", ContentFile(original))
        self.submission.formatted_pdf_file.save("ready-eight-pages.pdf", ContentFile(publication_after))
        self.mark_submission_publication_ready(self.submission)
        before_state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        readiness = publication_readiness_rows(strict_hash=True)
        result = create_pdf_comparison(self.submission.pk)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, result["token"], "after"), publication_after)
        with zipfile.ZipFile(export_publication_package()) as archive:
            name = next(name for name in archive.namelist() if name.startswith("PDF/"))
            self.assertEqual(archive.read(name), publication_after)
        result = prepare_crosscheck_upload("PDFCOMPARE")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_PDFCOMPARE.pdf"), publication_after)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before_state)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)

    def test_backup_excludes_temporary_comparison_and_keeps_review_artifacts(self):
        from submissions.services.system_state import export_system_state
        payload = create_pdf_comparison(self.submission.pk)
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            self.assertFalse(any("formatting_comparison_previews" in name for name in archive.namelist()))
            self.assertFalse(any("pdf_comparison_cache" in name for name in archive.namelist()))
        self.assertTrue((comparison_root() / payload["token"] / "after.pdf").exists())

    def test_cleanup_protects_open_comparison_and_clear_database_removes_it(self):
        from submissions.models import AppSetting
        from submissions.services.storage_inventory import build_storage_inventory
        payload = create_pdf_comparison(self.submission.pk)
        setting = AppSetting.load()
        setting.reports_folder = str(self.root / "data")
        setting.save()
        cpu_cache = self.root / "data" / "pdf_comparison_cache"
        (cpu_cache / "generated-report.zip").write_bytes(b"rebuildable cache test")
        inventory = build_storage_inventory()
        self.assertFalse(any("formatting_comparison_previews" in str(row["path"]) for row in inventory["report_export_cleanup_candidates"]))
        self.assertFalse(any("pdf_comparison_cache" in str(row["path"]) for row in inventory["report_export_cleanup_candidates"]))
        setting.reports_folder = str(self.root / "reports")
        setting.save()
        self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertFalse((comparison_root() / payload["token"]).exists())
        self.assertFalse(list(cpu_cache.iterdir()))
