import hashlib
import zipfile
from unittest.mock import patch

from django.db import OperationalError, ProgrammingError
from django.urls import reverse

from submissions.services.editor_uploads import create_editor_submission
from submissions.services.checks import publication_readiness_rows
from submissions.services.crosscheck import prepare_crosscheck_upload
from submissions.services.file_manager import publication_pdf_info, publication_source_info
from submissions.services.reports import export_publication_package
from submissions.services.publication_read import publication_database_signature
from submissions.services.workflow_alerts import workflow_alert_counts
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase


class WorkflowRefreshTests(EditorialAcceptanceTestCase):
    def test_alert_reads_are_fresh_uncached_and_do_not_change_state(self):
        paper = self.make_master_paper("P001", "Fresh status", "Example Author")
        self.make_final_submission(final_submission_id="1", paper_id_filled="P001")
        url = reverse("submissions:workflow_alerts")
        self.assertEqual(workflow_alert_counts()["start2_editor_conflicts"], 0)
        create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor PDF"),
            source_file=self.uploaded_file("editor.tex", b"source"),
            notes="Status test",
        )
        before = publication_database_signature()
        for _ in range(3):
            response = self.client.get(url)
            self.assertContains(response, "Start2/Editor version decision needed")
            self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(publication_database_signature(), before)

    def test_database_and_file_read_failures_are_unavailable_not_zero(self):
        for failure in (OperationalError("locked"), ProgrammingError("schema"), OSError("file read")):
            with self.subTest(failure=type(failure).__name__), patch(
                "submissions.controllers.ui.workflow_alert_counts", side_effect=failure
            ):
                response = self.client.get(reverse("submissions:workflow_alerts"))
                self.assertContains(response, "Workflow status is unavailable", status_code=503)
                self.assertNotContains(response, "schema", status_code=503)
                self.assertIn("no-store", response["Cache-Control"])

    def test_local_conflict_warning_is_scoped_to_matching_records_and_worklist(self):
        paper = self.make_master_paper("P001", "Version decision", "Example Author")
        self.make_final_submission(final_submission_id="1", paper_id_filled="P001")
        create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor PDF"),
            source_file=self.uploaded_file("editor.tex", b"source"),
            notes="Status test",
        )
        url = reverse("submissions:final_submission_list")
        for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
            response = self.client.get(url, {"filter": "version_conflicts"}, **headers)
            html = response.content.decode()
            self.assertLess(html.index('id="final-submission-worklist"'), html.index("Version decision required for"))
            empty = self.client.get(url, {"filter": "version_conflicts", "q": "NO-MATCH"}, **headers)
            self.assertNotContains(empty, "Version decision required for")
            self.assertNotContains(self.client.get(url, **headers), "Version decision required for")

    def test_organized_mutation_refreshes_filtered_membership_summary_and_page(self):
        for number in range(1, 4):
            paper_id = f"P{number:03d}"
            self.make_master_paper(paper_id, "Score review", "Example Author")
            self.make_final_submission(
                final_submission_id=str(number), paper_id_filled=paper_id,
                similarity_score=80, single_similarity_score=1,
            )
        url = reverse("submissions:organized_list") + "?filter=plagiarism_issues&sort=paper_id_asc&per_page=25"
        before = self.client.get(url)
        target = before.context["rows"][0]["submission"]
        response = self.client.post(url, {
            "submission_id": target.pk, "action": "approve_exception",
            "exception_key": f"plagiarism_percent:{target.pk}",
            "draft_reason_plagiarism_percent": "Editorial waiver",
            "evidence_token": self.exception_evidence_token(f"plagiarism_percent:{target.pk}"),
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_filter"], "plagiarism_issues")
        self.assertEqual(response.context["current_sort"], "paper_id_asc")
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertNotContains(response, f'id="organized-row-submission-{target.pk}"')
        self.assertContains(response, "Plagiarism % exception allowed.")
        self.assertContains(response, 'data-cfm-feedback-level="success"')
        self.assertContains(response, f'data-cfm-feedback-record="Paper {target.paper_id_filled} / Final {target.final_submission_id}"')
        self.assertEqual(response.context["summary"], self.client.get(url).context["summary"])

    def test_shared_form_guard_and_feedback_assets_are_scoped_without_rule_changes(self):
        self.make_master_paper("P001", "Shared UI", "Example Author")
        submission = self.make_final_submission(final_submission_id="1", paper_id_filled="P001")
        before = publication_database_signature()
        url = reverse("submissions:formatting")
        for mode in ("single", "focus"):
            response = self.client.get(url, {"mode": mode, "submission": submission.pk, "filter": "all"}, follow=True)
            self.assertContains(response, 'data-cfm-unsaved-guard="You have unsaved formatting changes. Leave without saving?"')
            self.assertContains(response, 'submissions/form_state.js')
            self.assertContains(response, 'submissions/form_guard.js')
            self.assertContains(response, 'data-cfm-toast-container')
        response = self.client.get(url, {"filter": "all"})
        self.assertNotContains(response, 'data-cfm-unsaved-guard=')
        self.assertEqual(publication_database_signature(), before)

    def test_audit_page_has_its_own_browser_title(self):
        response = self.client.get(reverse("submissions:audit_log"))
        self.assertContains(response, "<title>Audit Log", html=False)

    def test_fast_count_changes_never_reuse_previous_zero_or_warning(self):
        with patch("submissions.services.workflow_alerts.PublicationReadContext.load") as load, patch(
            "submissions.services.workflow_alerts.active_pdfs_needing_processing"
        ) as pending, patch("submissions.services.workflow_alerts.editor_conflict_count") as conflicts:
            for count in (0, 2, 0, 1, 0):
                pending.return_value = [object()] * count
                conflicts.return_value = count
                self.assertEqual(workflow_alert_counts(), {
                    "active_pdfs_need_processing": count, "start2_editor_conflicts": count,
                })
            self.assertEqual(load.call_count, 5)

    def test_organized_last_filtered_page_is_clamped_after_resolving_final_row(self):
        for number in range(1, 27):
            paper_id = f"P{number:03d}"
            self.make_master_paper(paper_id, "Page refill", f"Example Author {number}")
            self.make_final_submission(
                final_submission_id=str(number), paper_id_filled=paper_id,
                similarity_score=80, single_similarity_score=1,
            )
        url = reverse("submissions:organized_list") + "?filter=plagiarism_issues&sort=paper_id_asc&page_size=25&page=2"
        target = self.client.get(url).context["rows"][0]["submission"]
        response = self.client.post(url, {
            "submission_id": target.pk, "action": "approve_exception",
            "exception_key": f"plagiarism_percent:{target.pk}",
            "draft_reason_plagiarism_percent": "Editorial waiver",
            "evidence_token": self.exception_evidence_token(f"plagiarism_percent:{target.pk}"),
        }, HTTP_HX_REQUEST="true")
        self.assertEqual(response.context["pagination"].page_number, 1)
        self.assertEqual(response.context["pagination"].total_count, 25)
        self.assertEqual(len(response.context["rows"]), 25)

    def test_advisory_refresh_cannot_change_corrected_export_bytes_or_review_state(self):
        self.make_master_paper("P001", "Selected files", "Example Author")
        pdf = self.make_file("media/formatted/selected.pdf", b"corrected publication PDF")
        source = self.make_file("media/formatted/selected.tex", b"corrected publication source")
        submission = self.make_final_submission(
            final_submission_id="1", paper_id_filled="P001",
            final_submission_title="Selected files", extracted_title="Selected files",
            final_submission_authors="Example Author", extracted_authors="Example Author",
            formatted_pdf_file="formatted/selected.pdf", formatted_source_file="formatted/selected.tex",
            pdf_hash=hashlib.sha256(pdf.read_bytes()).hexdigest(),
            source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        before = publication_database_signature()
        readiness = publication_readiness_rows()
        for route in ("workflow_alerts", "organized_list", "final_submission_list", "workflow_alerts"):
            self.assertEqual(self.client.get(reverse(f"submissions:{route}")).status_code, 200)
        self.assertEqual(publication_database_signature(), before)
        self.assertEqual(publication_readiness_rows(), readiness)
        self.assertEqual(publication_pdf_info(submission)["path"], str(pdf))
        self.assertEqual(publication_source_info(submission)["path"], str(source))
        with zipfile.ZipFile(export_publication_package()) as archive:
            self.assertEqual(archive.read("PDF/P001-Selected files.pdf"), pdf.read_bytes())
            self.assertEqual(archive.read("Source/P001-Selected files.tex"), source.read_bytes())
        with zipfile.ZipFile(prepare_crosscheck_upload("FRESH")["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_FRESH.pdf"), pdf.read_bytes())
