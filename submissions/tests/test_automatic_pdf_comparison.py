import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from submissions.models import FinalSubmission, FormattingRevision, InitialPaper
from submissions.services.automatic_pdf_comparison import (
    ComparisonInProgress, SESSION_KEY, _request_directory, _request_lock,
    decode_upload_comparison, get_upload_comparison, remember_upload_comparison,
    upload_comparison_entries,
)
from submissions.services.formatting import (
    create_formatting_review_snapshot, load_formatting_review_snapshot,
    save_formatting_review_result, update_formatting_submission,
)
from submissions.services.pdf_comparison import (
    PDFComparisonError, StaleUploadComparison, comparison_pdf_bytes,
    comparison_root, load_pdf_comparison,
)
from submissions.services.publication_read import publication_database_signature
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase
from submissions.tests.test_pdf_comparison import make_pdf, reflow_pair


class AutomaticPDFComparisonTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(BASE_DIR=self.root.resolve()))
        self.make_master_paper()
        self.submission = self.make_final_submission(format_status="pending")
        from django.core.files.base import ContentFile
        self.original = make_pdf(["Original research findings are presented in Table 2."])
        self.corrected = make_pdf(["Corrected research findings are presented in Table 3."])
        self.submission.pdf_file.save("original.pdf", ContentFile(self.original))
        self.session = {}
        self.endpoint = reverse("submissions:formatting_upload_comparison", args=[self.submission.pk])

    def open_formatting_review(self, submission, *, mode="list", status_filter="all"):
        params = {"filter": status_filter}
        if mode == "single":
            params.update(mode=mode, current=submission.pk)
        elif mode == "focus":
            params.update(mode=mode, submission=submission.pk)
        return self.client.get(reverse("submissions:formatting"), params, follow=True)

    def formatting_post_data(self, response, submission, **overrides):
        row = next(row for row in response.context["rows"] if row["submission"].pk == submission.pk)
        navigation = response.context.get("single_navigation") or {}
        return {
            "submission_id": submission.pk, "mode": response.context["mode"],
            "filter": response.context["current_filter"], "q": response.context["q"],
            "return_to": response.wsgi_request.get_full_path(),
            "queue": navigation.get("token", ""), "review_snapshot": row["review_snapshot"],
            "format_status": "pending", "format_notes": "Saved upload note", **overrides,
        }

    def save(self, pdf=None, source=None):
        self.submission.refresh_from_db()
        token = create_formatting_review_snapshot({}, self.submission)
        evidence = load_formatting_review_snapshot({}, token, submission_id=self.submission.pk)
        return save_formatting_review_result(self.submission.pk, {
            "format_status": "pending", "format_notes": "Saved note",
            "corrected_pdf": SimpleUploadedFile("corrected.pdf", pdf) if pdf is not None else None,
            "corrected_source": SimpleUploadedFile("corrected.zip", source) if source is not None else None,
        }, evidence)

    def receipt(self, pdf=None):
        result = self.save(pdf if pdf is not None else self.corrected)
        return result, remember_upload_comparison(self.session, result)

    def build(self, entry, **kwargs):
        return get_upload_comparison(self.submission.pk, entry["token"], build=True, **kwargs)

    def view_upload(self, *, mode="list", status_filter="all", as_source=False):
        self.submission.refresh_from_db()
        page = self.open_formatting_review(self.submission, mode=mode, status_filter=status_filter)
        with patch("submissions.services.formatting.get_title_author", return_value=(self.submission.final_submission_title, "Author", 1)):
            response = self.client.post(reverse("submissions:formatting"), self.formatting_post_data(
                page, self.submission, format_status="pending",
                **{"corrected_source" if as_source else "corrected_pdf": SimpleUploadedFile("new.pdf", self.corrected)},
            ))
        return response

    def test_saved_result_carries_exact_revision_without_changing_legacy_return_type(self):
        result, entry = self.receipt()
        self.assertIsInstance(result.submission, FinalSubmission)
        self.assertEqual(result.revision.pdf_metadata["sha256"], hashlib.sha256(self.corrected).hexdigest())
        binding = decode_upload_comparison(self.submission.pk, entry["token"])
        self.assertEqual(binding["revision_id"], result.revision.pk)
        legacy = update_formatting_submission(result.submission, {"format_status": "pending", "format_notes": "Note only"})
        self.assertIsInstance(legacy, FinalSubmission)
        self.assertEqual(FormattingRevision.objects.count(), 1)

    def test_get_is_read_only_and_post_builds_once_then_reuses_the_same_result(self):
        result, entry = self.receipt()
        saved_state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        before_audit = (self.audit_root / "audit.log").read_bytes()
        response = self.client.get(self.endpoint, {"token": entry["token"]})
        self.assertEqual(response.json()["state"], "pending")
        self.assertFalse(comparison_root().exists())
        self.assertEqual((self.audit_root / "audit.log").read_bytes(), before_audit)
        from submissions.services.pdf_comparison import create_pdf_comparison
        with patch("submissions.services.automatic_pdf_comparison.create_pdf_comparison", wraps=create_pdf_comparison) as create:
            first = self.client.post(self.endpoint, {"token": entry["token"], "baseline": "revision-999", "next": "https://example.com/"})
            second = self.client.post(self.endpoint, {"token": entry["token"], "retry": "1"})
            self.assertEqual(create.call_count, 1)
        self.assertEqual(first.json()["state"], "complete")
        self.assertEqual(first["Cache-Control"], "no-store")
        self.assertEqual(first.json()["url"], second.json()["url"])
        self.assertNotIn("example.com", first.json()["url"])
        refreshed = self.client.get(self.endpoint, {"token": entry["token"]})
        self.assertEqual(refreshed.json()["url"], first.json()["url"])
        return_to = reverse("submissions:formatting") + "?filter=edited&q=study"
        contextual = self.client.get(self.endpoint, {"token": entry["token"], "next": return_to})
        query = parse_qs(urlsplit(contextual.json()["url"]).query)
        self.assertEqual(query["next"], [return_to])
        self.assertEqual(query["comparison"], parse_qs(urlsplit(first.json()["url"]).query)["comparison"])
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), saved_state)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(FormattingRevision.objects.count(), 1)
        events = [json.loads(line) for line in (self.audit_root / "audit.log").read_text().splitlines()]
        self.assertEqual(sum(event["action"] == "formatting_pdf_compare" and event["status"] == "success" for event in events), 1)

    def test_second_upload_always_compares_against_original_not_previous_corrected(self):
        first, _ = self.receipt()
        newer = make_pdf(["Newer corrected study content."])
        second, entry = self.receipt(newer)
        payload = self.build(entry)
        self.assertEqual(payload["upload_binding"]["revision_id"], second.revision.pk)
        self.assertNotEqual(payload["upload_binding"]["revision_id"], first.revision.pk)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "before"), self.original)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "after"), newer)

    def test_same_bytes_uploaded_again_still_invalidates_old_upload_request(self):
        _, old = self.receipt()
        current, new = self.receipt()
        response = self.client.post(self.endpoint, {"token": old["token"]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["retryable"])
        payload = self.build(new)
        self.assertEqual(payload["upload_binding"]["revision_id"], current.revision.pk)

    def test_new_upload_during_comparison_cannot_publish_a_result_for_the_wrong_version(self):
        _, entry = self.receipt()
        from submissions.services.pdf_comparison import compare_pdf_bytes
        newer = make_pdf(["Concurrent new version must remain saved."])
        def concurrent_save(*args):
            result = compare_pdf_bytes(*args)
            self.save(newer)
            return result
        with patch("submissions.services.pdf_comparison.compare_pdf_bytes", side_effect=concurrent_save):
            response = self.client.post(self.endpoint, {"token": entry["token"]})
        self.assertNotEqual(response.json()["state"], "complete")
        self.submission.refresh_from_db()
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), newer)
        self.assertEqual(FormattingRevision.objects.count(), 2)

    def test_finished_auto_result_cannot_be_opened_after_a_new_pdf_upload(self):
        _, entry = self.receipt()
        payload = self.build(entry)
        self.receipt(make_pdf(["Newer PDF"]))
        with self.assertRaises(PDFComparisonError):
            load_pdf_comparison(self.submission.pk, payload["token"])
        response = self.client.get(self.endpoint, {"token": entry["token"]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["retryable"])

    def test_notes_status_and_source_only_do_not_register_new_pdf_comparisons(self):
        _, entry = self.receipt()
        payload = self.build(entry)
        for source in (None, b"corrected source only"):
            result = self.save(source=source)
            self.assertIsNone(remember_upload_comparison(self.session, result))
        self.assertEqual(upload_comparison_entries(self.session)[0]["token"], entry["token"])
        self.assertEqual(get_upload_comparison(self.submission.pk, entry["token"])["token"], payload["token"])

    def test_missing_original_never_falls_back_to_revision_history(self):
        _, entry = self.receipt()
        Path(self.submission.pdf_file.path).unlink()
        response = self.client.post(self.endpoint, {"token": entry["token"]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("missing", response.json()["message"].lower())
        self.submission.refresh_from_db()
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), self.corrected)
        self.assertEqual(FormattingRevision.objects.count(), 1)

    def test_missing_or_mutated_corrected_file_is_never_substituted(self):
        for content in (None, make_pdf(["External replacement"])):
            result, entry = self.receipt()
            path = Path(result.submission.formatted_pdf_file.path)
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            response = self.client.post(self.endpoint, {"token": entry["token"]})
            self.assertNotEqual(response.json()["state"], "complete")

    def test_comparison_parse_failure_does_not_undo_upload_or_review_resets(self):
        result, entry = self.receipt(b"invalid PDF bytes")
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        response = self.client.post(self.endpoint, {"token": entry["token"]})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.json()["retryable"])
        self.assertEqual(Path(result.submission.formatted_pdf_file.path).read_bytes(), b"invalid PDF bytes")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)
        self.assertEqual(FormattingRevision.objects.count(), 1)

    def test_audit_and_cache_write_failures_leave_saved_files_intact(self):
        result, entry = self.receipt()
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with patch("submissions.services.pdf_comparison.audit_success", side_effect=OSError("audit unavailable")):
            response = self.client.post(self.endpoint, {"token": entry["token"]})
        self.assertEqual(response.status_code, 422)
        with patch("submissions.services.automatic_pdf_comparison._write_cache", side_effect=OSError("disk unavailable")):
            response = self.client.post(self.endpoint, {"token": entry["token"], "retry": "1"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)
        self.assertEqual(Path(result.submission.formatted_pdf_file.path).read_bytes(), self.corrected)
        self.assertEqual(self.client.post(self.endpoint, {"token": entry["token"], "retry": "1"}).json()["state"], "complete")

    def test_concurrent_duplicate_request_does_not_create_a_second_comparison(self):
        _, entry = self.receipt()
        binding = decode_upload_comparison(self.submission.pk, entry["token"])
        directory = _request_directory(binding, create=True)
        with _request_lock(directory):
            with self.assertRaises(ComparisonInProgress):
                self.build(entry)
        self.assertTrue(self.build(entry))

    def test_csrf_is_required_for_build_and_get_never_builds(self):
        from django.test import Client
        _, entry = self.receipt()
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(self.endpoint, {"token": entry["token"]}).status_code, 403)
        self.assertEqual(client.get(self.endpoint, {"token": entry["token"]}).json()["state"], "pending")
        self.assertFalse(comparison_root().exists())

    def test_cache_corruption_requires_explicit_retry_not_silent_rebuild(self):
        _, entry = self.receipt()
        payload = self.build(entry)
        binding = decode_upload_comparison(self.submission.pk, entry["token"])
        directory = _request_directory(binding)
        (directory / "payload.json").write_text("corrupt")
        with patch("submissions.services.automatic_pdf_comparison.create_pdf_comparison") as create:
            response = self.client.get(self.endpoint, {"token": entry["token"]})
            self.assertEqual(response.status_code, 422)
            response = self.client.post(self.endpoint, {"token": entry["token"]})
            self.assertEqual(response.status_code, 422)
            create.assert_not_called()
        response = self.client.post(self.endpoint, {"token": entry["token"], "retry": "1"})
        self.assertEqual(response.json()["state"], "complete")
        self.assertTrue((comparison_root() / payload["token"] / "after.pdf").exists())

    def test_receipts_are_bounded_and_older_pending_uploads_do_not_autostart(self):
        _, first = self.receipt()
        other = self.make_final_submission(final_submission_id="OTHER", paper_id_filled="P002")
        self.make_master_paper("P002")
        self.submission, previous = other, self.submission
        self.receipt(make_pdf(["Second paper PDF"]))
        self.submission = previous
        entries = upload_comparison_entries(self.session)
        self.assertTrue(entries[0]["autostart"])
        self.assertFalse(entries[1]["autostart"])
        self.assertEqual(entries[1]["token"], first["token"])

    def test_automatic_request_cache_is_excluded_from_backup_and_protected_until_clear(self):
        import zipfile
        from submissions.models import AppSetting
        from submissions.services.storage_inventory import build_storage_inventory
        from submissions.services.system_state import export_system_state
        _, entry = self.receipt()
        payload = self.build(entry)
        request_directory = _request_directory(decode_upload_comparison(self.submission.pk, entry["token"]))
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            self.assertFalse(any("formatting_comparison_previews" in name for name in archive.namelist()))
        setting = AppSetting.load()
        setting.reports_folder = str(self.root / "data")
        setting.save()
        inventory = build_storage_inventory()
        self.assertFalse(any("formatting_comparison_previews" in str(row["path"]) for row in inventory["report_export_cleanup_candidates"]))
        self.assertTrue(request_directory.exists())
        setting.reports_folder = str(self.root / "reports")
        setting.save()
        self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertFalse(request_directory.exists())
        self.assertFalse((comparison_root() / payload["token"]).exists())

    def test_invalid_cross_submission_expired_and_stale_scope_requests_are_rejected(self):
        _, entry = self.receipt()
        for token in ("bad", entry["token"] + "tampered"):
            self.assertEqual(self.client.post(self.endpoint, {"token": token}).status_code, 409)
        other = reverse("submissions:formatting_upload_comparison", args=[self.submission.pk + 1])
        self.assertEqual(self.client.post(other, {"token": entry["token"]}).status_code, 409)
        with patch("django.core.signing.time.time", return_value=time.time() + 10800):
            self.assertEqual(self.client.post(self.endpoint, {"token": entry["token"]}).status_code, 409)
            self.assertEqual(upload_comparison_entries(self.session), [])
        for field, value in (("discarded", True), ("active_version", False)):
            FinalSubmission.objects.filter(pk=self.submission.pk).update(**{field: value})
            response = self.client.post(self.endpoint, {"token": entry["token"]})
            self.assertEqual(response.status_code, 409)
            FinalSubmission.objects.filter(pk=self.submission.pk).update(**{field: not value})
        InitialPaper.objects.filter(paper_id="P001").update(publication_decision_status="decision_required")
        self.assertEqual(self.client.post(self.endpoint, {"token": entry["token"]}).status_code, 409)
        from django.utils import timezone
        InitialPaper.objects.filter(paper_id="P001").update(
            publication_decision_status="not_publishing", publication_exclusion_reason="withdrawn",
            publication_excluded_at=timezone.now(),
        )
        self.assertEqual(self.client.post(self.endpoint, {"token": entry["token"]}).status_code, 409)

    def test_upload_redirects_back_and_shows_a_shared_status_in_all_review_modes(self):
        for mode in ("list", "single", "focus"):
            with self.subTest(mode=mode):
                response = self.view_upload(mode=mode)
                self.assertEqual(response.status_code, 302)
                if mode == "single":
                    self.assertIn(f"current={self.submission.pk}", response.url)
                elif mode == "focus":
                    self.assertIn(f"submission={self.submission.pk}", response.url)
                page = self.client.get(response.url)
                self.assertContains(page, "data-upload-comparison")
                self.assertContains(page, "Original → Corrected")
                self.assertNotContains(page, "Comparison findings")
        self.assertFalse(comparison_root().exists())

    def test_pdf_in_source_field_registers_the_pdf_upload_not_a_source_only_event(self):
        response = self.view_upload(as_source=True)
        self.assertEqual(response.status_code, 302)
        entry = self.client.session[SESSION_KEY][0]
        payload = self.build(entry)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "after"), self.corrected)

    def test_title_guard_does_not_register_until_confirm_and_cancel_registers_nothing(self):
        page = self.open_formatting_review(self.submission, mode="single")
        data = self.formatting_post_data(page, self.submission, corrected_pdf=SimpleUploadedFile("wrong-title.pdf", self.corrected))
        with patch("submissions.services.formatting.get_title_author", return_value=("Wrong title", "Author", 1)):
            preview = self.client.post(reverse("submissions:formatting"), data)
        self.assertEqual(preview.status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)
        context = preview.context["formatting_confirmation"]
        confirmed = self.client.post(reverse("submissions:formatting"), {
            "action": "confirm_formatting_upload", "preview_token": context["token"],
            "submission_id": self.submission.pk, "mode": "single", "queue": context["queue"],
        })
        self.assertEqual(confirmed.status_code, 302)
        self.assertIn(SESSION_KEY, self.client.session)
        self.assertIn(f"current={self.submission.pk}", confirmed.url)
        existing = self.client.session[SESSION_KEY]
        page = self.open_formatting_review(self.submission)
        with patch("submissions.services.formatting.get_title_author", side_effect=ValueError("extractor unavailable")):
            preview = self.client.post(reverse("submissions:formatting"), self.formatting_post_data(page, self.submission, corrected_pdf=SimpleUploadedFile("cancel.pdf", self.corrected)))
        context = preview.context["formatting_confirmation"]
        self.client.post(reverse("submissions:formatting"), {"action": "cancel_formatting_upload", "preview_token": context["token"], "submission_id": self.submission.pk})
        self.assertEqual(self.client.session[SESSION_KEY], existing)
        self.assertEqual(FormattingRevision.objects.count(), 1)

    def test_comparison_preparation_failure_is_not_reported_as_upload_failure(self):
        with patch("submissions.controllers.reviews.remember_upload_comparison", side_effect=OSError("cannot prepare comparison")):
            response = self.view_upload()
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.url)
        self.assertContains(page, "Formatting files saved, but automatic comparison could not be prepared")
        self.assertNotContains(page, "Formatting update failed")
        self.submission.refresh_from_db()
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), self.corrected)
        self.assertEqual(FormattingRevision.objects.count(), 1)

    def test_filtered_out_saved_record_still_has_an_identified_comparison_status(self):
        self.submission.format_status = "review_ok"
        self.submission.save()
        response = self.view_upload(status_filter="review_ok")
        page = self.client.get(response.url)
        self.assertFalse(page.context["rows"])
        self.assertContains(page, f"PDF comparison · P001 / Final {self.submission.final_submission_id}")
        self.assertContains(page, "data-upload-comparison")

    def test_auto_comparison_does_not_change_publication_exports_or_readiness(self):
        from django.core.files.base import ContentFile
        from submissions.services.checks import publication_readiness_rows
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import export_publication_package
        import zipfile
        original, after = reflow_pair(extra_pages=6)
        self.submission.pdf_file.save("publication-original.pdf", ContentFile(original))
        result, entry = self.receipt(after)
        self.mark_submission_publication_ready(result.submission)
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        readiness = publication_readiness_rows(strict_hash=True)
        self.build(entry)
        with zipfile.ZipFile(export_publication_package()) as archive:
            name = next(name for name in archive.namelist() if name.startswith("PDF/"))
            self.assertEqual(archive.read(name), after)
        with zipfile.ZipFile(prepare_crosscheck_upload("AUTOCOMPARE")["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_AUTOCOMPARE.pdf"), after)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)
