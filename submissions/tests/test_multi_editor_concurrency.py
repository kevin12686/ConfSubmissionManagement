import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from submissions.forms import AppSettingForm, FinalSubmissionForm
from submissions.models import AppSetting, FinalSubmission
from submissions.services.checks import publication_readiness_rows
from submissions.services.editor_uploads import discard_submission
from submissions.services.exceptions import (
    approve_exception,
    exception_rows_for_submission,
)
from submissions.services.manual_edit import apply_final_submission_manual_edit
from submissions.services.organized_list import organized_list_rows
from submissions.services.pdf_processor import process_all_pdfs
from submissions.services.recompute import recompute_active_and_duplicate_state
from submissions.services.reports import export_publication_package
from submissions.services.title_author_extraction import (
    extract_title_author_for_submission,
    extract_title_author_with_grobid,
)
from submissions.services.verification import mark_not_publishing, undo_not_publishing
from submissions.services.workflow_evidence import (
    final_submission_edit_evidence,
    final_submission_state_evidence,
    make_evidence_token,
    submission_group_evidence,
)
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase


class MultiEditorConcurrencyTests(EditorialAcceptanceTestCase):
    def _settings_form_data(self, settings_obj, evidence_token, **overrides):
        data = {
            "action": "save_settings",
            "evidence_token": evidence_token,
            "conference_name": settings_obj.conference_name,
            "page_minimum": settings_obj.page_minimum,
            "page_limit": settings_obj.page_limit,
            "author_paper_limit": settings_obj.author_paper_limit,
            "max_authors_per_paper": settings_obj.max_authors_per_paper,
            "title_words_for_filename": settings_obj.title_words_for_filename,
            "active_version_rule": settings_obj.active_version_rule,
            "time_zone": settings_obj.time_zone,
            "publication_pdf_debug_folder": (
                settings_obj.publication_pdf_debug_folder
            ),
            "reports_folder": settings_obj.reports_folder,
            "extraction_results_folder": settings_obj.extraction_results_folder,
            "plagiarism_reports_folder": settings_obj.plagiarism_reports_folder,
            "grobid_enabled": settings_obj.grobid_enabled,
            "grobid_api_url": settings_obj.grobid_api_url,
            "grobid_timeout_seconds": settings_obj.grobid_timeout_seconds,
            "plagiarism_percent_threshold": (
                settings_obj.plagiarism_percent_threshold
            ),
            "single_similarity_threshold": (
                settings_obj.single_similarity_threshold
            ),
        }
        data.update(overrides)
        return data

    def _form_data(self, submission, **overrides):
        data = {
            "final_submission_id": submission.final_submission_id,
            "start2_paper_id_raw": submission.start2_paper_id_raw,
            "paper_id_filled": submission.paper_id_filled,
            "final_submission_title": submission.final_submission_title,
            "final_submission_authors": submission.final_submission_authors,
            "upload_date": submission.upload_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "similarity_score": submission.similarity_score,
            "single_similarity_score": submission.single_similarity_score,
        }
        data.update(overrides)
        return data

    def test_final_edit_rejects_cross_domain_change_after_form_validation(self):
        self.make_master_paper("P001", "Locked Edit", "Ada")
        submission = self.make_final_submission(
            final_submission_id="LOCK-1",
            paper_id_filled="P001",
            final_submission_title="Locked Edit",
            extracted_title="Locked Edit",
            format_status="review_ok",
            source_hash="original-source-hash",
        )
        page = self.client.get(
            reverse("submissions:final_submission_edit", args=[submission.pk])
        )
        token = page.context["evidence_token"]
        form = FinalSubmissionForm(
            self._form_data(
                submission,
                final_submission_authors="Editor A metadata change",
            ),
            instance=submission,
        )
        self.assertTrue(form.is_valid(), form.errors)

        editor_b = FinalSubmission.objects.get(pk=submission.pk)
        editor_b.formatted_pdf_file.save(
            "editor-b-corrected.pdf",
            ContentFile(b"editor-b-corrected"),
            save=False,
        )
        editor_b.format_status = "needs_edit"
        editor_b.source_hash = ""
        editor_b.save()

        with self.assertRaisesRegex(ValueError, "record changed"):
            apply_final_submission_manual_edit(
                submission,
                form,
                expected_evidence_token=token,
            )

        submission.refresh_from_db()
        self.assertEqual(submission.final_submission_authors, "Ada Lovelace; Alan Turing")
        self.assertEqual(submission.format_status, "needs_edit")
        self.assertEqual(submission.source_hash, "")
        self.assertTrue(submission.formatted_pdf_file)
        self.assertIn(
            "Formatting Not Review OK",
            {row["category"] for row in publication_readiness_rows()},
        )

    def test_stale_settings_form_cannot_overwrite_newer_editor_values(self):
        settings_obj = AppSetting.load()
        page = self.client.get(reverse("submissions:settings"))
        stale_token = page.context["settings_evidence_token"]

        AppSetting.objects.filter(pk=settings_obj.pk).update(page_limit=20)
        response = self.client.post(
            reverse("submissions:settings"),
            self._settings_form_data(
                settings_obj,
                stale_token,
                plagiarism_percent_threshold="40.00",
            ),
            follow=True,
        )

        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.page_limit, 20)
        self.assertEqual(str(settings_obj.plagiarism_percent_threshold), "35.00")
        self.assertContains(response, "record changed after this page was loaded")

    def test_normal_settings_save_does_not_scan_final_submissions(self):
        FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=f"SETTINGS-PERF-{index}",
                    paper_id_filled=f"P{index:04d}",
                )
                for index in range(250)
            ]
        )
        settings_obj = AppSetting.load()
        page = self.client.get(reverse("submissions:settings"))
        token = page.context["settings_evidence_token"]

        with CaptureQueriesContext(connection) as context:
            response = self.client.post(
                reverse("submissions:settings"),
                self._settings_form_data(
                    settings_obj,
                    token,
                    page_limit=15,
                ),
            )

        self.assertEqual(response.status_code, 302)
        final_table = FinalSubmission._meta.db_table
        self.assertFalse(
            any(final_table in query["sql"] for query in context.captured_queries)
        )
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.page_limit, 15)

    def test_process_pdfs_does_not_overwrite_concurrent_editor_change(self):
        self.make_master_paper("P001", "Concurrent Processing", "Ada")
        submission = self.make_final_submission(
            final_submission_id="PROCESS-RACE",
            paper_id_filled="P001",
            final_submission_title="Concurrent Processing",
            extracted_title="Concurrent Processing",
            format_status="pending",
            processing_status="pending",
            processing_message="Waiting for processing.",
            source_hash="",
        )

        def process_while_editor_saves(candidate, force=False, save=True):
            FinalSubmission.objects.filter(pk=candidate.pk).update(
                format_status="review_ok",
                format_notes="Editor B completed review.",
                processing_message="Editor B preserved this message.",
                source_hash="editor-b-source-hash",
            )
            candidate.processing_status = "error"
            candidate.processing_message = "Stale batch error."
            candidate.format_status = "pending"
            candidate.source_hash = ""
            return False

        with patch(
            "submissions.services.pdf_processor.process_submission_pdf",
            side_effect=process_while_editor_saves,
        ), patch(
            "submissions.services.pdf_processor.sync_debug_publication_files",
            return_value={
                "synced_count": 0,
                "skipped_count": 0,
                "manifest_path": "",
            },
        ):
            result = process_all_pdfs()

        submission.refresh_from_db()
        self.assertEqual(result["concurrent_skipped"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(submission.format_status, "review_ok")
        self.assertEqual(
            submission.format_notes,
            "Editor B completed review.",
        )
        self.assertEqual(
            submission.processing_message,
            "Editor B preserved this message.",
        )
        self.assertEqual(submission.source_hash, "editor-b-source-hash")

    def test_process_pdfs_stale_batch_cannot_overwrite_newer_thumbnails(self):
        self.make_master_paper("P001", "Concurrent Thumbnails", "Ada")
        submission = self.make_final_submission(
            final_submission_id="THUMB-RACE",
            paper_id_filled="P001",
            final_submission_title="Concurrent Thumbnails",
            extracted_title="Concurrent Thumbnails",
            processing_status="pending",
        )
        stale_folder = self.media_root / "pdf_thumbnails" / "stale-result"
        newer_folder = self.media_root / "pdf_thumbnails" / "newer-result"

        def process_while_newer_result_saves(candidate, force=False, save=True):
            stale_folder.mkdir(parents=True)
            (stale_folder / "page-1.png").write_bytes(b"stale thumbnail")
            newer_folder.mkdir(parents=True)
            (newer_folder / "page-1.png").write_bytes(b"newer thumbnail")
            FinalSubmission.objects.filter(pk=candidate.pk).update(
                thumbnail_folder=str(newer_folder),
                thumbnail_status="processed",
                thumbnail_message="Newer processing result.",
            )
            candidate.thumbnail_folder = str(stale_folder)
            candidate.thumbnail_status = "processed"
            candidate.thumbnail_message = "Stale processing result."
            candidate.processing_status = "processed"
            return True

        with patch(
            "submissions.services.pdf_processor.process_submission_pdf",
            side_effect=process_while_newer_result_saves,
        ), patch(
            "submissions.services.pdf_processor.sync_debug_publication_files",
            return_value={
                "synced_count": 0,
                "skipped_count": 0,
                "manifest_path": "",
            },
        ):
            result = process_all_pdfs()

        submission.refresh_from_db()
        self.assertEqual(result["concurrent_skipped"], 1)
        self.assertEqual(submission.thumbnail_folder, str(newer_folder))
        self.assertEqual(
            (newer_folder / "page-1.png").read_bytes(),
            b"newer thumbnail",
        )
        self.assertFalse(stale_folder.exists())

    def test_builtin_extraction_does_not_overwrite_concurrent_review(self):
        self.make_master_paper("P001", "Concurrent Extraction", "Ada")
        submission = self.make_final_submission(
            final_submission_id="EXTRACT-RACE",
            paper_id_filled="P001",
            final_submission_title="Concurrent Extraction",
            extracted_title="Old extraction",
            extracted_authors="Old Author",
            title_author_review_status="pending",
        )
        old_image = (
            self.media_root
            / "title_author_verification"
            / "EXTRACT-RACE"
            / "old.png"
        )
        old_image.parent.mkdir(parents=True, exist_ok=True)
        old_image.write_bytes(b"editor-b-evidence")
        submission.title_author_verification_image = str(old_image)
        submission.save(
            update_fields=[
                "title_author_verification_image",
                "updated_at",
            ]
        )

        def extract_while_editor_reviews(*args, **kwargs):
            FinalSubmission.objects.filter(pk=submission.pk).update(
                extracted_title="Editor B title",
                extracted_authors="Editor B author",
                title_author_review_status="review_ok",
                title_author_verified=True,
            )
            return "Stale title", "Stale author", 1

        def make_staged_image(
            _pdf_path,
            _title,
            _authors,
            _source,
            target_dir,
        ):
            path = Path(target_dir) / "stale.png"
            path.write_bytes(b"stale extraction evidence")
            return path, []

        with patch(
            "submissions.services.title_author_extraction.get_title_author",
            side_effect=extract_while_editor_reviews,
        ), patch(
            "submissions.services.title_author_extraction.generate_text_verification_image",
            side_effect=make_staged_image,
        ):
            result = extract_title_author_for_submission(submission)

        self.assertFalse(result)
        submission.refresh_from_db()
        self.assertEqual(submission.extracted_title, "Editor B title")
        self.assertEqual(submission.extracted_authors, "Editor B author")
        self.assertEqual(submission.title_author_review_status, "review_ok")
        self.assertEqual(
            submission.title_author_verification_image,
            str(old_image),
        )
        self.assertEqual(old_image.read_bytes(), b"editor-b-evidence")

    def test_grobid_extraction_does_not_overwrite_concurrent_review(self):
        self.make_master_paper("P001", "Concurrent GROBID", "Ada")
        submission = self.make_final_submission(
            final_submission_id="GROBID-RACE",
            paper_id_filled="P001",
            final_submission_title="Concurrent GROBID",
            extracted_title="Old extraction",
            extracted_authors="Old Author",
            title_author_review_status="red_flag",
        )
        setting = AppSetting.load()
        setting.grobid_enabled = True
        setting.save(update_fields=["grobid_enabled"])

        def extract_while_editor_reviews(*args, **kwargs):
            FinalSubmission.objects.filter(pk=submission.pk).update(
                extracted_title="Editor B GROBID title",
                extracted_authors="Editor B GROBID author",
                title_author_review_status="review_ok",
                title_author_verified=True,
            )
            return SimpleNamespace(
                title="Stale GROBID title",
                authors="Stale GROBID author",
                author_count=1,
            )

        def make_staged_image(
            _pdf_path,
            _title,
            _authors,
            _source,
            target_dir,
        ):
            path = Path(target_dir) / "stale-grobid.png"
            path.write_bytes(b"stale GROBID evidence")
            return path, []

        with patch(
            "submissions.services.title_author_extraction.extract_header_with_grobid",
            side_effect=extract_while_editor_reviews,
        ), patch(
            "submissions.services.title_author_extraction.generate_text_verification_image",
            side_effect=make_staged_image,
        ):
            result = extract_title_author_with_grobid(
                submission,
                skip_health_check=True,
            )

        self.assertFalse(result)
        submission.refresh_from_db()
        self.assertEqual(
            submission.extracted_title,
            "Editor B GROBID title",
        )
        self.assertEqual(
            submission.extracted_authors,
            "Editor B GROBID author",
        )
        self.assertEqual(submission.title_author_review_status, "review_ok")

    def test_settings_form_rejects_managed_roots_but_allows_dedicated_folder(self):
        settings_obj = AppSetting.load()
        base_data = self._settings_form_data(settings_obj, "")
        base_data.pop("action")
        base_data.pop("evidence_token")

        for unsafe_path in (".", "data", "data/logs"):
            with self.subTest(unsafe_path=unsafe_path):
                form = AppSettingForm(
                    {
                        **base_data,
                        "reports_folder": unsafe_path,
                    },
                    instance=settings_obj,
                )
                self.assertFalse(form.is_valid())
                self.assertIn("reports_folder", form.errors)

        form = AppSettingForm(
            {
                **base_data,
                "reports_folder": "data/reports",
            },
            instance=settings_obj,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_paper_id_stale_verify_is_rejected_but_current_title_diff_can_be_verified(self):
        self.make_master_paper("P001", "Accepted Title", "Ada")
        submission = self.make_final_submission(
            final_submission_id="VERIFY-1",
            paper_id_filled="P001",
            final_submission_title="Accepted Title",
            extracted_title="Accepted Title",
            paper_id_verified=False,
            verification_status="pending",
        )
        page = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"submission": submission.pk},
        )
        stale_token = page.context["rows"][0]["paper_id_evidence_token"]

        submission.final_submission_title = "Author Revised Title"
        submission.extracted_title = "Author Revised Title"
        submission.paper_id_verified = False
        submission.verification_status = "title_mismatch"
        submission.save(
            update_fields=[
                "final_submission_title",
                "extracted_title",
                "paper_id_verified",
                "verification_status",
                "updated_at",
            ]
        )
        response = self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "corrected_paper_id": "P001",
                "action": "verify",
                "evidence_token": stale_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)

        current_page = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"submission": submission.pk},
        )
        current_token = current_page.context["rows"][0]["paper_id_evidence_token"]
        self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "corrected_paper_id": "P001",
                "action": "verify",
                "evidence_token": current_token,
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "verified")
        self.assertIn("title differs", submission.verification_message)

    def test_use_suggestion_rejects_stale_paper_master_catalog(self):
        self.make_master_paper("P001", "Related Submission", "Ada")
        submission = self.make_final_submission(
            final_submission_id="SUGGEST-1",
            paper_id_filled="UNKNOWN",
            final_submission_title="Exact New Master Title",
            extracted_title="Exact New Master Title",
            paper_id_verified=False,
            verification_status="invalid_paper_id",
        )
        page = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"submission": submission.pk},
        )
        stale_token = page.context["rows"][0]["paper_id_evidence_token"]
        self.make_master_paper("P002", "Exact New Master Title", "Grace")

        self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "action": "use_suggestion",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.paper_id_filled, "UNKNOWN")
        self.assertFalse(submission.paper_id_verified)

        current_page = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"submission": submission.pk},
        )
        current_row = current_page.context["rows"][0]
        self.assertEqual(current_row["suggested_paper"].paper_id, "P002")
        self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "action": "use_suggestion",
                "evidence_token": current_row["paper_id_evidence_token"],
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.paper_id_filled, "P002")
        self.assertTrue(submission.paper_id_verified)

    def test_stale_unverify_cannot_reverse_newer_paper_id_review(self):
        self.make_master_paper("P001", "Review One", "Ada")
        submission = self.make_final_submission(
            final_submission_id="UNVERIFY-1",
            paper_id_filled="P001",
            final_submission_title="Review One",
            extracted_title="Review One",
        )
        page = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"submission": submission.pk},
        )
        stale_token = page.context["rows"][0]["paper_id_unverify_evidence_token"]

        submission.verification_message = "Editor B reviewed the current record."
        submission.save(update_fields=["verification_message", "updated_at"])
        self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "action": "unverify",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(
            submission.verification_message,
            "Editor B reviewed the current record.",
        )

    def test_stale_not_publishing_page_cannot_exclude_remapped_group(self):
        self.make_master_paper("P001", "First", "Ada")
        self.make_master_paper("P002", "Second", "Grace")
        target = self.make_final_submission(
            final_submission_id="NP-1",
            paper_id_filled="P001",
            final_submission_title="First",
            extracted_title="First",
        )
        second = self.make_final_submission(
            final_submission_id="NP-2",
            paper_id_filled="P002",
            final_submission_title="Second",
            extracted_title="Second",
        )
        page = self.client.get(
            reverse("submissions:not_publishing_list"),
            {"submission": target.pk},
        )
        stale_token = page.context[
            "focused_submission"
        ].publication_decision_evidence_token

        target.paper_id_filled = "P002"
        target.paper_id_verified = False
        target.save(
            update_fields=[
                "paper_id_filled",
                "paper_id_verified",
                "updated_at",
            ]
        )
        self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": target.pk,
                "action": "mark_not_publishing",
                "publication_exclusion_reason": "withdrawn",
                "evidence_token": stale_token,
            },
        )
        target.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(target.excluded_from_publication)
        self.assertFalse(second.excluded_from_publication)

    def test_excluded_submission_requires_undo_before_paper_id_remap(self):
        self.make_master_paper("P001", "Excluded", "Ada")
        self.make_master_paper("P002", "Destination", "Grace")
        submission = self.make_final_submission(
            final_submission_id="NP-REMAP",
            paper_id_filled="P001",
            final_submission_title="Excluded",
            extracted_title="Excluded",
        )
        mark_not_publishing(submission, "withdrawn", "Author withdrew.")
        submission.refresh_from_db()
        token = make_evidence_token(
            "final-submission-edit",
            final_submission_edit_evidence(submission),
        )
        form = FinalSubmissionForm(
            self._form_data(submission, paper_id_filled="P002"),
            instance=submission,
        )
        self.assertTrue(form.is_valid(), form.errors)

        with self.assertRaisesRegex(ValueError, "Undo Not Publishing"):
            apply_final_submission_manual_edit(
                submission,
                form,
                expected_evidence_token=token,
            )
        submission.refresh_from_db()
        self.assertEqual(submission.paper_id_filled, "P001")
        self.assertTrue(submission.excluded_from_publication)

        undo_not_publishing(submission)
        submission.refresh_from_db()
        remap_token = make_evidence_token(
            "final-submission-edit",
            final_submission_edit_evidence(submission),
        )
        remap_form = FinalSubmissionForm(
            self._form_data(submission, paper_id_filled="P002"),
            instance=submission,
        )
        self.assertTrue(remap_form.is_valid(), remap_form.errors)
        apply_final_submission_manual_edit(
            submission,
            remap_form,
            expected_evidence_token=remap_token,
        )
        submission.refresh_from_db()
        self.assertEqual(submission.paper_id_filled, "P002")
        self.assertFalse(submission.excluded_from_publication)
        self.assertFalse(submission.paper_id_verified)

    def test_stale_undo_not_publishing_keeps_newer_group_decision(self):
        self.make_master_paper("P001", "Withdrawn", "Ada")
        submission = self.make_final_submission(
            final_submission_id="NP-UNDO",
            paper_id_filled="P001",
            final_submission_title="Withdrawn",
            extracted_title="Withdrawn",
        )
        mark_not_publishing(submission, "withdrawn", "Initial decision.")
        page = self.client.get(
            reverse("submissions:not_publishing_list"),
            {"submission": submission.pk},
        )
        stale_token = page.context[
            "focused_submission"
        ].publication_decision_evidence_token

        submission.refresh_from_db()
        submission.publication_exclusion_notes = "Editor B confirmed withdrawal."
        submission.save(
            update_fields=["publication_exclusion_notes", "updated_at"]
        )
        self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": submission.pk,
                "action": "undo_not_publishing",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.excluded_from_publication)
        self.assertEqual(
            submission.publication_exclusion_notes,
            "Editor B confirmed withdrawal.",
        )

    def test_stale_duplicate_author_review_cannot_approve_new_author_list(self):
        self.make_master_paper("P001", "Duplicates", "Ada")
        submission = self.make_final_submission(
            final_submission_id="DUP-1",
            paper_id_filled="P001",
            final_submission_title="Duplicates",
            extracted_title="Duplicates",
            extracted_authors="Ada Lovelace; Ada Lovelace",
            duplicate_author_review_status="pending",
        )
        page = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        row = next(
            item
            for item in page.context["rows"]
            if item.get("submission") and item["submission"].pk == submission.pk
        )
        stale_token = row["duplicate_author_evidence_token"]

        submission.extracted_authors = "Grace Hopper; Grace Hopper"
        submission.duplicate_author_review_status = "pending"
        submission.save(
            update_fields=[
                "extracted_authors",
                "duplicate_author_review_status",
                "updated_at",
            ]
        )
        self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": submission.pk,
                "action": "mark_duplicate_author_reviewed",
                "duplicate_author_review_notes": "Reviewed Ada.",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.duplicate_author_review_status, "pending")
        self.assertIn(
            "Duplicate Author In Paper",
            {row["category"] for row in publication_readiness_rows()},
        )

    def test_stale_duplicate_author_reset_cannot_reverse_newer_review(self):
        self.make_master_paper("P001", "Duplicate Reset", "Ada")
        submission = self.make_final_submission(
            final_submission_id="DUP-RESET",
            paper_id_filled="P001",
            final_submission_title="Duplicate Reset",
            extracted_title="Duplicate Reset",
            extracted_authors="Ada Lovelace; Ada Lovelace",
            duplicate_author_review_status="review_ok",
        )
        page = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        row = next(
            item
            for item in page.context["rows"]
            if item.get("submission") and item["submission"].pk == submission.pk
        )
        stale_token = row["duplicate_author_evidence_token"]

        submission.extracted_authors = "Grace Hopper; Grace Hopper"
        submission.duplicate_author_review_notes = "Editor B reviewed Grace."
        submission.save(
            update_fields=[
                "extracted_authors",
                "duplicate_author_review_notes",
                "updated_at",
            ]
        )
        self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": submission.pk,
                "action": "reset_duplicate_author_review",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.duplicate_author_review_status, "review_ok")
        self.assertEqual(
            submission.duplicate_author_review_notes,
            "Editor B reviewed Grace.",
        )

    def test_stale_manual_override_cannot_replace_newer_editor_metadata(self):
        self.make_master_paper("P001", "Override", "Ada")
        submission = self.make_final_submission(
            final_submission_id="OVERRIDE-1",
            paper_id_filled="P001",
            final_submission_title="Override",
            extracted_title="Old title",
            extracted_authors="Old author",
        )
        page = self.client.get(
            reverse(
                "submissions:title_author_manual_override_form",
                args=[submission.pk],
            )
        )
        stale_token = page.context["evidence_token"]

        submission.extracted_title = "Editor B title"
        submission.extracted_authors = "Editor B author"
        submission.save(
            update_fields=[
                "extracted_title",
                "extracted_authors",
                "updated_at",
            ]
        )
        self.client.post(
            reverse("submissions:title_author_extraction"),
            {
                "submission_id": submission.pk,
                "action": "manual_override",
                "manual_extracted_title": "Editor A title",
                "manual_extracted_authors": "Editor A author",
                "manual_override_reason": "Editor A stale form.",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.extracted_title, "Editor B title")
        self.assertEqual(submission.extracted_authors, "Editor B author")

    def test_author_number_exception_evidence_binds_normalized_author_list(self):
        settings_obj = AppSetting.load()
        settings_obj.max_authors_per_paper = 1
        settings_obj.save(update_fields=["max_authors_per_paper"])
        submission = self.make_final_submission(
            final_submission_id="AUTH-1",
            extracted_authors="Ada Lovelace; Alan Turing",
        )
        old_row = next(
            row
            for row in exception_rows_for_submission(submission)
            if row["type"] == "author_number"
        )

        submission.extracted_authors = "Grace Hopper; Katherine Johnson"
        submission.save(update_fields=["extracted_authors", "updated_at"])
        with self.assertRaisesRegex(ValueError, "record changed"):
            approve_exception(
                old_row,
                "Approved old author list.",
                expected_evidence_token=old_row["evidence_token"],
            )
        submission.refresh_from_db()
        self.assertFalse(submission.author_number_exception_approved)

    def test_stale_discard_cannot_change_candidate_and_package_uses_new_bytes(self):
        self.make_master_paper("P001", "Concurrent Version", "Ada")
        pdf_v10 = self.make_pdf_file("concurrent-v10.pdf", b"PDF-V10")
        source_v10 = self.make_source_file("concurrent-v10.docx", b"SOURCE-V10")
        v10 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Concurrent Version",
            extracted_title="Concurrent Version",
            current_file_path=str(pdf_v10),
            source_current_file_path=str(source_v10),
        )
        stale_token = make_evidence_token(
            "version-decision",
            submission_group_evidence(v10, [v10]),
        )

        pdf_v11 = self.make_pdf_file("concurrent-v11.pdf", b"PDF-V11")
        source_v11 = self.make_source_file("concurrent-v11.docx", b"SOURCE-V11")
        v11 = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="Concurrent Version",
            extracted_title="Concurrent Version",
            current_file_path=str(pdf_v11),
            source_current_file_path=str(source_v11),
        )
        recompute_active_and_duplicate_state()

        with self.assertRaisesRegex(ValueError, "record changed"):
            discard_submission(
                v10,
                "Stale editor decision.",
                expected_evidence_token=stale_token,
            )
        v10.refresh_from_db()
        v11.refresh_from_db()
        self.assertFalse(v10.discarded)
        self.assertTrue(v11.active_version)

        rows, _summary, _settings, _filter, _sort = organized_list_rows()
        organized = next(row for row in rows if row["paper"].paper_id == "P001")
        self.assertEqual(organized["submission"].pk, v11.pk)
        self.assertEqual(
            Path(organized["publication_pdf"]["path"]).read_bytes(),
            b"PDF-V11",
        )
        self.assertEqual(
            Path(organized["publication_source"]["path"]).read_bytes(),
            b"SOURCE-V11",
        )

        package = export_publication_package()
        with zipfile.ZipFile(package) as archive:
            pdf_name = next(name for name in archive.namelist() if name.startswith("PDF/"))
            source_name = next(
                name for name in archive.namelist() if name.startswith("Source/")
            )
            self.assertEqual(archive.read(pdf_name), b"PDF-V11")
            self.assertEqual(archive.read(source_name), b"SOURCE-V11")
            self.assertNotEqual(archive.read(pdf_name), b"PDF-V10")

    def test_stale_undo_discard_cannot_reverse_newer_version_decision(self):
        self.make_master_paper("P001", "Discard Undo", "Ada")
        submission = self.make_final_submission(
            final_submission_id="DISCARD-UNDO",
            paper_id_filled="P001",
            final_submission_title="Discard Undo",
            extracted_title="Discard Undo",
        )
        discard_submission(submission, "Initial discard.")
        page = self.client.get(reverse("submissions:final_submission_list"))
        listed = next(
            item
            for item in page.context["submissions"]
            if item.pk == submission.pk
        )
        stale_token = listed.version_decision_evidence_token

        submission.refresh_from_db()
        submission.discard_notes = "Editor B confirmed discard."
        submission.save(update_fields=["discard_notes", "updated_at"])
        self.client.post(
            reverse("submissions:final_submission_list"),
            {
                "submission_id": submission.pk,
                "action": "undo_discard_submission",
                "evidence_token": stale_token,
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.discarded)
        self.assertEqual(submission.discard_notes, "Editor B confirmed discard.")

    def test_evidence_digest_generation_has_no_database_or_file_io(self):
        submissions = [
            self.make_final_submission(
                final_submission_id=f"PERF-{index}",
                paper_id_filled=f"P{index:03}",
            )
            for index in range(1, 26)
        ]
        with self.assertNumQueries(0):
            for submission in submissions:
                state = final_submission_state_evidence(submission)
                make_evidence_token("performance-probe", state)
                make_evidence_token(
                    "performance-group-probe",
                    submission_group_evidence(submission, [submission]),
                )

    def test_missing_signed_evidence_fails_closed(self):
        self.make_master_paper("P001", "Evidence Required", "Ada")
        submission = self.make_final_submission(
            final_submission_id="NO-TOKEN",
            paper_id_filled="P001",
            final_submission_title="Evidence Required",
            extracted_title="Evidence Required",
            paper_id_verified=False,
            verification_status="pending",
        )
        self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "action": "verify",
                "corrected_paper_id": "P001",
            },
        )
        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)

        self.client.post(
            reverse("submissions:final_submission_list"),
            {
                "submission_id": submission.pk,
                "action": "discard_submission",
                "discard_notes": "Unsigned stale action.",
            },
        )
        submission.refresh_from_db()
        self.assertFalse(submission.discarded)

        submission.extracted_authors = "Ada Lovelace; Ada Lovelace"
        submission.duplicate_author_review_status = "pending"
        submission.save(
            update_fields=[
                "extracted_authors",
                "duplicate_author_review_status",
                "updated_at",
            ]
        )
        self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": submission.pk,
                "action": "mark_duplicate_author_reviewed",
                "duplicate_author_review_notes": "Unsigned review.",
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.duplicate_author_review_status, "pending")

    def test_fresh_evidence_allows_guarded_editor_action_sequence(self):
        self.make_master_paper("P001", "Action Sequence", "Ada")
        submission = self.make_final_submission(
            final_submission_id="SEQUENCE-1",
            paper_id_filled="P001",
            final_submission_title="Action Sequence",
            extracted_title="Action Sequence",
            extracted_authors="Ada Lovelace; Ada Lovelace",
            duplicate_author_review_status="pending",
        )

        organized = self.client.get(
            reverse("submissions:organized_list"),
            {"filter": "all"},
        )
        row = next(
            item
            for item in organized.context["rows"]
            if item.get("submission") and item["submission"].pk == submission.pk
        )
        self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": submission.pk,
                "action": "mark_duplicate_author_reviewed",
                "duplicate_author_review_notes": "Confirmed names.",
                "evidence_token": row["duplicate_author_evidence_token"],
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.duplicate_author_review_status, "review_ok")

        final_list = self.client.get(reverse("submissions:final_submission_list"))
        listed = next(
            item
            for item in final_list.context["submissions"]
            if item.pk == submission.pk
        )
        self.client.post(
            reverse("submissions:final_submission_list"),
            {
                "submission_id": submission.pk,
                "action": "discard_submission",
                "discard_notes": "Temporary version decision.",
                "evidence_token": listed.version_decision_evidence_token,
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.discarded)

        final_list = self.client.get(reverse("submissions:final_submission_list"))
        listed = next(
            item
            for item in final_list.context["submissions"]
            if item.pk == submission.pk
        )
        self.client.post(
            reverse("submissions:final_submission_list"),
            {
                "submission_id": submission.pk,
                "action": "undo_discard_submission",
                "evidence_token": listed.version_decision_evidence_token,
            },
        )
        submission.refresh_from_db()
        self.assertFalse(submission.discarded)

        not_publishing = self.client.get(
            reverse("submissions:not_publishing_list"),
            {"submission": submission.pk},
        )
        focused = not_publishing.context["focused_submission"]
        self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": submission.pk,
                "action": "mark_not_publishing",
                "publication_exclusion_reason": "withdrawn",
                "publication_exclusion_notes": "Temporary decision.",
                "evidence_token": focused.publication_decision_evidence_token,
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.excluded_from_publication)

        not_publishing = self.client.get(
            reverse("submissions:not_publishing_list"),
            {"submission": submission.pk},
        )
        focused = not_publishing.context["focused_submission"]
        self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": submission.pk,
                "action": "undo_not_publishing",
                "evidence_token": focused.publication_decision_evidence_token,
            },
        )
        submission.refresh_from_db()
        self.assertFalse(submission.excluded_from_publication)
        self.assertFalse(submission.paper_id_verified)

        verify_page = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"submission": submission.pk},
        )
        verify_row = verify_page.context["rows"][0]
        self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "action": "verify",
                "corrected_paper_id": "P001",
                "evidence_token": verify_row["paper_id_evidence_token"],
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)

        override_form = self.client.get(
            reverse(
                "submissions:title_author_manual_override_form",
                args=[submission.pk],
            )
        )
        self.client.post(
            reverse("submissions:title_author_extraction"),
            {
                "submission_id": submission.pk,
                "action": "manual_override",
                "manual_extracted_title": "Action Sequence",
                "manual_extracted_authors": "Ada Lovelace",
                "manual_override_reason": "Confirmed against current PDF.",
                "evidence_token": override_form.context["evidence_token"],
            },
        )
        submission.refresh_from_db()
        self.assertEqual(submission.title_author_source, "manual_override")
        self.assertEqual(submission.extracted_authors, "Ada Lovelace")
