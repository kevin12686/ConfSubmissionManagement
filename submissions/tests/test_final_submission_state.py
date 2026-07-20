import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from submissions.models import (
    FinalSubmission,
    FinalSubmissionFileState,
    FinalSubmissionIdentityState,
    FinalSubmissionPlagiarismState,
    FinalSubmissionPublicationState,
    FinalSubmissionReviewState,
    InitialPaper,
)
from submissions.services.final_submission_state import (
    MIRRORED_SOURCE_FIELDS,
    STATE_DOMAINS,
    bulk_sync_submission_state_records,
    bulk_update_submissions,
    defer_submission_state_sync,
    sync_all_submission_state_records,
)
from submissions.services.crosscheck import import_crosscheck_results
from submissions.services.integrations import import_external_results
from submissions.services.pdf_processor import process_all_pdfs
from submissions.services.recompute import recompute_active_and_duplicate_state


class FinalSubmissionStatePersistenceTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audit_root = Path(self.temp_dir.name) / "logs"
        self.audit_root.mkdir()
        self.audit_root_patcher = patch(
            "submissions.services.audit.audit_log_root",
            lambda: self.audit_root,
        )
        self.audit_root_patcher.start()
        self.addCleanup(self.audit_root_patcher.stop)
        self.submission = FinalSubmission.objects.create(
            final_submission_id="F001",
            start2_paper_id_raw="P001",
            paper_id_filled="P001",
            final_submission_title="A Publication-Safe Test",
            final_submission_authors="Alice; Bob",
            extracted_title="A Publication-Safe Test",
            extracted_authors="Alice; Bob",
            processing_status="pending",
            plagiarism_status="pending",
        )

    def test_mapping_covers_every_compatibility_field(self):
        compatibility_fields = {
            field.name
            for field in FinalSubmission._meta.concrete_fields
            if field.name not in {"id", "created_at", "updated_at"}
        }
        self.assertEqual(MIRRORED_SOURCE_FIELDS, compatibility_fields)

    def test_initial_save_populates_every_state_domain(self):
        for domain in STATE_DOMAINS:
            state = getattr(self.submission, f"{domain.key}_state")
            for item in domain.fields:
                self.assertEqual(
                    getattr(state, item.target),
                    item.value_from(self.submission),
                    f"{domain.key}.{item.target}",
                )

    def test_update_fields_only_upserts_affected_domains(self):
        self.submission.processing_status = "error"
        self.submission.processing_message = "Missing PDF."

        with CaptureQueriesContext(connection) as captured:
            self.submission.save(
                update_fields=[
                    "processing_status",
                    "processing_message",
                    "updated_at",
                ]
            )

        sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertIn(FinalSubmissionFileState._meta.db_table, sql)
        self.assertIn(FinalSubmissionReviewState._meta.db_table, sql)
        self.assertNotIn(FinalSubmissionIdentityState._meta.db_table, sql)
        self.assertNotIn(FinalSubmissionPublicationState._meta.db_table, sql)
        self.assertNotIn(FinalSubmissionPlagiarismState._meta.db_table, sql)

    def test_bulk_sync_repairs_missing_rows_and_matches_source(self):
        FinalSubmissionIdentityState.objects.filter(
            final_submission=self.submission
        ).delete()
        FinalSubmissionFileState.objects.filter(
            final_submission=self.submission
        ).delete()

        bulk_sync_submission_state_records([self.submission])

        self.submission.refresh_from_db()
        for domain in STATE_DOMAINS:
            state = getattr(self.submission, f"{domain.key}_state")
            for item in domain.fields:
                self.assertEqual(
                    getattr(state, item.target),
                    item.value_from(self.submission),
                    f"{domain.key}.{item.target}",
                )

    def test_deferred_sync_flushes_latest_values_once(self):
        original_message = self.submission.file_state.processing_message

        with defer_submission_state_sync():
            self.submission.processing_message = "First"
            self.submission.save(
                update_fields=["processing_message", "updated_at"]
            )
            self.submission.processing_message = "Second"
            self.submission.save(
                update_fields=["processing_message", "updated_at"]
            )
            self.submission.file_state.refresh_from_db()
            self.assertEqual(
                self.submission.file_state.processing_message,
                original_message,
            )

        self.submission.file_state.refresh_from_db()
        self.assertEqual(self.submission.file_state.processing_message, "Second")

    def test_deferred_sync_and_outer_transaction_roll_back_together(self):
        original_message = self.submission.processing_message

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                with defer_submission_state_sync():
                    self.submission.processing_message = "Must roll back"
                    self.submission.save(
                        update_fields=["processing_message", "updated_at"]
                    )
                    raise RuntimeError("stop")

        self.submission.refresh_from_db()
        self.submission.file_state.refresh_from_db()
        self.assertEqual(self.submission.processing_message, original_message)
        self.assertEqual(
            self.submission.file_state.processing_message,
            original_message,
        )

    def test_bulk_update_preserves_model_derived_review_fields(self):
        self.submission.title_author_review_status = "review_ok"
        self.submission.processing_status = "processed"

        bulk_update_submissions(
            [self.submission],
            ["title_author_review_status", "processing_status"],
        )

        self.submission.refresh_from_db()
        self.submission.file_state.refresh_from_db()
        self.submission.review_state.refresh_from_db()
        self.assertTrue(self.submission.title_author_verified)
        self.assertIsNotNone(self.submission.title_author_verified_at)
        self.assertTrue(self.submission.review_state.title_author_verified)
        self.assertEqual(
            self.submission.file_state.processing_status,
            "processed",
        )

    def test_full_state_sync_query_count_does_not_scale_per_submission(self):
        FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=f"F{index:03d}",
                    paper_id_filled=f"P{index:03d}",
                )
                for index in range(2, 52)
            ]
        )

        with CaptureQueriesContext(connection) as captured:
            synced = sync_all_submission_state_records()

        self.assertEqual(synced, 51)
        self.assertLessEqual(len(captured), 15)
        for state_model in [
            FinalSubmissionIdentityState,
            FinalSubmissionFileState,
            FinalSubmissionReviewState,
            FinalSubmissionPublicationState,
            FinalSubmissionPlagiarismState,
        ]:
            self.assertEqual(state_model.objects.count(), 51)

    def test_bulk_update_query_count_does_not_scale_per_submission(self):
        submissions = [self.submission]
        for index in range(2, 52):
            submissions.append(
                FinalSubmission.objects.create(
                    final_submission_id=f"F{index:03d}",
                    paper_id_filled=f"P{index:03d}",
                )
            )
        for submission in submissions:
            submission.processing_status = "error"
            submission.processing_message = "Batch error"

        with CaptureQueriesContext(connection) as captured:
            updated = bulk_update_submissions(
                submissions,
                ["processing_status", "processing_message"],
            )

        self.assertEqual(updated, 51)
        self.assertLessEqual(len(captured), 15)
        self.assertEqual(
            FinalSubmissionFileState.objects.filter(
                processing_status="error",
                processing_message="Batch error",
            ).count(),
            51,
        )

    def test_process_all_pdfs_batch_keeps_file_state_in_sync(self):
        InitialPaper.objects.create(
            paper_id="P001",
            acceptance_status="Accepted",
            title="A Publication-Safe Test",
        )

        with patch(
            "submissions.services.pdf_processor.sync_debug_publication_files",
            return_value={
                "synced_count": 0,
                "skipped_count": 0,
                "manifest_path": "",
            },
        ), patch("submissions.services.pdf_processor.audit_success"):
            result = process_all_pdfs()

        self.submission.refresh_from_db()
        self.submission.file_state.refresh_from_db()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.submission.processing_status, "error")
        self.assertEqual(
            self.submission.file_state.processing_status,
            self.submission.processing_status,
        )
        self.assertEqual(
            self.submission.file_state.processing_message,
            self.submission.processing_message,
        )

    def test_active_duplicate_recompute_keeps_identity_state_in_sync(self):
        replacement = FinalSubmission.objects.create(
            final_submission_id="F010",
            start2_paper_id_raw="P001",
            paper_id_filled="P001",
            final_submission_title="A Publication-Safe Test",
        )

        recompute_active_and_duplicate_state()

        self.submission.refresh_from_db()
        replacement.refresh_from_db()
        self.submission.identity_state.refresh_from_db()
        replacement.identity_state.refresh_from_db()
        self.assertFalse(self.submission.active_version)
        self.assertTrue(self.submission.duplicate_submission)
        self.assertTrue(replacement.active_version)
        self.assertFalse(replacement.duplicate_submission)
        self.assertEqual(
            self.submission.identity_state.active_version,
            self.submission.active_version,
        )
        self.assertEqual(
            self.submission.identity_state.duplicate_submission,
            self.submission.duplicate_submission,
        )
        self.assertEqual(
            replacement.identity_state.active_version,
            replacement.active_version,
        )
        self.assertEqual(
            replacement.identity_state.duplicate_submission,
            replacement.duplicate_submission,
        )

    def test_crosscheck_bulk_import_keeps_plagiarism_state_in_sync(self):
        InitialPaper.objects.create(
            paper_id="P001",
            acceptance_status="Accepted",
            title="A Publication-Safe Test",
        )
        self.submission.active_version = True
        self.submission.save(update_fields=["active_version", "updated_at"])
        uploaded_file = SimpleUploadedFile(
            "crosscheck.csv",
            (
                "filename,plagiarism_percent,single_percent\n"
                "P001_batch.pdf,7,2\n"
            ).encode(),
            content_type="text/csv",
        )

        with patch(
            "submissions.services.crosscheck._crosscheck_batch_submission",
            return_value=(self.submission, "", False),
        ):
            result = import_crosscheck_results(uploaded_file)

        self.submission.refresh_from_db()
        self.submission.plagiarism_state.refresh_from_db()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            self.submission.plagiarism_state.similarity_score,
            self.submission.similarity_score,
        )
        self.assertEqual(
            self.submission.plagiarism_state.single_similarity_score,
            self.submission.single_similarity_score,
        )
        self.assertEqual(
            self.submission.plagiarism_state.plagiarism_imported_at,
            self.submission.plagiarism_imported_at,
        )

    def test_external_results_bulk_import_keeps_review_and_plagiarism_state_in_sync(self):
        uploaded_file = SimpleUploadedFile(
            "external.csv",
            (
                "final_submission_id,extracted_title,extracted_authors,"
                "similarity_score,single_similarity_score\n"
                "F001,Updated Extracted Title,Alice; Bob; Carol,8,3\n"
            ).encode(),
            content_type="text/csv",
        )

        result = import_external_results(uploaded_file)

        self.submission.refresh_from_db()
        self.submission.review_state.refresh_from_db()
        self.submission.plagiarism_state.refresh_from_db()
        self.assertEqual(result["updated_title_author"], 1)
        self.assertEqual(result["updated_plagiarism"], 1)
        self.assertEqual(
            self.submission.review_state.extracted_title,
            self.submission.extracted_title,
        )
        self.assertEqual(
            self.submission.review_state.extracted_authors,
            self.submission.extracted_authors,
        )
        self.assertEqual(
            self.submission.review_state.title_author_review_status,
            self.submission.title_author_review_status,
        )
        self.assertEqual(
            self.submission.plagiarism_state.similarity_score,
            self.submission.similarity_score,
        )
        self.assertEqual(
            self.submission.plagiarism_state.single_similarity_score,
            self.submission.single_similarity_score,
        )
