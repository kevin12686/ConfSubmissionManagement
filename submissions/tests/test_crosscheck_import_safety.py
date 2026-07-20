import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.crosscheck import (
    CROSSCHECK_EXPORT_MISSING_RESULTS,
    crosscheck_provenance_root,
    import_crosscheck_results,
    prepare_crosscheck_upload,
    upload_crosscheck_reports,
)
from submissions.services.import_export import (
    MAPPING_SHEET_NAME,
    MASTER_SHEET_NAME,
    START2_SHEET_NAME,
    import_final_submissions,
)
from submissions.services.import_preview import (
    apply_import_preview,
    preview_final_import,
)
from submissions.services.integrations import import_external_results
from submissions.services.storage_inventory import (
    CLEANUP_CONFIRMATION_TEXT,
    apply_storage_cleanup,
    preview_storage_cleanup,
)


class CrossCheckImportSafetyTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir(parents=True)
        self.settings_override = override_settings(
            BASE_DIR=self.root,
            MEDIA_ROOT=self.media_root,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.temp_dir.cleanup)

        setting = AppSetting.load()
        setting.reports_folder = str(self.root / "reports")
        setting.plagiarism_reports_folder = str(
            self.root / "plagiarism_reports"
        )
        setting.extraction_results_folder = str(self.root / "extraction")
        setting.incoming_folder = str(self.root / "incoming")
        setting.active_final_folder = str(self.root / "active")
        setting.old_versions_folder = str(self.root / "old")
        setting.publication_pdf_debug_folder = str(self.root / "debug")
        setting.save()
        for folder in (
            setting.reports_folder,
            setting.plagiarism_reports_folder,
            setting.extraction_results_folder,
            setting.incoming_folder,
            setting.active_final_folder,
            setting.old_versions_folder,
            setting.publication_pdf_debug_folder,
        ):
            Path(folder).mkdir(parents=True, exist_ok=True)

    def make_submission(
        self,
        *,
        paper_id="P001",
        final_id="10",
        pdf_bytes=b"publication pdf",
        active=True,
        excluded=False,
    ):
        InitialPaper.objects.get_or_create(
            paper_id=paper_id,
            defaults={
                "acceptance_status": "Accepted",
                "title": f"Title {paper_id}",
                "authors": "Ada",
            },
        )
        submission = FinalSubmission.objects.create(
            final_submission_id=final_id,
            start2_paper_id_raw=paper_id,
            paper_id_filled=paper_id,
            final_submission_title=f"Title {paper_id}",
            final_submission_authors="Ada",
            active_version=active,
            excluded_from_publication=excluded,
        )
        submission.pdf_file.save(
            f"{final_id}-{paper_id}.pdf",
            ContentFile(pdf_bytes),
            save=True,
        )
        submission.current_file_path = submission.pdf_file.path
        submission.save(update_fields=["current_file_path", "updated_at"])
        return submission

    def csv_upload(self, body):
        return SimpleUploadedFile(
            "results.csv",
            body.encode("utf-8"),
            content_type="text/csv",
        )

    def report_upload(self, name, body):
        return SimpleUploadedFile(
            name,
            body,
            content_type="application/pdf",
        )

    def test_cleanup_does_not_release_batch_token_or_delete_provenance(self):
        self.make_submission()
        result = prepare_crosscheck_upload("DURABLE")
        provenance = (
            crosscheck_provenance_root()
            / "DURABLE"
            / "all.json"
        )
        self.assertTrue(provenance.exists())

        preview = preview_storage_cleanup("generated_reports_exports")
        cleanup_paths = {
            Path(row["path"]).resolve() for row in preview["files"]
        }
        self.assertIn(Path(result["zip_path"]).resolve(), cleanup_paths)
        self.assertNotIn(provenance.resolve(), cleanup_paths)
        apply_storage_cleanup(
            preview["token"],
            CLEANUP_CONFIRMATION_TEXT,
        )

        self.assertFalse(Path(result["zip_path"]).exists())
        self.assertTrue(provenance.exists())
        with self.assertRaisesRegex(ValueError, "already been used"):
            prepare_crosscheck_upload("DURABLE")

    def test_external_result_with_unknown_explicit_final_id_does_not_fallback(self):
        submission = self.make_submission()

        result = import_external_results(
            self.csv_upload(
                "final_submission_id,paper_id,similarity_score,"
                "single_similarity_score\n"
                "UNKNOWN,P001,4,1\n"
            )
        )

        self.assertEqual(result["unmatched"], 1)
        submission.refresh_from_db()
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)

    def test_external_result_by_paper_id_blocks_multiple_active_finals(self):
        first = self.make_submission(final_id="10")
        second = self.make_submission(final_id="11")

        with self.assertRaisesRegex(
            ValueError,
            "multiple active Final Submissions",
        ):
            import_external_results(
                self.csv_upload(
                    "paper_id,similarity_score,single_similarity_score\n"
                    "P001,4,1\n"
                )
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.similarity_score)
        self.assertIsNone(second.similarity_score)

    def test_external_result_blocks_duplicate_rows_for_same_final(self):
        submission = self.make_submission()

        with self.assertRaisesRegex(
            ValueError,
            "exactly one row per Final Submission",
        ):
            import_external_results(
                self.csv_upload(
                    "final_submission_id,similarity_score,"
                    "single_similarity_score\n"
                    "10,4,1\n"
                    "10,8,2\n"
                )
            )

        submission.refresh_from_db()
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)

    def test_cleaned_old_batch_cannot_bind_to_replacement_in_another_scope(self):
        old = self.make_submission(final_id="10", pdf_bytes=b"old pdf")
        prepare_crosscheck_upload("OLD")
        shutil.rmtree(self.root / "data" / "crosscheck_upload" / "OLD")

        old.active_version = False
        old.save(update_fields=["active_version", "updated_at"])
        replacement = self.make_submission(
            final_id="11",
            pdf_bytes=b"new pdf",
        )

        result = import_crosscheck_results(
            self.csv_upload(
                "filename,plagiarism_percent,single_percent\n"
                "P001_OLD.pdf,4,1\n"
            )
        )
        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["stale"]), 1)
        replacement.refresh_from_db()
        self.assertIsNone(replacement.similarity_score)

        with self.assertRaisesRegex(ValueError, "different Final Submission"):
            prepare_crosscheck_upload(
                "OLD",
                scope=CROSSCHECK_EXPORT_MISSING_RESULTS,
            )

    def test_tampered_provenance_is_rejected(self):
        submission = self.make_submission()
        prepare_crosscheck_upload("TAMPER")
        provenance = (
            crosscheck_provenance_root()
            / "TAMPER"
            / "all.json"
        )
        payload = json.loads(provenance.read_text(encoding="utf-8"))
        payload["rows"][0]["final_submission_id"] = "999"
        provenance.write_text(json.dumps(payload), encoding="utf-8")

        result = import_crosscheck_results(
            self.csv_upload(
                "filename,plagiarism_percent,single_percent\n"
                "P001_TAMPER.pdf,4,1\n"
            )
        )

        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["stale"]), 1)
        submission.refresh_from_db()
        self.assertIsNone(submission.similarity_score)

    def test_malformed_percent_rejects_row_without_clearing_old_values(self):
        submission = self.make_submission()
        submission.similarity_score = 8
        submission.single_similarity_score = 2
        submission.save(
            update_fields=[
                "similarity_score",
                "single_similarity_score",
                "updated_at",
            ]
        )
        prepare_crosscheck_upload("PERCENT")

        result = import_crosscheck_results(
            self.csv_upload(
                "filename,plagiarism_percent,single_percent\n"
                "P001_PERCENT.pdf,not-a-number,3\n"
            )
        )

        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["invalid"]), 1)
        submission.refresh_from_db()
        self.assertEqual(submission.similarity_score, 8)
        self.assertEqual(submission.single_similarity_score, 2)

    def test_blank_percent_explicitly_sets_missing(self):
        submission = self.make_submission()
        submission.similarity_score = 8
        submission.single_similarity_score = 2
        submission.save(
            update_fields=[
                "similarity_score",
                "single_similarity_score",
                "updated_at",
            ]
        )
        prepare_crosscheck_upload("BLANK")

        result = import_crosscheck_results(
            self.csv_upload(
                "filename,plagiarism_percent,single_percent\n"
                "P001_BLANK.pdf,,\n"
            )
        )

        self.assertEqual(result["updated"], 1)
        submission.refresh_from_db()
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)

    def test_missing_percent_column_rejects_file_without_changes(self):
        submission = self.make_submission()
        submission.similarity_score = 8
        submission.single_similarity_score = 2
        submission.save(
            update_fields=[
                "similarity_score",
                "single_similarity_score",
                "updated_at",
            ]
        )
        prepare_crosscheck_upload("MISSING_COLUMN")

        with self.assertRaisesRegex(ValueError, "single_percent"):
            import_crosscheck_results(
                self.csv_upload(
                    "filename,plagiarism_percent\n"
                    "P001_MISSING_COLUMN.pdf,4\n"
                )
            )

        submission.refresh_from_db()
        self.assertEqual(submission.similarity_score, 8)
        self.assertEqual(submission.single_similarity_score, 2)

    def test_result_import_reads_each_token_provenance_once_per_batch(self):
        self.make_submission(paper_id="P001", final_id="10")
        self.make_submission(paper_id="P002", final_id="20")
        prepare_crosscheck_upload("CACHED")

        from submissions.services import crosscheck

        with patch(
            "submissions.services.crosscheck._token_provenance_payloads",
            wraps=crosscheck._token_provenance_payloads,
        ) as provenance_loader:
            result = import_crosscheck_results(
                self.csv_upload(
                    "filename,plagiarism_percent,single_percent\n"
                    "P001_CACHED.pdf,4,1\n"
                    "P002_CACHED.pdf,5,2\n"
                )
            )

        self.assertEqual(result["updated"], 2)
        self.assertEqual(provenance_loader.call_count, 1)

    def test_report_db_failure_restores_previous_file_and_database(self):
        submission = self.make_submission()
        prepare_crosscheck_upload("REPORT")
        upload_crosscheck_reports(
            [self.report_upload("P001_REPORT.pdf", b"old report")]
        )
        submission.refresh_from_db()
        report_path = Path(submission.plagiarism_report_path)
        imported_at = submission.plagiarism_imported_at

        with patch(
            "submissions.services.crosscheck.bulk_update_submissions",
            side_effect=RuntimeError("injected DB failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected DB failure"):
                upload_crosscheck_reports(
                    [self.report_upload("P001_REPORT.pdf", b"new report")]
                )

        submission.refresh_from_db()
        self.assertEqual(submission.plagiarism_report_path, str(report_path))
        self.assertEqual(submission.plagiarism_imported_at, imported_at)
        self.assertEqual(report_path.read_bytes(), b"old report")
        self.assertEqual(
            list(report_path.parent.glob(".P001_REPORT.pdf.*")),
            [],
        )

    def test_report_promote_failure_restores_previous_file(self):
        self.make_submission()
        prepare_crosscheck_upload("PROMOTE")
        upload_crosscheck_reports(
            [self.report_upload("P001_PROMOTE.pdf", b"old report")]
        )
        target = Path(AppSetting.load().plagiarism_reports_folder) / (
            "P001_PROMOTE.pdf"
        )
        real_replace = os.replace

        def fail_new_file_promote(source, destination):
            if str(source).endswith(".part") and Path(destination) == target:
                raise OSError("injected promote failure")
            return real_replace(source, destination)

        with patch(
            "submissions.services.crosscheck.os.replace",
            side_effect=fail_new_file_promote,
        ):
            with self.assertRaisesRegex(OSError, "injected promote failure"):
                upload_crosscheck_reports(
                    [self.report_upload("P001_PROMOTE.pdf", b"new report")]
                )

        self.assertEqual(target.read_bytes(), b"old report")
        self.assertEqual(list(target.parent.glob(".P001_PROMOTE.pdf.*")), [])

    def test_multiple_active_and_mixed_scope_do_not_create_zip(self):
        self.make_submission(final_id="10")
        self.make_submission(final_id="11")
        with self.assertRaisesRegex(ValueError, "multiple active"):
            prepare_crosscheck_upload("MULTI")
        self.assertFalse(
            (self.root / "data" / "crosscheck_upload" / "MULTI").exists()
        )

        FinalSubmission.objects.all().delete()
        self.make_submission(final_id="20", active=True)
        self.make_submission(final_id="21", active=False, excluded=True)
        with self.assertRaisesRegex(ValueError, "mixed Not Publishing"):
            prepare_crosscheck_upload("MIXED")
        self.assertFalse(
            (self.root / "data" / "crosscheck_upload" / "MIXED").exists()
        )

    def test_duplicate_same_kind_multipart_is_blocking_and_never_applies(self):
        InitialPaper.objects.create(
            paper_id="P001",
            acceptance_status="Accepted",
            title="Title P001",
            authors="Ada",
        )
        metadata = SimpleUploadedFile(
            "final.csv",
            (
                "final_submission_id,author_entered_paper_id,"
                "final_submission_title,final_submission_authors\n"
                "10,P001,Title P001,Ada\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )
        preview = preview_final_import(
            metadata,
            [
                self.report_upload(
                    "10_file_Submit_PDF.pdf",
                    b"first pdf",
                ),
                self.report_upload(
                    "10_file_Submit_Source.pdf",
                    b"second pdf",
                ),
            ],
        )

        self.assertEqual(len(preview["blocking_errors"]), 1)
        self.assertIn("Multiple PDF files", preview["blocking_errors"][0])
        with self.assertRaisesRegex(ValueError, "blocking errors"):
            apply_import_preview(preview["token"])
        self.assertFalse(
            FinalSubmission.objects.filter(final_submission_id="10").exists()
        )

    def test_legacy_mapping_api_uses_preview_and_blocks_duplicate_rows(self):
        workbook = io.BytesIO()
        mapping_columns = [f"c{index}" for index in range(15)]
        with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
            pd.DataFrame(
                [
                    {
                        "paper_id": "P001",
                        "acceptance_status": "accepted",
                        "title": "Mapped",
                        "authors": "Ada",
                    }
                ]
            ).to_excel(writer, sheet_name=MASTER_SHEET_NAME, index=False)
            pd.DataFrame(
                [
                    {
                        "submission id": "10",
                        "paper-id": "P001",
                        "title": "Mapped",
                        "authors": "Ada",
                    },
                    {
                        "submission id": "10",
                        "paper-id": "P001",
                        "title": "Different",
                        "authors": "Grace",
                    },
                ]
            ).to_excel(writer, sheet_name=START2_SHEET_NAME, index=False)
            pd.DataFrame(
                [
                    [
                        "10",
                        "P001",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "10_file_Submit_PDF.pdf",
                    ]
                ],
                columns=mapping_columns,
            ).to_excel(writer, sheet_name=MAPPING_SHEET_NAME, index=False)
        workbook.seek(0)

        with self.assertRaisesRegex(ValueError, "Duplicate Final ID"):
            import_final_submissions(
                SimpleUploadedFile(
                    "mapping.xlsx",
                    workbook.getvalue(),
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                )
            )

        self.assertFalse(InitialPaper.objects.filter(paper_id="P001").exists())
        self.assertFalse(FinalSubmission.objects.exists())

    def test_legacy_mapping_update_uses_canonical_review_resets(self):
        submission = self.make_submission(paper_id="P001", final_id="10")
        InitialPaper.objects.create(
            paper_id="P002",
            acceptance_status="Accepted",
            title="Official P002",
            authors="Grace",
        )
        submission.paper_id_verified = True
        submission.verification_status = "verified"
        submission.extracted_title = "Old Extracted"
        submission.extracted_title_verified = True
        submission.extracted_title_match_status = "verified"
        submission.save()

        workbook = io.BytesIO()
        mapping_columns = [f"c{index}" for index in range(15)]
        with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
            pd.DataFrame(
                [
                    {
                        "submission id": "10",
                        "paper-id": "P002",
                        "title": "Different Final Title",
                        "authors": "Grace",
                    }
                ]
            ).to_excel(writer, sheet_name=START2_SHEET_NAME, index=False)
            pd.DataFrame(
                [["10", "P002", *("" for _index in range(13))]],
                columns=mapping_columns,
            ).to_excel(writer, sheet_name=MAPPING_SHEET_NAME, index=False)
        workbook.seek(0)

        import_final_submissions(
            SimpleUploadedFile(
                "mapping.xlsx",
                workbook.getvalue(),
                content_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
            )
        )

        submission.refresh_from_db()
        self.assertEqual(submission.paper_id_filled, "P002")
        self.assertFalse(submission.paper_id_verified)
        self.assertNotEqual(submission.verification_status, "verified")
        self.assertFalse(submission.extracted_title_verified)
        self.assertNotEqual(
            submission.extracted_title_match_status,
            "verified",
        )

    def test_direct_import_api_uses_canonical_pdf_reset_rules(self):
        submission = self.make_submission()
        submission.page_count = 8
        submission.pdf_hash = "old-hash"
        submission.processing_status = "processed"
        submission.format_status = "review_ok"
        submission.similarity_score = 4
        submission.single_similarity_score = 1
        submission.save()
        metadata = SimpleUploadedFile(
            "final.csv",
            (
                "final_submission_id,author_entered_paper_id,"
                "final_submission_title,final_submission_authors\n"
                "10,P001,Title P001,Ada\n"
            ).encode("utf-8"),
            content_type="text/csv",
        )

        import_final_submissions(
            metadata,
            [
                self.report_upload(
                    "10_file_Submit_PDF.pdf",
                    b"replacement pdf",
                )
            ],
        )

        submission.refresh_from_db()
        self.assertIsNone(submission.page_count)
        self.assertEqual(submission.pdf_hash, "")
        self.assertEqual(submission.processing_status, "pending")
        self.assertEqual(submission.format_status, "pending")
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)
