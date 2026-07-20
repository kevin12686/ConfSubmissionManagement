import hashlib
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from submissions.forms import FinalSubmissionForm
from submissions.models import AppSetting, FinalSubmission, PaperAuthor
from submissions.services.checks import (
    publication_readiness_rows,
    rebuild_paper_authors,
)
from submissions.services.editor_uploads import editor_conflict_paper_ids
from submissions.services.organized_list import organized_list_rows
from submissions.services.manual_edit import apply_final_submission_manual_edit
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.publication_read import PublicationReadContext
from submissions.services.reports import (
    PublicationPackageBlocked,
    export_publication_package,
)
from submissions.services.title_author_extraction import (
    set_title_author_review_status,
)
from submissions.services.verification import (
    mark_not_publishing,
    undo_not_publishing,
)
from submissions.services.workflow_evidence import (
    final_submission_edit_evidence,
    make_evidence_token,
)
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase


class PublicationSafetyRegressionTests(EditorialAcceptanceTestCase):
    def test_active_version_uses_all_numeric_chunks_in_final_id(self):
        self.make_master_paper("P001", "Natural Final IDs", "Ada")
        lower = self.make_final_submission(
            final_submission_id="A9B3",
            final_submission_title="Natural Final IDs",
        )
        higher = self.make_final_submission(
            final_submission_id="A10B2",
            final_submission_title="Natural Final IDs",
        )

        determine_active_versions()

        lower.refresh_from_db()
        higher.refresh_from_db()
        self.assertFalse(lower.active_version)
        self.assertTrue(higher.active_version)

    def test_not_publishing_updates_every_version_for_the_paper(self):
        self.make_master_paper("P001", "Withdrawn Paper", "Ada")
        self.make_master_paper("P002", "Publishing Paper", "Grace")
        start2 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Withdrawn Paper",
        )
        editor = self.make_final_submission(
            final_submission_id="EDITOR-P001-001",
            paper_id_filled="P001",
            final_submission_title="Withdrawn Paper",
            submission_origin="editor_upload",
        )
        self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P002",
            final_submission_title="Publishing Paper",
            extracted_title="Publishing Paper",
        )
        determine_active_versions()
        start2.refresh_from_db()
        self.assertFalse(start2.active_version)
        self.assertIn("P001", editor_conflict_paper_ids())

        mark_not_publishing(start2, "withdrawn", "Paper-level decision.")

        start2.refresh_from_db()
        editor.refresh_from_db()
        self.assertTrue(start2.excluded_from_publication)
        self.assertTrue(editor.excluded_from_publication)
        self.assertNotIn("P001", editor_conflict_paper_ids())
        self.assertEqual(publication_readiness_rows(), [])
        with zipfile.ZipFile(export_publication_package()) as archive:
            self.assertNotIn("PDF/P001-Withdrawn Paper.pdf", archive.namelist())
            self.assertIn("PDF/P002-Publishing Paper.pdf", archive.namelist())

        undo_not_publishing(editor)
        start2.refresh_from_db()
        editor.refresh_from_db()
        self.assertFalse(start2.excluded_from_publication)
        self.assertFalse(editor.excluded_from_publication)
        self.assertFalse(start2.paper_id_verified)
        self.assertFalse(editor.paper_id_verified)

    def test_mixed_not_publishing_state_blocks_final_and_draft_packages(self):
        self.make_master_paper("P001", "Mixed Decision", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            final_submission_title="Mixed Decision",
            excluded_from_publication=True,
            active_version=False,
        )
        active = self.make_final_submission(
            final_submission_id="11",
            final_submission_title="Mixed Decision",
            active_version=True,
        )

        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertIn("Mixed Not Publishing Decision", categories)
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package()
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package(force=True)

        context = PublicationReadContext.load()
        rows, *_rest = organized_list_rows(context=context, hydrate=False)
        row = next(item for item in rows if item["paper"].paper_id == "P001")
        self.assertTrue(row["publication_decision_conflict"])
        self.assertEqual(row["submission"].pk, active.pk)
        self.assertNotEqual(row["submission"].pk, old.pk)

    def test_mixed_decision_lookup_aggregates_before_loading_details(self):
        self.make_master_paper("P001", "Mixed Decision", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            excluded_from_publication=True,
            active_version=False,
        )
        self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            excluded_from_publication=False,
            active_version=True,
        )
        for index in range(20, 40):
            self.make_final_submission(
                final_submission_id=str(index),
                paper_id_filled=f"P{index}",
                excluded_from_publication=False,
                active_version=False,
            )

        context = PublicationReadContext.load()
        with CaptureQueriesContext(connection) as captured:
            groups = context.mixed_publication_decision_groups

        self.assertEqual(set(groups), {"P001"})
        self.assertEqual(len(captured), 2)
        self.assertIn("COUNT", captured[0]["sql"].upper())
        self.assertIn(" IN ", captured[1]["sql"].upper())

    def test_multiple_active_versions_do_not_display_an_arbitrary_file(self):
        self.make_master_paper("P001", "Ambiguous Active", "Ada")
        first = self.make_final_submission(
            final_submission_id="10",
            final_submission_title="Ambiguous Active",
            active_version=True,
        )
        second = self.make_final_submission(
            final_submission_id="11",
            final_submission_title="Ambiguous Active",
            active_version=True,
        )

        context = PublicationReadContext.load()
        rows, *_rest = organized_list_rows(context=context, hydrate=False)
        row = next(item for item in rows if item["paper"].paper_id == "P001")

        self.assertIsNone(row["submission"])
        self.assertEqual(
            set(row["multiple_active_final_ids"]),
            {first.final_submission_id, second.final_submission_id},
        )
        self.assertEqual(row["verify_label"], "Multiple active finals")

    def test_stale_title_author_review_cannot_approve_changed_metadata(self):
        self.make_master_paper("P001", "Reviewed Title", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Reviewed Title",
            extracted_title="Reviewed Title",
            extracted_authors="Ada Lovelace",
            title_author_review_status="pending",
            title_author_verified=False,
            extracted_title_verified=False,
        )
        page = self.client.get(
            reverse("submissions:title_author_extraction"),
            {"filter": "all"},
        )
        token = next(
            row["evidence_token"]
            for row in page.context["rows"]
            if row["submission"].pk == submission.pk
        )
        submission.extracted_title = "Changed by another editor"
        submission.extracted_authors = "Another Author"
        submission.save(
            update_fields=["extracted_title", "extracted_authors", "updated_at"]
        )

        response = self.client.post(
            reverse("submissions:title_author_extraction"),
            {
                "submission_id": submission.pk,
                "action": "set_review_status",
                "review_status": "review_ok",
                "evidence_token": token,
            },
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertFalse(submission.title_author_verified)

    def test_review_ok_requires_both_title_and_authors(self):
        submission = self.make_final_submission(
            extracted_title="Only Title",
            extracted_authors="",
            title_author_review_status="pending",
        )
        with self.assertRaisesMessage(
            ValueError,
            "requires both an extracted title and extracted authors",
        ):
            set_title_author_review_status(submission, "review_ok")

    def test_stale_plagiarism_exception_cannot_approve_new_score(self):
        self.make_master_paper("P001", "Score Review", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Score Review",
            extracted_title="Score Review",
            similarity_score=40,
            single_similarity_score=1,
        )
        page = self.client.get(
            reverse("submissions:exceptions_center"),
            {"filter": "all"},
        )
        row = next(
            item
            for item in page.context["rows"]
            if item["key"] == f"plagiarism_percent:{submission.pk}"
        )
        submission.similarity_score = 90
        submission.save(update_fields=["similarity_score", "updated_at"])

        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {
                "exception_key": row["key"],
                "action": "approve_exception",
                "reason": "Reviewed old score",
                "evidence_token": row["evidence_token"],
            },
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.plagiarism_percent_exception_approved)
        self.assertIsNone(
            submission.plagiarism_percent_exception_approved_score
        )

    def test_stale_manual_edit_cannot_overwrite_another_editor(self):
        self.make_master_paper("P001", "Original Title", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Original Title",
            extracted_title="Original Title",
        )
        page = self.client.get(
            reverse("submissions:final_submission_edit", args=[submission.pk])
        )
        token = page.context["evidence_token"]
        original_upload_date = submission.upload_date
        submission.final_submission_title = "Newer Editor Title"
        submission.save(update_fields=["final_submission_title", "updated_at"])

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            {
                "evidence_token": token,
                "final_submission_id": submission.final_submission_id,
                "start2_paper_id_raw": submission.start2_paper_id_raw,
                "paper_id_filled": submission.paper_id_filled,
                "final_submission_title": "Original Title",
                "final_submission_authors": submission.final_submission_authors,
                "upload_date": timezone.localtime(original_upload_date).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
                "similarity_score": "1",
                "single_similarity_score": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.final_submission_title, "Newer Editor Title")

    def test_failed_original_file_edit_keeps_reviewed_corrected_files(self):
        self.make_master_paper("P001", "Rollback Files", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Rollback Files",
            extracted_title="Rollback Files",
        )
        submission.formatted_pdf_file.save(
            "reviewed.pdf",
            ContentFile(b"reviewed corrected pdf"),
            save=False,
        )
        submission.formatted_source_file.save(
            "reviewed.docx",
            ContentFile(b"reviewed corrected source"),
            save=False,
        )
        submission.save()
        corrected_pdf_path = Path(submission.formatted_pdf_file.path)
        corrected_source_path = Path(submission.formatted_source_file.path)
        original_pdf_name = submission.pdf_file.name
        token = make_evidence_token(
            "final-submission-edit",
            final_submission_edit_evidence(submission),
        )

        form = FinalSubmissionForm(
            self.final_submission_form_data(submission),
            {
                "pdf_file": SimpleUploadedFile(
                    "replacement.pdf",
                    b"replacement original pdf",
                    content_type="application/pdf",
                ),
            },
            instance=submission,
        )
        self.assertTrue(form.is_valid(), form.errors)

        with patch(
            "submissions.services.manual_edit.rebuild_paper_authors",
            side_effect=RuntimeError("injected downstream failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected downstream failure",
            ):
                apply_final_submission_manual_edit(
                    submission,
                    form,
                    expected_evidence_token=token,
                )

        submission.refresh_from_db()
        self.assertEqual(submission.pdf_file.name, original_pdf_name)
        self.assertEqual(
            Path(submission.formatted_pdf_file.path),
            corrected_pdf_path,
        )
        self.assertEqual(
            Path(submission.formatted_source_file.path),
            corrected_source_path,
        )
        self.assertEqual(
            corrected_pdf_path.read_bytes(),
            b"reviewed corrected pdf",
        )
        self.assertEqual(
            corrected_source_path.read_bytes(),
            b"reviewed corrected source",
        )

    def test_failed_manual_report_edit_does_not_overwrite_previous_report(self):
        self.make_master_paper("P001", "Rollback Report", "Ada")
        old_report = self.make_pdf_file(
            "reports/original-report.pdf",
            b"original report",
        )
        submission = self.make_final_submission(
            final_submission_title="Rollback Report",
            extracted_title="Rollback Report",
            plagiarism_report_path=str(old_report),
        )
        token = make_evidence_token(
            "final-submission-edit",
            final_submission_edit_evidence(submission),
        )
        form = FinalSubmissionForm(
            self.final_submission_form_data(submission),
            instance=submission,
        )
        self.assertTrue(form.is_valid(), form.errors)

        with patch(
            "submissions.services.manual_edit._guard_review_fields",
            side_effect=RuntimeError("injected report failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected report failure"):
                apply_final_submission_manual_edit(
                    submission,
                    form,
                    report_file=SimpleUploadedFile(
                        "new-report.pdf",
                        b"new report",
                        content_type="application/pdf",
                    ),
                    expected_evidence_token=token,
                )

        submission.refresh_from_db()
        self.assertEqual(submission.plagiarism_report_path, str(old_report))
        self.assertEqual(old_report.read_bytes(), b"original report")

    def test_stale_process_pdf_issue_cannot_overwrite_format_review(self):
        self.make_master_paper("P001", "Formatting Concurrency", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Formatting Concurrency",
            extracted_title="Formatting Concurrency",
            format_status="pending",
            format_notes="",
        )
        page = self.client.get(reverse("submissions:process"))
        row = next(
            item
            for item in page.context["processed_rows"]
            if item["submission"].pk == submission.pk
        )
        submission.format_status = "review_ok"
        submission.format_notes = "Reviewed by another editor."
        submission.save(
            update_fields=["format_status", "format_notes", "updated_at"]
        )

        response = self.client.post(
            reverse("submissions:process"),
            {
                "action": "record_format_issue",
                "submission_id": submission.pk,
                "evidence_token": row["formatting_evidence_token"],
                "issue_note": "Old page issue.",
            },
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.format_status, "review_ok")
        self.assertEqual(
            submission.format_notes,
            "Reviewed by another editor.",
        )

    def test_master_title_edit_resets_verification_and_stale_edit_is_rejected(self):
        paper = self.make_master_paper("P001", "Master Original", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Master Original",
            extracted_title="Master Original",
            paper_id_verified=True,
        )
        stale_page = self.client.get(
            reverse("submissions:initial_paper_edit", args=[paper.pk])
        )
        stale_token = stale_page.context["evidence_token"]
        paper.title = "Newer Master Title"
        paper.save(update_fields=["title", "updated_at"])

        response = self.client.post(
            reverse("submissions:initial_paper_edit", args=[paper.pk]),
            {
                "evidence_token": stale_token,
                "paper_id": "P001",
                "acceptance_status": "accepted",
                "title": "Stale Master Title",
                "authors": "Ada",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        self.assertEqual(paper.title, "Newer Master Title")

        current_page = self.client.get(
            reverse("submissions:initial_paper_edit", args=[paper.pk])
        )
        response = self.client.post(
            reverse("submissions:initial_paper_edit", args=[paper.pk]),
            {
                "evidence_token": current_page.context["evidence_token"],
                "paper_id": "P001",
                "acceptance_status": "accepted",
                "title": "Approved Master Revision",
                "authors": "Ada",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "pending")

    def test_compound_source_extension_is_preserved_in_package(self):
        self.make_master_paper("P001", "Compound Source", "Ada")
        submission = self.make_final_submission(
            final_submission_title="Compound Source",
            extracted_title="Compound Source",
        )
        source_bytes = b"compressed source archive"
        submission.formatted_source_file.save(
            "paper-source.tar.gz",
            ContentFile(source_bytes),
            save=True,
        )
        submission.source_hash = hashlib.sha256(source_bytes).hexdigest()
        submission.save(update_fields=["source_hash", "updated_at"])

        with zipfile.ZipFile(export_publication_package()) as archive:
            source_name = next(
                name for name in archive.namelist() if name.startswith("Source/")
            )
            self.assertTrue(source_name.endswith(".tar.gz"))
            self.assertEqual(archive.read(source_name), source_bytes)

    def test_author_cache_excludes_active_finals_outside_paper_master(self):
        self.make_master_paper("P001", "In Scope", "Ada")
        included = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="In Scope",
            extracted_title="In Scope",
            extracted_authors="Included Author",
        )
        excluded = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="NOTMASTER",
            final_submission_title="Out of Scope",
            extracted_title="Out of Scope",
            extracted_authors="Outside Author",
        )

        rebuild_paper_authors()

        self.assertTrue(
            PaperAuthor.objects.filter(final_submission=included).exists()
        )
        self.assertFalse(
            PaperAuthor.objects.filter(final_submission=excluded).exists()
        )

    def test_publication_author_limit_uses_context_when_cache_is_empty(self):
        settings_obj = AppSetting.load()
        settings_obj.author_paper_limit = 1
        settings_obj.save(update_fields=["author_paper_limit"])
        for paper_id, final_id in (("P001", "101"), ("P002", "102")):
            self.make_master_paper(paper_id, f"Paper {paper_id}", "Ada Lovelace")
            self.make_final_submission(
                final_submission_id=final_id,
                paper_id_filled=paper_id,
                final_submission_title=f"Paper {paper_id}",
                extracted_title=f"Paper {paper_id}",
                extracted_authors="Ada Lovelace",
            )

        PaperAuthor.objects.all().delete()

        blockers = publication_readiness_rows(
            context=PublicationReadContext.load()
        )
        self.assertIn("Author Over Limit", {row["category"] for row in blockers})
        self.assert_publication_blocked("Author Over Limit")

    def test_stale_author_cache_cannot_create_false_publication_blocker(self):
        settings_obj = AppSetting.load()
        settings_obj.author_paper_limit = 1
        settings_obj.save(update_fields=["author_paper_limit"])
        self.make_master_paper("P001", "Current Paper", "Ada Lovelace")
        current = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Current Paper",
            extracted_title="Current Paper",
            extracted_authors="Ada Lovelace",
        )
        stale = self.make_final_submission(
            final_submission_id="100",
            paper_id_filled="P001",
            final_submission_title="Old Paper",
            extracted_title="Old Paper",
            extracted_authors="Ada Lovelace",
            active_version=False,
        )
        PaperAuthor.objects.bulk_create(
            [
                PaperAuthor(
                    final_submission=current,
                    paper_id="P001",
                    author_name="Ada Lovelace",
                    normalized_author_name="ada lovelace",
                    author_order=1,
                ),
                PaperAuthor(
                    final_submission=stale,
                    paper_id="P999",
                    author_name="Ada Lovelace",
                    normalized_author_name="ada lovelace",
                    author_order=1,
                ),
            ]
        )

        blockers = publication_readiness_rows(
            context=PublicationReadContext.load()
        )
        self.assertNotIn("Author Over Limit", {row["category"] for row in blockers})
