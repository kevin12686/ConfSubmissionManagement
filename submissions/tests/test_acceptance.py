import csv
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from submissions.forms import FinalSubmissionForm
from submissions.models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    FinalSubmissionFileState,
    FinalSubmissionIdentityState,
    FinalSubmissionPlagiarismState,
    FinalSubmissionPublicationState,
    FinalSubmissionReviewState,
    InitialPaper,
    PaperAuthor,
)
from submissions.services.checks import (
    _annotate_error_rows,
    author_count_rows,
    dashboard_counts,
    duplicate_authors_in_paper,
    error_report_rows,
    publication_duplicate_map,
    publication_readiness_rows,
    rebuild_paper_authors,
    split_authors,
)
from submissions.services.file_manager import publication_pdf_info, publication_source_info
from submissions.services.formatting import update_formatting_submission
from submissions.services.exceptions import approve_exception, exception_rows
from submissions.services.import_export import (
    MASTER_SHEET_NAME,
    MAPPING_SHEET_NAME,
    START2_SHEET_NAME,
    _mark_duplicate_submissions,
    import_final_submissions,
    import_initial_papers,
)
from submissions.services.integrations import import_external_results
from submissions.services.import_preview import apply_import_preview, preview_final_import
from submissions.services.organized_list import organized_list_rows
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.reports import (
    author_count_frame,
    export_active_versions,
    export_all_reports,
    export_publication_package,
)
from submissions.services.crosscheck import import_crosscheck_results, upload_crosscheck_reports
from submissions.services.system_state import (
    CONFIRMATION_TEXT,
    apply_system_state_restore,
    export_system_state,
    preview_system_state_restore,
)
from submissions.services.storage_inventory import (
    CLEANUP_CONFIRMATION_TEXT,
    apply_storage_cleanup,
    build_storage_inventory,
    preview_storage_cleanup,
    repair_publication_paths,
)
from submissions.services.title_author_extraction import (
    unverify_extracted_title,
    unverify_title_author,
    verify_extracted_title,
    verify_title_author,
)
from submissions.services.verification import (
    mark_not_publishing,
    undo_not_publishing,
    unverify_submission,
    verification_rows,
    verify_submission,
)


class EditorialAcceptanceTestCase(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.preview_root = self.root / "import_previews"
        self.preview_root.mkdir()
        self.preview_root_patcher = patch(
            "submissions.services.import_preview.preview_root",
            lambda: self.preview_root,
        )
        self.preview_root_patcher.start()
        self.addCleanup(self.preview_root_patcher.stop)
        self.system_state_reports_root = self.root / "system_state_backups"
        self.system_state_restore_root = self.root / "system_state_restore_previews"
        self.storage_cleanup_root = self.root / "storage_cleanup_previews"
        self.system_state_reports_root.mkdir()
        self.system_state_restore_root.mkdir()
        self.storage_cleanup_root.mkdir()
        self.addCleanup(
            shutil.rmtree,
            django_settings.BASE_DIR / "data" / "restored_external_folders",
            True,
        )
        self.system_state_reports_patcher = patch(
            "submissions.services.system_state.system_state_reports_root",
            lambda: self.system_state_reports_root,
        )
        self.system_state_restore_patcher = patch(
            "submissions.services.system_state.restore_preview_root",
            lambda: self.system_state_restore_root,
        )
        self.storage_cleanup_patcher = patch(
            "submissions.services.storage_inventory.cleanup_preview_root",
            lambda: self.storage_cleanup_root,
        )
        self.system_state_reports_patcher.start()
        self.system_state_restore_patcher.start()
        self.storage_cleanup_patcher.start()
        self.addCleanup(self.system_state_reports_patcher.stop)
        self.addCleanup(self.system_state_restore_patcher.stop)
        self.addCleanup(self.storage_cleanup_patcher.stop)

        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(self.root / "reports")
        settings_obj.active_final_folder = str(self.root / "active")
        settings_obj.old_versions_folder = str(self.root / "old")
        settings_obj.incoming_folder = str(self.root / "incoming")
        settings_obj.title_words_for_filename = 4
        settings_obj.page_minimum = 6
        settings_obj.page_limit = 12
        settings_obj.max_authors_per_paper = 5
        settings_obj.author_paper_limit = 3
        settings_obj.plagiarism_percent_threshold = 35
        settings_obj.single_similarity_threshold = 10
        settings_obj.active_version_rule = "final_id"
        settings_obj.save()

    def make_master_paper(self, paper_id="P001", title="Ready Paper", authors="Ada Lovelace; Alan Turing"):
        return InitialPaper.objects.create(
            paper_id=paper_id,
            acceptance_status="accepted",
            title=title,
            authors=authors,
        )

    def make_file(self, relative_path, content):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def make_pdf_file(self, name, content=None):
        payload = content if content is not None else f"PDF {name}".encode()
        return self.make_file(f"files/{name}", payload)

    def make_source_file(self, name, content=None):
        payload = content if content is not None else f"SOURCE {name}".encode()
        return self.make_file(f"files/{name}", payload)

    def make_final_submission(self, **overrides):
        paper_id = overrides.get("paper_id_filled", "P001")
        final_id = overrides.get("final_submission_id", "100")
        title = overrides.get("extracted_title", overrides.get("final_submission_title", "Ready Paper"))
        values = {
            "final_submission_id": final_id,
            "start2_paper_id_raw": paper_id,
            "paper_id_filled": paper_id,
            "final_submission_title": title,
            "final_submission_authors": "Ada Lovelace; Alan Turing",
            "upload_date": timezone.now(),
            "current_file_path": str(self.make_pdf_file(f"{final_id}.pdf")),
            "source_current_file_path": str(self.make_source_file(f"{final_id}.docx")),
            "extracted_title": title,
            "extracted_authors": "Ada Lovelace; Alan Turing",
            "page_count": 8,
            "processing_status": "processed",
            "processing_message": "Ready.",
            "pdf_hash": f"hash-{final_id}",
            "active_version": True,
            "paper_id_verified": True,
            "verification_status": "verified",
            "title_author_verified": True,
            "extracted_title_verified": True,
            "format_status": "review_ok",
            "similarity_score": 1,
            "single_similarity_score": 1,
        }
        values.update(overrides)
        if "title_author_review_status" not in overrides:
            values["title_author_review_status"] = (
                "review_ok" if values.get("title_author_verified") else "pending"
            )
        return FinalSubmission.objects.create(**values)

    def uploaded_csv(self, name, text):
        return SimpleUploadedFile(name, text.encode("utf-8-sig"), content_type="text/csv")

    def uploaded_file(self, name, content=b"file-content"):
        return SimpleUploadedFile(name, content)

    def final_submission_form_data(self, submission, **overrides):
        data = {
            "final_submission_id": submission.final_submission_id,
            "start2_paper_id_raw": submission.start2_paper_id_raw,
            "paper_id_filled": submission.paper_id_filled,
            "final_submission_title": submission.final_submission_title,
            "final_submission_authors": submission.final_submission_authors,
            "upload_date": submission.upload_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "extracted_title": submission.extracted_title,
            "extracted_authors": submission.extracted_authors,
            "title_author_source": submission.title_author_source,
            "title_author_extraction_message": submission.title_author_extraction_message,
            "title_author_review_status": submission.title_author_review_status,
            "duplicate_author_review_status": submission.duplicate_author_review_status,
            "duplicate_author_review_notes": submission.duplicate_author_review_notes,
            "extracted_title_match_message": submission.extracted_title_match_message,
            "similarity_score": (
                "" if submission.similarity_score is None else str(int(submission.similarity_score))
            ),
            "single_similarity_score": (
                ""
                if submission.single_similarity_score is None
                else str(int(submission.single_similarity_score))
            ),
            "processing_message": submission.processing_message,
            "publication_exclusion_reason": submission.publication_exclusion_reason,
            "publication_exclusion_notes": submission.publication_exclusion_notes,
        }
        if submission.extracted_title_verified:
            data["extracted_title_verified"] = "on"
        if submission.excluded_from_publication:
            data["excluded_from_publication"] = "on"
        data.update(overrides)
        return data

    def assert_publication_blocked(self, expected_text):
        with self.assertRaisesMessage(ValueError, expected_text):
            export_publication_package()

    def assert_zip_contains_manifest_pdf_source(self, zip_path, expected_rows):
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)), "ZIP entries must be unique.")
            manifest_name = next(name for name in names if name.startswith("publication_manifest_"))
            manifest_text = archive.read(manifest_name).decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(manifest_text)))
            self.assertEqual(len(rows), len(expected_rows))
            for expected in expected_rows:
                row = next(item for item in rows if item["ID"] == expected["paper_id"])
                self.assertEqual(row["Extracted Title"], expected["title"])
                self.assertEqual(row["Author Number"], str(expected["author_count"]))
                self.assertEqual(row["Page Number"], str(expected["page_count"]))
                self.assertEqual(row["Similarity (P)"], str(expected["similarity_score"]))
                self.assertEqual(row["Similarity (S)"], str(expected["single_similarity_score"]))
                self.assertEqual(archive.read(expected["pdf_arcname"]), expected["pdf_bytes"])
            self.assertEqual(archive.read(expected["source_arcname"]), expected["source_bytes"])


class StorageManagementTests(EditorialAcceptanceTestCase):
    def test_clear_database_wipes_app_state_settings_and_managed_files(self):
        settings_obj = AppSetting.load()
        settings_obj.conference_name = "Conference To Wipe"
        settings_obj.save()
        self.make_master_paper(paper_id="W001")
        submission = self.make_final_submission(final_submission_id="9901", paper_id_filled="W001")
        PaperAuthor.objects.create(
            final_submission=submission,
            paper_id="W001",
            author_name="Ada Lovelace",
            normalized_author_name="ada lovelace",
            author_order=1,
        )
        AuthorLimitWaiver.objects.create(
            normalized_author_name="ada lovelace",
            display_author_name="Ada Lovelace",
            approved=True,
            reason="temporary",
            approved_publication_paper_count=4,
            approved_at=timezone.now(),
        )
        managed_files = [
            self.root / "reports" / "report.xlsx",
            self.root / "active" / "active.pdf",
            self.media_root / "format_previews" / "preview.png",
            self.root / "data" / "system_state_backups" / "backup.zip",
            self.root / "data" / "storage_cleanup_previews" / "cleanup.json",
            self.root / "data" / "crosscheck_upload" / "TOKEN" / "upload.zip",
        ]
        for path in managed_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"wipe me")

        with override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.media_root):
            response = self.client.post(
                reverse("submissions:clear_database"),
                {"confirmation": "CLEAR DATABASE"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(InitialPaper.objects.count(), 0)
        self.assertEqual(FinalSubmission.objects.count(), 0)
        self.assertEqual(PaperAuthor.objects.count(), 0)
        self.assertEqual(AuthorLimitWaiver.objects.count(), 0)
        self.assertEqual(FinalSubmissionIdentityState.objects.count(), 0)
        self.assertEqual(FinalSubmissionFileState.objects.count(), 0)
        self.assertEqual(FinalSubmissionReviewState.objects.count(), 0)
        self.assertEqual(FinalSubmissionPublicationState.objects.count(), 0)
        self.assertEqual(FinalSubmissionPlagiarismState.objects.count(), 0)
        self.assertEqual(AppSetting.objects.count(), 1)
        self.assertEqual(AppSetting.load().conference_name, "")
        self.assertEqual(AppSetting.load().reports_folder, "data/reports")
        for path in managed_files:
            self.assertFalse(path.exists(), f"{path} should be removed by full wipe")

    def test_storage_inventory_detects_missing_current_path_and_orphan_cache(self):
        submission = self.make_final_submission(final_submission_id="8101", paper_id_filled="S001")
        Path(submission.current_file_path).unlink()
        cache_file = self.media_root / "format_previews" / "orphan-preview.png"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"preview")

        inventory = build_storage_inventory()

        self.assertTrue(
            any(
                row["final_submission_id"] == "8101"
                and row["role"] == "Current publication PDF"
                for row in inventory["missing_references"]
            )
        )
        self.assertTrue(
            any(
                Path(row["path"]).resolve() == cache_file.resolve()
                for row in inventory["cleanup_candidates"]
            )
        )
        self.assertTrue(
            any(row["category"] == "generated_cache" for row in inventory["categories"])
        )

    def test_storage_cleanup_preview_and_apply_are_conservative(self):
        submission = self.make_final_submission(final_submission_id="8102", paper_id_filled="S002")
        submission.pdf_file.save("protected-original.pdf", ContentFile(b"original"), save=True)
        submission.formatted_pdf_file.save("protected-corrected.pdf", ContentFile(b"corrected"), save=True)
        original_path = Path(submission.pdf_file.path)
        corrected_path = Path(submission.formatted_pdf_file.path)
        cache_file = self.media_root / "format_previews" / "cleanup-me.png"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"cache")
        orphan_output = self.root / "active" / "orphan-output.pdf"
        orphan_output.parent.mkdir(parents=True, exist_ok=True)
        orphan_output.write_bytes(b"orphan")

        preview = preview_storage_cleanup()

        self.assertTrue(cache_file.exists())
        self.assertTrue(orphan_output.exists())
        selected_paths = {Path(row["path"]).resolve() for row in preview["files"]}
        self.assertIn(cache_file.resolve(), selected_paths)
        self.assertIn(orphan_output.resolve(), selected_paths)
        self.assertNotIn(original_path.resolve(), selected_paths)
        self.assertNotIn(corrected_path.resolve(), selected_paths)
        with self.assertRaises(ValueError):
            apply_storage_cleanup(preview["token"], "wrong")
        self.assertTrue(cache_file.exists())

        result = apply_storage_cleanup(preview["token"], CLEANUP_CONFIRMATION_TEXT)

        self.assertGreaterEqual(result["deleted_count"], 2)
        self.assertFalse(cache_file.exists())
        self.assertFalse(orphan_output.exists())
        self.assertTrue(original_path.exists())
        self.assertTrue(corrected_path.exists())

    def test_repair_publication_paths_recreates_active_and_old_outputs(self):
        active = self.make_final_submission(final_submission_id="8201", paper_id_filled="S003")
        inactive = self.make_final_submission(
            final_submission_id="8200",
            paper_id_filled="S003",
            active_version=False,
        )
        active.pdf_file.save("active-canonical.pdf", ContentFile(b"active pdf"), save=True)
        active.source_file.save("active-canonical.docx", ContentFile(b"active source"), save=True)
        inactive.pdf_file.save("old-canonical.pdf", ContentFile(b"old pdf"), save=True)
        inactive.source_file.save("old-canonical.docx", ContentFile(b"old source"), save=True)
        active.current_file_path = str(self.root / "missing" / "active.pdf")
        active.source_current_file_path = str(self.root / "missing" / "active.docx")
        active.save(update_fields=["current_file_path", "source_current_file_path", "updated_at"])
        inactive.current_file_path = str(self.root / "missing" / "old.pdf")
        inactive.source_current_file_path = str(self.root / "missing" / "old.docx")
        inactive.save(update_fields=["current_file_path", "source_current_file_path", "updated_at"])
        no_pdf = self.make_final_submission(
            final_submission_id="8202",
            paper_id_filled="S004",
            current_file_path=str(self.root / "missing" / "no-source.pdf"),
        )

        result = repair_publication_paths()

        active.refresh_from_db()
        inactive.refresh_from_db()
        no_pdf.refresh_from_db()
        self.assertEqual(result["pdf_repaired_count"], 2)
        self.assertEqual(result["source_repaired_count"], 2)
        self.assertTrue(Path(active.current_file_path).exists())
        self.assertTrue(Path(inactive.current_file_path).exists())
        self.assertIn(str(self.root / "active"), active.current_file_path)
        self.assertIn(str(self.root / "old"), inactive.current_file_path)
        self.assertEqual(Path(active.current_file_path).read_bytes(), b"active pdf")
        self.assertEqual(Path(inactive.current_file_path).read_bytes(), b"old pdf")
        self.assertTrue(Path(active.source_current_file_path).exists())
        self.assertTrue(Path(inactive.source_current_file_path).exists())
        self.assertEqual(no_pdf.current_file_path, str(self.root / "missing" / "no-source.pdf"))


class SystemStateTests(EditorialAcceptanceTestCase):
    def test_system_state_restore_round_trips_settings_records_and_files(self):
        settings_obj = AppSetting.load()
        settings_obj.conference_name = "DSA 2026"
        settings_obj.page_limit = 10
        settings_obj.save()
        self.make_master_paper(paper_id="R001", title="Restored Paper")
        pdf_path = self.media_root / "active" / "R001.pdf"
        source_path = self.media_root / "source" / "R001.docx"
        report_path = self.media_root / "reports" / "R001_report.pdf"
        for path, payload in [
            (pdf_path, b"publication pdf"),
            (source_path, b"publication source"),
            (report_path, b"plagiarism report"),
        ]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        submission = self.make_final_submission(
            final_submission_id="9001",
            paper_id_filled="R001",
            start2_paper_id_raw="R001",
            final_submission_title="Restored Paper",
            extracted_title="Restored Paper",
            current_file_path=str(pdf_path),
            source_current_file_path=str(source_path),
            plagiarism_report_path=str(report_path),
        )
        PaperAuthor.objects.create(
            final_submission=submission,
            paper_id="R001",
            author_name="Ada Lovelace",
            normalized_author_name="ada lovelace",
            author_order=1,
        )
        AuthorLimitWaiver.objects.create(
            normalized_author_name="ada lovelace",
            display_author_name="Ada Lovelace",
            approved=True,
            reason="Chair approved.",
            approved_publication_paper_count=4,
            approved_at=timezone.now(),
        )

        snapshot = export_system_state()
        FinalSubmission.objects.create(final_submission_id="temp", paper_id_filled="TEMP")
        pdf_path.unlink()
        upload = SimpleUploadedFile(
            "snapshot.zip",
            snapshot["path"].read_bytes(),
            content_type="application/zip",
        )

        preview = preview_system_state_restore(upload)
        self.assertEqual(FinalSubmission.objects.filter(final_submission_id="temp").count(), 1)
        result = apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)

        settings_obj = AppSetting.load()
        self.assertEqual(settings_obj.conference_name, "DSA 2026")
        self.assertEqual(settings_obj.page_limit, 10)
        self.assertEqual(InitialPaper.objects.get().paper_id, "R001")
        restored = FinalSubmission.objects.get()
        self.assertEqual(restored.final_submission_id, "9001")
        self.assertTrue(Path(restored.current_file_path).exists())
        self.assertEqual(Path(restored.current_file_path).read_bytes(), b"publication pdf")
        self.assertTrue(Path(restored.source_current_file_path).exists())
        self.assertTrue(Path(restored.plagiarism_report_path).exists())
        self.assertEqual(PaperAuthor.objects.count(), 1)
        self.assertEqual(AuthorLimitWaiver.objects.count(), 1)
        self.assertTrue(Path(result["pre_restore_backup"]).exists())

    def test_system_state_manifest_contains_app_and_archive_versions(self):
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            state = json.loads(archive.read("state.json").decode("utf-8"))
            archive_text = json.dumps({"manifest": manifest, "state": state})
        self.assertEqual(manifest["app_name"], "Conference Final Manager")
        self.assertEqual(manifest["app_version"], "1.0.1")
        self.assertEqual(manifest["state_archive_version"], 2)
        self.assertNotIn(str(self.root), archive_text)
        self.assertNotIn("/var/", archive_text)
        self.assertNotIn("/private/var/", archive_text)
        self.assertNotIn("original_path", archive_text)
        self.assertNotIn("original_root", archive_text)

    def test_restore_preview_rejects_stale_apply(self):
        snapshot = export_system_state()
        upload = SimpleUploadedFile(
            "snapshot.zip",
            snapshot["path"].read_bytes(),
            content_type="application/zip",
        )
        preview = preview_system_state_restore(upload)
        InitialPaper.objects.create(paper_id="NEW")

        with self.assertRaisesMessage(Exception, "Database changed after preview"):
            apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)


class ImportAndMappingTests(EditorialAcceptanceTestCase):
    def test_initial_import_creates_updates_and_skips_blank_rows(self):
        result = import_initial_papers(
            self.uploaded_csv(
                "initial.csv",
                "paper_id,acceptance_status,title,authors\n"
                "R001,accepted,Original Title,Ada\n"
                ",accepted,Blank Row,Ignored\n",
            )
        )
        self.assertEqual(result, {"created": 1, "updated": 0})

        result = import_initial_papers(
            self.uploaded_csv(
                "initial.csv",
                "paper_id,acceptance_status,title,authors\n"
                "R001,accepted,Updated Title,Ada; Alan\n",
            )
        )
        paper = InitialPaper.objects.get(paper_id="R001")
        self.assertEqual(result, {"created": 0, "updated": 1})
        self.assertEqual(paper.title, "Updated Title")

    def test_initial_import_reads_preferred_xlsx_sheet(self):
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame([{"paper_id": "WRONG", "title": "Wrong Sheet", "authors": "Ignored"}]).to_excel(
                writer,
                sheet_name="Other",
                index=False,
            )
            pd.DataFrame(
                [{"paper_id": "X001", "acceptance_status": "accepted", "title": "Excel Master", "authors": "Ada"}]
            ).to_excel(writer, sheet_name=MASTER_SHEET_NAME, index=False)
        buffer.seek(0)

        result = import_initial_papers(
            SimpleUploadedFile("initial.xlsx", buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        )

        self.assertEqual(result, {"created": 1, "updated": 0})
        self.assertTrue(InitialPaper.objects.filter(paper_id="X001", title="Excel Master").exists())
        self.assertFalse(InitialPaper.objects.filter(paper_id="WRONG").exists())

    def test_final_import_resolves_official_id_and_attaches_swapped_files_by_extension(self):
        self.make_master_paper("R001", "Camera Ready Title", "Ada")
        result = import_final_submissions(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,r1,Camera Ready Title,Ada,2026-05-07 09:00:00\n",
            ),
            [
                self.uploaded_file("10_file_Submit_Source.pdf", b"pdf bytes"),
                self.uploaded_file("10_file_Submit_PDF.docx", b"source bytes"),
            ],
        )

        submission = FinalSubmission.objects.get(final_submission_id="10")
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["attached_pdfs"], 1)
        self.assertEqual(result["attached_sources"], 1)
        self.assertEqual(submission.paper_id_filled, "R001")
        self.assertTrue(submission.pdf_file.name.endswith(".pdf"))
        self.assertTrue(submission.source_file.name.endswith(".docx"))

    def test_mapping_workbook_imports_master_start2_and_mapping_table(self):
        buffer = io.BytesIO()
        mapping_columns = [f"c{i}" for i in range(15)]
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame(
                [{"paper_id": "P777", "acceptance_status": "accepted", "title": "Mapped Paper", "authors": "Ada"}]
            ).to_excel(writer, sheet_name=MASTER_SHEET_NAME, index=False)
            pd.DataFrame(
                [{"submission id": "77", "paper-id": "author typo", "title": "Mapped Paper", "authors": "Ada"}]
            ).to_excel(writer, sheet_name=START2_SHEET_NAME, index=False)
            pd.DataFrame([["77", "P777", "", "", "", "", "", "", "", "", "", "", "", "", "77_file_Submit_PDF.pdf"]], columns=mapping_columns).to_excel(
                writer,
                sheet_name=MAPPING_SHEET_NAME,
                index=False,
            )
        buffer.seek(0)

        result = import_final_submissions(
            SimpleUploadedFile("mapping.xlsx", buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        )

        submission = FinalSubmission.objects.get(final_submission_id="77")
        self.assertEqual(result["initial_created"], 1)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(submission.paper_id_filled, "P777")
        self.assertEqual(submission.mapping_source, MAPPING_SHEET_NAME)
        self.assertEqual(submission.original_file_name, "77_file_Submit_PDF.pdf")

    def test_final_import_preview_pdf_change_resets_dependent_reviews(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        submission = self.make_final_submission(final_submission_id="10")
        token_payload = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,Ready Paper,Ada,2026-05-07 09:00:00\n",
            ),
            [self.uploaded_file("10_file_Submit_PDF.pdf", b"new pdf bytes")],
        )
        result = apply_import_preview(token_payload["token"])

        submission.refresh_from_db()
        self.assertEqual(result["pdf_reset"], 1)
        self.assertEqual(submission.processing_status, "pending")
        self.assertEqual(submission.extracted_title, "")
        self.assertEqual(submission.format_status, "pending")
        self.assertIsNone(submission.similarity_score)
        self.assertFalse(submission.title_author_verified)


class VersionAndFileSelectionTests(EditorialAcceptanceTestCase):
    def test_active_version_largest_final_id_and_duplicate_flag(self):
        self.make_master_paper("P001")
        old = self.make_final_submission(final_submission_id="9", paper_id_filled="P001")
        new = self.make_final_submission(final_submission_id="10", paper_id_filled="P001")

        determine_active_versions()
        _mark_duplicate_submissions()
        old.refresh_from_db()
        new.refresh_from_db()

        self.assertFalse(old.active_version)
        self.assertTrue(new.active_version)
        self.assertTrue(old.duplicate_submission)
        self.assertIn("Replaced Final Submission", {row["category"] for row in error_report_rows()})

    def test_active_version_upload_date_rule_uses_final_id_as_tiebreaker(self):
        self.make_master_paper("P001")
        settings_obj = AppSetting.load()
        settings_obj.active_version_rule = "upload_date"
        settings_obj.save()
        date_value = timezone.datetime(2026, 5, 7, 9, tzinfo=timezone.get_current_timezone())
        low = self.make_final_submission(final_submission_id="9", paper_id_filled="P001", upload_date=date_value)
        high = self.make_final_submission(final_submission_id="10", paper_id_filled="P001", upload_date=date_value)

        determine_active_versions()
        low.refresh_from_db()
        high.refresh_from_db()

        self.assertFalse(low.active_version)
        self.assertTrue(high.active_version)

    def test_pdf_and_source_publication_precedence(self):
        self.make_master_paper("P001")
        original_pdf = self.make_pdf_file("original.pdf", b"original pdf")
        active_pdf = self.make_pdf_file("active.pdf", b"active pdf")
        corrected_pdf = self.make_pdf_file("corrected.pdf", b"corrected pdf")
        original_source = self.media_root / "source_submissions" / "original.docx"
        original_source.parent.mkdir(parents=True, exist_ok=True)
        original_source.write_bytes(b"original source")
        current_source = self.make_source_file("current.docx", b"current source")
        corrected_source = self.make_source_file("corrected.docx", b"corrected source")
        submission = self.make_final_submission(
            current_file_path=str(active_pdf),
            source_file="source_submissions/original.docx",
            source_current_file_path=str(current_source),
        )
        submission.pdf_file.save("original.pdf", ContentFile(original_pdf.read_bytes()), save=True)
        submission.formatted_pdf_file.save("corrected.pdf", ContentFile(corrected_pdf.read_bytes()), save=True)
        submission.formatted_source_file.save("corrected.docx", ContentFile(corrected_source.read_bytes()), save=True)

        self.assertEqual(publication_pdf_info(submission)["source"], "corrected")
        self.assertEqual(publication_source_info(submission)["source"], "corrected")

        submission.formatted_pdf_file.delete(save=True)
        submission.refresh_from_db()
        self.assertEqual(publication_pdf_info(submission)["source"], "processed")

        submission.formatted_source_file.delete(save=True)
        submission.refresh_from_db()
        source_info = publication_source_info(submission)
        self.assertEqual(source_info["source"], "current")
        self.assertEqual(Path(source_info["path"]), current_source)

    def test_three_round_resubmission_invalidates_old_corrections_and_uses_latest_active_only(self):
        self.make_master_paper("P001", "Complex Replacement", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Complex Replacement",
            extracted_title="Complex Replacement",
            current_file_path=str(self.make_pdf_file("round1.pdf", b"round1 pdf")),
        )
        middle = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="Complex Replacement",
            extracted_title="Complex Replacement",
            current_file_path=str(self.make_pdf_file("round2.pdf", b"round2 pdf")),
        )
        middle.formatted_pdf_file.save("round2_corrected.pdf", ContentFile(b"round2 corrected"), save=True)
        newest = self.make_final_submission(
            final_submission_id="12",
            paper_id_filled="P001",
            final_submission_title="Complex Replacement",
            extracted_title="Complex Replacement",
            current_file_path=str(self.make_pdf_file("round3.pdf", b"round3 pdf")),
            source_current_file_path=str(self.make_source_file("round3.docx", b"round3 source")),
        )

        determine_active_versions()
        _mark_duplicate_submissions()
        old.refresh_from_db()
        middle.refresh_from_db()
        newest.refresh_from_db()

        self.assertFalse(old.active_version)
        self.assertFalse(middle.active_version)
        self.assertTrue(newest.active_version)
        self.assertTrue(old.duplicate_submission)
        self.assertTrue(middle.duplicate_submission)
        self.assertEqual(publication_pdf_info(newest)["path"], newest.current_file_path)

        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            self.assertIn("PDF/P001-Complex Replacement.pdf", names)
            self.assertEqual(archive.read("PDF/P001-Complex Replacement.pdf"), b"round3 pdf")
            self.assertEqual(archive.read("Source/P001-Complex Replacement.docx"), b"round3 source")
            self.assertNotIn(b"round2 corrected", [archive.read(name) for name in names if name.startswith("PDF/")])

    def test_new_pdf_after_page_exception_resets_exception_and_blocks_until_reprocessed(self):
        self.make_master_paper("P001", "Page Exception Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            final_submission_title="Page Exception Paper",
            extracted_title="Page Exception Paper",
            page_count=13,
        )
        rows, _ = exception_rows("all")
        approve_exception(next(row for row in rows if row["type"] == "page"), "Publisher allowed 13 pages.")
        submission.refresh_from_db()
        self.assertTrue(submission.has_valid_page_limit_exception)

        token_payload = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,Page Exception Paper,Ada,2026-05-07 09:00:00\n",
            ),
            [self.uploaded_file("10_file_Submit_PDF.pdf", b"new pdf bytes")],
        )
        apply_import_preview(token_payload["token"])

        submission.refresh_from_db()
        self.assertFalse(submission.page_limit_exception_approved)
        self.assertIsNone(submission.page_count)
        self.assertEqual(submission.processing_status, "pending")
        self.assert_publication_blocked("PDF Not Processed")

    def test_new_source_after_review_resets_title_author_reviews_but_keeps_pdf_processing(self):
        self.make_master_paper("P001", "Source Replacement", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            final_submission_title="Source Replacement",
            extracted_title="Source Replacement",
        )
        token_payload = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,Source Replacement,Ada,2026-05-07 09:00:00\n",
            ),
            [self.uploaded_file("10_file_Submit_Source.docx", b"new source bytes")],
        )
        apply_import_preview(token_payload["token"])

        submission.refresh_from_db()
        self.assertEqual(submission.processing_status, "processed")
        self.assertFalse(submission.title_author_verified)
        self.assertFalse(submission.extracted_title_verified)
        self.assertEqual(submission.format_status, "pending")
        self.assert_publication_blocked("Unverified Title/Author Extraction")


class ComplexReplacementWorkflowTests(EditorialAcceptanceTestCase):
    def test_correcting_mapping_to_paper_with_existing_submission_recomputes_active_and_package(self):
        self.make_master_paper("P001", "Mapped Replacement", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Mapped Replacement",
            extracted_title="Mapped Replacement",
            current_file_path=str(self.make_pdf_file("old.pdf", b"old mapped pdf")),
        )
        wrong = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="WRONG",
            final_submission_title="Mapped Replacement",
            extracted_title="Mapped Replacement",
            current_file_path=str(self.make_pdf_file("new.pdf", b"new mapped pdf")),
            source_current_file_path=str(self.make_source_file("new.docx", b"new mapped source")),
        )

        self.assert_publication_blocked("Unclassified Final Not In Master")
        verify_submission(wrong, "P001")
        old.refresh_from_db()
        wrong.refresh_from_db()

        self.assertFalse(old.active_version)
        self.assertTrue(old.duplicate_submission)
        self.assertTrue(wrong.active_version)
        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.read("PDF/P001-Mapped Replacement.pdf"), b"new mapped pdf")
            self.assertEqual(archive.read("Source/P001-Mapped Replacement.docx"), b"new mapped source")

    def test_replacing_original_pdf_archives_corrected_files_and_resets_all_dependent_state(self):
        self.make_master_paper("P001", "Replace Original", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            final_submission_title="Replace Original",
            extracted_title="Replace Original",
            page_count=13,
            plagiarism_report_path=str(self.make_file("reports/old_report.pdf", b"old report")),
        )
        rows, _ = exception_rows("all")
        approve_exception(next(row for row in rows if row["type"] == "page"), "publisher approved")
        submission.formatted_pdf_file.save("corrected.pdf", ContentFile(b"corrected pdf"), save=True)
        submission.formatted_source_file.save("corrected.docx", ContentFile(b"corrected source"), save=True)
        submission.refresh_from_db()
        self.assertTrue(submission.formatted_pdf_file)
        self.assertTrue(submission.formatted_source_file)

        token_payload = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,Replace Original,Ada,2026-05-07 09:00:00\n",
            ),
            [self.uploaded_file("10_file_Submit_PDF.pdf", b"replacement pdf")],
        )
        apply_import_preview(token_payload["token"])

        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)
        self.assertFalse(submission.formatted_source_file)
        self.assertFalse(submission.page_limit_exception_approved)
        self.assertEqual(submission.processing_status, "pending")
        self.assertEqual(submission.extracted_title, "")
        self.assertEqual(submission.plagiarism_report_path, "")
        self.assertIsNone(submission.similarity_score)
        self.assertEqual(submission.format_status, "pending")
        archive_root = self.media_root / "invalidated_corrected_files" / "10"
        self.assertTrue(any(archive_root.rglob("corrected.pdf")))
        self.assertTrue(any(archive_root.rglob("corrected.docx")))

    def test_external_results_with_old_final_id_do_not_pollute_active_replacement(self):
        self.make_master_paper("P001", "External Replacement", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="External Replacement",
            extracted_title="Old External Title",
            similarity_score=1,
        )
        active = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="External Replacement",
            extracted_title="External Replacement",
            similarity_score=None,
            single_similarity_score=None,
        )
        determine_active_versions()
        _mark_duplicate_submissions()

        result = import_external_results(
            self.uploaded_csv(
                "external.csv",
                "final_submission_id,paper_id,extracted_title,extracted_authors,similarity_score,single_similarity_score\n"
                "10,P001,Old Imported Title,Old Author,5,2\n",
            )
        )

        old.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(result["updated_title_author"], 1)
        self.assertEqual(old.extracted_title, "Old Imported Title")
        self.assertEqual(active.extracted_title, "External Replacement")
        self.assertIsNone(active.similarity_score)
        self.assert_publication_blocked("Missing Plagiarism Result")

    def test_external_results_without_final_id_apply_to_active_replacement_only(self):
        self.make_master_paper("P001", "External Active", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="External Active",
            extracted_title="Old Title",
        )
        active = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="External Active",
            extracted_title="Active Title",
        )
        determine_active_versions()
        _mark_duplicate_submissions()

        import_external_results(
            self.uploaded_csv(
                "external.csv",
                "paper_id,extracted_title,extracted_authors,similarity_score,single_similarity_score\n"
                "P001,Active Imported Title,Active Author,4,1\n",
            )
        )

        old.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(old.extracted_title, "Old Title")
        self.assertEqual(active.extracted_title, "Active Imported Title")
        self.assertFalse(active.title_author_verified)
        self.assertFalse(active.extracted_title_verified)
        self.assert_publication_blocked("Unverified Title/Author Extraction")

    def test_crosscheck_results_and_reports_follow_active_replacement_and_stale_report_blocks(self):
        self.make_master_paper("P001", "Crosscheck Replacement", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Crosscheck Replacement",
            extracted_title="Crosscheck Replacement",
            similarity_score=1,
            single_similarity_score=1,
        )
        active = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="Crosscheck Replacement",
            extracted_title="Crosscheck Replacement",
            similarity_score=None,
            single_similarity_score=None,
        )
        determine_active_versions()
        _mark_duplicate_submissions()

        upload_crosscheck_reports([self.uploaded_file("P001_BATCH.pdf", b"matching report")])
        active.refresh_from_db()
        old.refresh_from_db()
        self.assertTrue(active.plagiarism_report_path.endswith("P001_BATCH.pdf"))
        self.assertEqual(old.plagiarism_report_path, "")

        import_crosscheck_results(
            self.uploaded_csv(
                "crosscheck.csv",
                "filename,plagiarism_percent,single_percent\n"
                "P001_BATCH.pdf,6,2\n",
            )
        )
        active.refresh_from_db()
        self.assertTrue(active.plagiarism_report_stale)
        self.assert_publication_blocked("Stale Plagiarism Report")

        upload_crosscheck_reports([self.uploaded_file("P001_BATCH.pdf", b"updated matching report")])
        active.refresh_from_db()
        self.assertFalse(active.plagiarism_report_stale)
        self.assertEqual(active.similarity_score, 6)
        self.assertEqual(active.single_similarity_score, 2)
        self.assertEqual(publication_readiness_rows(), [])

    def test_not_publishing_reactivated_replacement_requires_fresh_editor_verification(self):
        self.make_master_paper("P001", "Reactivated Paper", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Reactivated Paper",
            extracted_title="Reactivated Paper",
        )
        active = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="Reactivated Paper",
            extracted_title="Reactivated Paper",
        )
        determine_active_versions()
        _mark_duplicate_submissions()
        mark_not_publishing(active, "unpaid", "payment missing")
        self.assert_publication_blocked("no publishable active final submissions")

        undo_not_publishing(active)
        active.refresh_from_db()
        self.assertFalse(active.paper_id_verified)
        self.assertEqual(active.verification_status, "pending")
        self.assert_publication_blocked("Unverified Paper ID")

        verify_submission(active, "P001")
        self.assertEqual(publication_readiness_rows(), [])

    def test_corrected_source_upload_resets_reviews_and_is_packaged(self):
        self.make_master_paper("P001", "Corrected Source", "Ada")
        submission = self.make_final_submission(extracted_title="Corrected Source", final_submission_title="Corrected Source")
        update_formatting_submission(
            submission,
            {
                "corrected_pdf": None,
                "corrected_source": self.uploaded_file("corrected.docx", b"corrected source"),
                "format_status": "review_ok",
                "format_notes": "source fixed",
            },
        )
        submission.refresh_from_db()
        self.assertFalse(submission.title_author_verified)
        self.assertFalse(submission.extracted_title_verified)
        verify_title_author(submission)
        verify_extracted_title(submission)
        submission.format_status = "review_ok"
        submission.save(update_fields=["format_status"])

        zip_path = export_publication_package()
        self.assert_zip_contains_manifest_pdf_source(
            zip_path,
            [
                {
                    "paper_id": "P001",
                    "title": "Corrected Source",
                    "author_count": 2,
                    "page_count": 8,
                    "similarity_score": "1",
                    "single_similarity_score": "1",
                    "pdf_arcname": "PDF/P001-Corrected Source.pdf",
                    "source_arcname": "Source/P001-Corrected Source.docx",
                    "pdf_bytes": Path(submission.current_file_path).read_bytes(),
                    "source_bytes": b"corrected source",
                }
            ],
        )


class PublicationReadinessTests(EditorialAcceptanceTestCase):
    def test_empty_master_and_empty_publishable_set_are_blocked(self):
        self.assert_publication_blocked("Paper Master List is empty")
        self.make_master_paper("P001")
        submission = self.make_final_submission()
        mark_not_publishing(submission, "unpaid", "not paid")

        self.assertEqual(publication_readiness_rows(), [])
        self.assert_publication_blocked("no publishable active final submissions")

    def test_missing_final_submission_blocks_publication(self):
        self.make_master_paper("P001")
        self.assert_publication_blocked("Missing Final Submission")

    def test_force_publication_package_creates_draft_with_warning_csv(self):
        self.make_master_paper("P001", "Needs Review Paper", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Needs Review Paper",
            extracted_title="Needs Review Paper",
            title_author_verified=False,
        )

        self.assert_publication_blocked("Unverified Title/Author Extraction")
        zip_path = export_publication_package(force=True)

        self.assertIn("publication_package_draft_", Path(zip_path).name)
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            manifest_name = next(name for name in names if name.startswith("publication_manifest_"))
            warnings_name = next(
                name for name in names if name.startswith("publication_package_warnings_")
            )
            manifest_text = archive.read(manifest_name).decode("utf-8-sig")
            warning_text = archive.read(warnings_name).decode("utf-8-sig")
            self.assertIn("P001", manifest_text)
            self.assertIn("Unverified Title/Author Extraction", warning_text)
            self.assertIn("PDF/P001-Needs Review Paper.pdf", names)
            self.assertIn("Source/P001-Needs Review Paper.docx", names)

    def test_force_publication_package_skips_missing_final_and_missing_files(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        self.make_master_paper("P002", "No Final Paper", "Grace")
        self.make_master_paper("P003", "Missing Source Paper", "Katherine")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
        )
        missing_source = self.make_final_submission(
            final_submission_id="30",
            paper_id_filled="P003",
            final_submission_title="Missing Source Paper",
            extracted_title="Missing Source Paper",
        )
        Path(missing_source.source_current_file_path).unlink()

        zip_path = export_publication_package(force=True)

        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            manifest_name = next(name for name in names if name.startswith("publication_manifest_"))
            warnings_name = next(
                name for name in names if name.startswith("publication_package_warnings_")
            )
            manifest_rows = list(
                csv.DictReader(io.StringIO(archive.read(manifest_name).decode("utf-8-sig")))
            )
            warning_text = archive.read(warnings_name).decode("utf-8-sig")
            self.assertEqual([row["ID"] for row in manifest_rows], ["P001"])
            self.assertIn("Skipped from draft package", warning_text)
            self.assertIn("Missing Final Submission", warning_text)
            self.assertIn("Missing Source File", warning_text)
            self.assertIn("PDF/P001-Ready Paper.pdf", names)
            self.assertNotIn("PDF/P003-Missing Source Paper.pdf", names)

    def test_force_publication_package_still_blocks_when_nothing_can_be_packaged(self):
        self.make_master_paper("P001")

        with self.assertRaisesMessage(ValueError, "no papers have both publication PDF and source files"):
            export_publication_package(force=True)

    def test_manually_verified_title_mismatch_warns_ui_but_allows_publication(self):
        self.make_master_paper("P001", "Paper Master Title", "Ada")
        self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Different Final Title",
            extracted_title="Different Final Title",
            paper_id_verified=True,
            verification_status="verified",
            verification_message="Paper ID manually verified; title differs.",
        )

        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Final Title / Paper Master Title Mismatch", categories)
        self.assertEqual(dashboard_counts()["title_mismatches"], 0)
        self.assertTrue(Path(export_publication_package()).exists())
        response = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        self.assertContains(response, "ID verified, title differs")

    def test_not_publishing_excludes_submission_from_readiness_dashboard_and_package(self):
        self.make_master_paper("P001")
        self.make_master_paper("P002", "Publishing Paper", "Grace")
        excluded = self.make_final_submission(final_submission_id="1", paper_id_filled="P001")
        included = self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Publishing Paper",
            extracted_title="Publishing Paper",
            final_submission_authors="Grace Hopper",
            extracted_authors="Grace Hopper",
        )
        mark_not_publishing(excluded, "withdrawn", "withdrawn by author")

        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Missing Final Submission", categories)
        self.assertEqual(dashboard_counts()["missing_final_submissions"], 0)
        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertIn("PDF/P002-Publishing Paper.pdf", archive.namelist())
            self.assertNotIn("PDF/P001-Ready Paper.pdf", archive.namelist())

        undo_not_publishing(excluded)
        self.assert_publication_blocked("Unverified Paper ID")

    def test_every_core_readiness_blocker_blocks_publication(self):
        cases = [
            ("unverified", {"paper_id_verified": False, "auto_verify_blocked": True}, "Unverified Paper ID"),
            (
                "title mismatch",
                {
                    "paper_id_verified": False,
                    "verification_status": "title_mismatch",
                    "final_submission_title": "Different Paper",
                },
                "Final Title / Paper Master Title Mismatch",
            ),
            ("missing pdf", {"current_file_path": ""}, "Missing PDF"),
            ("pdf not processed", {"processing_status": "pending", "pdf_hash": "", "page_count": None}, "PDF Not Processed"),
            ("missing source", {"source_current_file_path": ""}, "Missing Source File"),
            ("page high", {"page_count": 13}, "Page Limit Exceeded"),
            ("page low", {"page_count": 5}, "Below Page Minimum"),
            ("pdf error", {"processing_status": "error", "processing_message": "bad pdf"}, "PDF Processing Error"),
            ("missing extracted title", {"extracted_title": ""}, "Missing Extracted Title"),
            ("missing extracted authors", {"extracted_authors": ""}, "Missing Extracted Authors"),
            ("unverified title author", {"title_author_verified": False}, "Unverified Title/Author Extraction"),
            ("unverified title match", {"extracted_title_verified": False}, "Unverified Extracted Title Match"),
            ("format", {"format_status": "pending"}, "Formatting Not Review OK"),
            ("missing plagiarism", {"similarity_score": None}, "Missing Plagiarism Result"),
            ("plagiarism p", {"similarity_score": 36}, "Plagiarism % Over Threshold"),
            ("plagiarism s", {"single_similarity_score": 11}, "Single % Over Threshold"),
            (
                "too many authors",
                {"extracted_authors": "A; B; C; D; E; F", "final_submission_authors": "A; B; C; D; E; F"},
                "Author Over Limit",
            ),
        ]
        for index, (_label, overrides, expected_category) in enumerate(cases, start=1):
            with self.subTest(expected_category=expected_category):
                InitialPaper.objects.all().delete()
                FinalSubmission.objects.all().delete()
                self.make_master_paper(f"P{index:03}", "Ready Paper", "Ada")
                self.make_final_submission(final_submission_id=str(index), paper_id_filled=f"P{index:03}", **overrides)
                self.assert_publication_blocked(expected_category)

    def test_corrected_pdf_needs_processing_blocks_publication(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(pdf_hash="stale-original-hash")
        submission.formatted_pdf_file.save("corrected.pdf", ContentFile(b"corrected pdf"), save=True)

        self.assert_publication_blocked("Corrected PDF Not Processed")

    def test_multiple_active_finals_for_same_paper_block_publication(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        self.make_final_submission(final_submission_id="10", paper_id_filled="P001")
        self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            current_file_path=str(self.make_pdf_file("second-active.pdf", b"second active pdf")),
            source_current_file_path=str(self.make_source_file("second-active.docx", b"second active source")),
        )

        self.assert_publication_blocked("Multiple Active Final Submissions")

    def test_numeric_author_entered_id_resolves_only_when_title_matches(self):
        self.make_master_paper("R032", "Numeric Match", "Ada")
        self.make_master_paper("R033", "Other Paper", "Grace")

        result = import_final_submissions(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "32,32,Numeric Match,Ada,2026-05-07 09:00:00\n",
            )
        )

        submission = FinalSubmission.objects.get(final_submission_id="32")
        self.assertEqual(result["created"], 1)
        self.assertEqual(submission.paper_id_filled, "R032")
        self.assertTrue(submission.paper_id_verified)

        result = import_final_submissions(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "33,33,Wrong Numeric Title,Ada,2026-05-07 09:00:00\n",
            )
        )

        submission = FinalSubmission.objects.get(final_submission_id="33")
        self.assertEqual(result["created"], 1)
        self.assertEqual(submission.paper_id_filled, "33")
        self.assertFalse(submission.paper_id_verified)

    def test_numeric_author_entered_id_uses_title_to_choose_between_prefixed_candidates(self):
        self.make_master_paper("R032", "Research Track Paper", "Ada")
        self.make_master_paper("X032", "Workshop Track Paper", "Grace")

        import_final_submissions(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "32,32,Workshop Track Paper,Grace,2026-05-07 09:00:00\n"
                "33,32,Unknown Track Paper,Ada,2026-05-07 09:05:00\n",
            )
        )

        matched = FinalSubmission.objects.get(final_submission_id="32")
        unmatched = FinalSubmission.objects.get(final_submission_id="33")
        self.assertEqual(matched.paper_id_filled, "X032")
        self.assertTrue(matched.paper_id_verified)
        self.assertEqual(unmatched.paper_id_filled, "32")
        self.assertFalse(unmatched.paper_id_verified)

    def test_page_limit_exception_allows_publication_until_page_count_changes(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(page_count=13)
        self.assert_publication_blocked("Page Limit Exceeded")

        submission.page_limit_exception_approved = True
        submission.page_limit_exception_reason = "Chair approved invited paper length."
        submission.page_limit_exception_page_count = 13
        submission.page_limit_exception_approved_at = timezone.now()
        submission.save()

        blocking_categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Page Limit Exceeded", blocking_categories)
        self.assertIn("Allowed Page Exception", {row["category"] for row in error_report_rows()})
        self.assertTrue(Path(export_publication_package()).exists())

        submission.page_count = 14
        submission.save(update_fields=["page_count", "updated_at"])
        self.assert_publication_blocked("Page Limit Exceeded")

    def test_single_paper_author_number_exception_allows_publication_until_authors_change(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(
            extracted_authors="A; B; C; D; E; F",
            final_submission_authors="A; B; C; D; E; F",
        )
        self.assert_publication_blocked("Author Over Limit")

        submission.author_number_exception_approved = True
        submission.author_number_exception_reason = "Panel paper approved by chairs."
        submission.author_number_exception_author_count = 6
        submission.author_number_exception_approved_at = timezone.now()
        submission.save()

        blocking_categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Author Over Limit", blocking_categories)
        self.assertIn("Allowed Author Number Exception", {row["category"] for row in error_report_rows()})
        self.assertTrue(Path(export_publication_package()).exists())

        submission.extracted_authors = "A; B; C; D; E; F; G"
        submission.save(update_fields=["extracted_authors", "updated_at"])
        self.assert_publication_blocked("Author Over Limit")

    def test_same_author_over_paper_limit_blocks_publication(self):
        for index in range(1, 5):
            paper_id = f"P{index:03}"
            self.make_master_paper(paper_id, f"Paper {index}", "Repeat Author")
            self.make_final_submission(
                final_submission_id=str(index),
                paper_id_filled=paper_id,
                final_submission_title=f"Paper {index}",
                extracted_title=f"Paper {index}",
                final_submission_authors="Repeat Author",
                extracted_authors="Repeat Author",
            )

        self.assert_publication_blocked("Author Over Limit")
        self.assertIn("Author Over Limit", {row["category"] for row in error_report_rows()})

    def test_author_paper_count_exception_allows_publication_until_count_changes(self):
        for index in range(1, 5):
            paper_id = f"P{index:03}"
            self.make_master_paper(paper_id, f"Paper {index}", "Repeat Author")
            self.make_final_submission(
                final_submission_id=str(index),
                paper_id_filled=paper_id,
                final_submission_title=f"Paper {index}",
                extracted_title=f"Paper {index}",
                final_submission_authors="Repeat Author",
                extracted_authors="Repeat Author",
            )
        self.assert_publication_blocked("Author Over Limit")

        waiver = AuthorLimitWaiver.objects.create(
            normalized_author_name="repeat author",
            display_author_name="Repeat Author",
            approved=True,
            reason="Program chair approved four accepted papers.",
            approved_publication_paper_count=4,
            approved_at=timezone.now(),
        )
        self.assertTrue(waiver.is_valid_for_count(4))
        blocking_categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Author Over Limit", blocking_categories)
        self.assertIn("Allowed Author Paper Count Exception", {row["category"] for row in error_report_rows()})
        self.assertTrue(Path(export_publication_package()).exists())

        self.make_master_paper("P005", "Paper 5", "Repeat Author")
        self.make_final_submission(
            final_submission_id="5",
            paper_id_filled="P005",
            final_submission_title="Paper 5",
            extracted_title="Paper 5",
            final_submission_authors="Repeat Author",
            extracted_authors="Repeat Author",
        )
        self.assert_publication_blocked("Author Over Limit")

    def test_author_splitter_counts_individual_authors(self):
        self.assertEqual(split_authors("A and B"), ["A", "B"])
        self.assertEqual(split_authors("A, B, and C"), ["A", "B", "C"])
        self.assertEqual(split_authors("A, B and C"), ["A", "B", "C"])
        self.assertEqual(split_authors("A; B; C"), ["A", "B", "C"])

    def test_duplicate_author_in_same_paper_blocks_until_reviewed(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(extracted_authors="Ada Lovelace, Alan Turing, Ada Lovelace")

        duplicates = duplicate_authors_in_paper(submission.extracted_authors)
        self.assertEqual(duplicates[0]["normalized_author_name"], "ada lovelace")
        self.assertEqual(duplicates[0]["count"], 2)
        self.assert_publication_blocked("Duplicate Author In Paper")

        submission.duplicate_author_review_status = "review_ok"
        submission.duplicate_author_review_notes = "confirmed different people"
        submission.duplicate_author_reviewed_at = timezone.now()
        submission.save(
            update_fields=[
                "duplicate_author_review_status",
                "duplicate_author_review_notes",
                "duplicate_author_reviewed_at",
                "updated_at",
            ]
        )
        self.assertNotIn(
            "Duplicate Author In Paper",
            {row["category"] for row in publication_readiness_rows()},
        )

        submission.duplicate_author_review_status = "pending"
        submission.duplicate_author_review_notes = ""
        submission.duplicate_author_reviewed_at = None
        submission.save(
            update_fields=[
                "duplicate_author_review_status",
                "duplicate_author_review_notes",
                "duplicate_author_reviewed_at",
                "updated_at",
            ]
        )
        self.assert_publication_blocked("Duplicate Author In Paper")

    def test_same_author_in_same_paper_keeps_rows_but_counts_one_publication_paper(self):
        self.make_master_paper("P001")
        self.make_final_submission(extracted_authors="Ada Lovelace, Alan Turing, Ada Lovelace")

        rebuild_paper_authors()
        rows = author_count_rows()
        ada_row = next(row for row in rows if row["normalized_author_name"] == "ada lovelace")
        self.assertEqual(ada_row["publication_paper_count"], 1)
        self.assertEqual(ada_row["duplicate_author_papers"], "P001")
        self.assertEqual(
            PaperAuthor.objects.filter(normalized_author_name="ada lovelace").count(),
            2,
        )

    def test_author_count_displays_distinct_original_author_spellings(self):
        self.make_master_paper("P001", "First", "Chih-Wei Hsu")
        self.make_master_paper("P002", "Second", "ChihWei Hsu")
        self.make_master_paper("P003", "Third", "Chih-Wei Hsu")
        self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="First",
            extracted_title="First",
            extracted_authors="Chih-Wei Hsu",
        )
        self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Second",
            extracted_title="Second",
            extracted_authors="ChihWei Hsu",
        )
        self.make_final_submission(
            final_submission_id="3",
            paper_id_filled="P003",
            final_submission_title="Third",
            extracted_title="Third",
            extracted_authors="Chih-Wei Hsu",
        )

        rebuild_paper_authors()
        rows = author_count_rows()
        author_row = next(row for row in rows if row["normalized_author_name"] == "chihwei hsu")

        self.assertEqual(author_row["publication_paper_count"], 3)
        self.assertEqual(author_row["display_author_names"], ["Chih-Wei Hsu", "ChihWei Hsu"])
        self.assertEqual(author_row["display_author_name"], "Chih-Wei Hsu; ChihWei Hsu")

        response = self.client.get(reverse("submissions:author_count"))
        self.assertContains(response, "Display Names")
        self.assertContains(response, "Chih-Wei Hsu")
        self.assertContains(response, "ChihWei Hsu")

        frame = author_count_frame()
        exported_row = frame[frame["normalized_author_name"] == "chihwei hsu"].iloc[0]
        self.assertEqual(exported_row["display_author_name"], "Chih-Wei Hsu; ChihWei Hsu")

    def test_unclassified_final_blocks_until_marked_not_publishing(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        self.make_final_submission(final_submission_id="ready", paper_id_filled="P001")
        submission = self.make_final_submission(paper_id_filled="UNKNOWN")
        self.assert_publication_blocked("Unclassified Final Not In Master")

        mark_not_publishing(submission, "not_in_master", "wrong upload")
        self.assertEqual(publication_readiness_rows(), [])


class DuplicatePublicationTests(EditorialAcceptanceTestCase):
    def test_duplicate_title_pdf_and_source_block_publication_and_appear_in_reports(self):
        shared_pdf = self.make_pdf_file("shared.pdf", b"shared pdf")
        shared_source = self.make_source_file("shared.docx", b"shared source")
        for paper_id, final_id in [("P001", "1"), ("P002", "2")]:
            self.make_master_paper(paper_id, "Duplicate Title", "Ada")
            self.make_final_submission(
                final_submission_id=final_id,
                paper_id_filled=paper_id,
                current_file_path=str(shared_pdf),
                source_current_file_path=str(shared_source),
                final_submission_title="Duplicate Title",
                extracted_title="Duplicate Title",
            )

        categories = {row["category"] for row in error_report_rows()}
        self.assertIn("Duplicate Publication Title", categories)
        self.assertIn("Duplicate Publication PDF", categories)
        self.assertIn("Duplicate Publication Source", categories)
        self.assertTrue(publication_duplicate_map())
        self.assert_publication_blocked("Duplicate Publication Title")

    def test_replaced_old_duplicate_file_does_not_create_publication_duplicate(self):
        self.make_master_paper("P001", "Current One", "Ada")
        self.make_master_paper("P002", "Current Two", "Alan")
        shared_pdf = self.make_pdf_file("old-shared.pdf", b"old shared")
        self.make_final_submission(final_submission_id="1", paper_id_filled="P001", current_file_path=str(shared_pdf))
        self.make_final_submission(final_submission_id="2", paper_id_filled="P001", current_file_path=str(shared_pdf))
        self.make_final_submission(
            final_submission_id="3",
            paper_id_filled="P002",
            final_submission_title="Current Two",
            extracted_title="Current Two",
            extracted_authors="Alan Turing",
            final_submission_authors="Alan Turing",
        )
        determine_active_versions()
        _mark_duplicate_submissions()

        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Duplicate Publication PDF", categories)


class PublicationPackageManifestTests(EditorialAcceptanceTestCase):
    def test_package_sanitizes_paper_id_in_zip_filenames(self):
        self.make_master_paper("R/032", "Slash Paper", "Ada")
        self.make_final_submission(
            final_submission_id="32",
            paper_id_filled="R/032",
            start2_paper_id_raw="R/032",
            final_submission_title="Slash Paper",
            extracted_title="Slash Paper",
            current_file_path=str(self.make_pdf_file("slash.pdf", b"slash pdf")),
            source_current_file_path=str(self.make_source_file("slash.docx", b"slash source")),
        )

        zip_path = export_publication_package()

        with zipfile.ZipFile(zip_path) as archive:
            self.assertIn("PDF/R_032-Slash Paper.pdf", archive.namelist())
            self.assertIn("Source/R_032-Slash Paper.docx", archive.namelist())
            self.assertNotIn("PDF/R/032-Slash Paper.pdf", archive.namelist())

    def test_successful_package_contains_manifest_and_matching_files(self):
        self.make_master_paper("P001", "First Camera Ready", "Ada; Alan")
        self.make_master_paper("P002", "Second Camera Ready", "Grace")
        first_pdf = self.make_pdf_file("first.pdf", b"first pdf")
        first_source = self.make_source_file("first.docx", b"first source")
        second_pdf = self.make_pdf_file("second.pdf", b"second pdf")
        second_source = self.make_source_file("second.tex", b"second source")
        self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="First Camera Ready",
            extracted_title="First Camera Ready",
            extracted_authors="Ada Lovelace; Alan Turing",
            current_file_path=str(first_pdf),
            source_current_file_path=str(first_source),
            page_count=8,
            similarity_score=2,
            single_similarity_score=1,
        )
        self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Second Camera Ready",
            extracted_title="Second Camera Ready",
            final_submission_authors="Grace Hopper",
            extracted_authors="Grace Hopper",
            current_file_path=str(second_pdf),
            source_current_file_path=str(second_source),
            page_count=9,
            similarity_score=3,
            single_similarity_score=2,
        )

        zip_path = export_publication_package()

        self.assert_zip_contains_manifest_pdf_source(
            zip_path,
            [
                {
                    "paper_id": "P001",
                    "title": "First Camera Ready",
                    "author_count": 2,
                    "page_count": 8,
                    "similarity_score": "2",
                    "single_similarity_score": "1",
                    "pdf_arcname": "PDF/P001-First Camera Ready.pdf",
                    "source_arcname": "Source/P001-First Camera Ready.docx",
                    "pdf_bytes": b"first pdf",
                    "source_bytes": b"first source",
                },
                {
                    "paper_id": "P002",
                    "title": "Second Camera Ready",
                    "author_count": 1,
                    "page_count": 9,
                    "similarity_score": "3",
                    "single_similarity_score": "2",
                    "pdf_arcname": "PDF/P002-Second Camera Ready.pdf",
                    "source_arcname": "Source/P002-Second Camera Ready.tex",
                    "pdf_bytes": b"second pdf",
                    "source_bytes": b"second source",
                },
            ],
        )


class ViewWorkflowSmokeTests(EditorialAcceptanceTestCase):
    def test_get_smoke_for_editorial_pages(self):
        paths = [
            reverse("submissions:dashboard"),
            reverse("submissions:initial_paper_list"),
            reverse("submissions:final_submission_list"),
            reverse("submissions:organized_list"),
            reverse("submissions:verify_paper_ids"),
            reverse("submissions:not_publishing_list"),
            reverse("submissions:process"),
            reverse("submissions:title_author_extraction"),
            reverse("submissions:formatting"),
            reverse("submissions:active_versions"),
            reverse("submissions:old_versions"),
            reverse("submissions:error_report"),
            reverse("submissions:exceptions_center"),
            reverse("submissions:author_count"),
            reverse("submissions:integration"),
            reverse("submissions:settings"),
            reverse("submissions:export_reports"),
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_settings_can_reset_temp_folder_paths(self):
        settings_obj = AppSetting.load()
        self.assertTrue(str(settings_obj.reports_folder).startswith(str(self.root)))
        response = self.client.post(
            reverse("submissions:settings"),
            {"action": "reset_folders"},
        )
        self.assertEqual(response.status_code, 302)
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.reports_folder, "data/reports")

    def test_exception_center_actions_require_notes_and_save_state(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(
            page_count=13,
            extracted_authors="A; B; C; D; E; F",
            final_submission_authors="A; B; C; D; E; F",
        )

        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {"exception_key": f"page:{submission.pk}", "action": "approve_exception"},
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.page_limit_exception_approved)

        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {
                "exception_key": f"page:{submission.pk}",
                "action": "approve_exception",
                "reason": "chair approved",
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.has_valid_page_limit_exception)

        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {
                "exception_key": f"author_number:{submission.pk}",
                "action": "approve_exception",
                "reason": "panel paper",
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.author_number_exception_author_count, 6)

        for index in range(2, 5):
            paper_id = f"P{index:03}"
            self.make_master_paper(paper_id, f"Paper {index}", "A")
            self.make_final_submission(
                final_submission_id=str(index),
                paper_id_filled=paper_id,
                final_submission_title=f"Paper {index}",
                extracted_title=f"Paper {index}",
                final_submission_authors="A",
                extracted_authors="A",
            )
        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {
                "exception_key": "author_limit:a",
                "action": "approve_exception",
                "reason": "chair approved author workload",
            },
        )
        self.assertEqual(response.status_code, 302)
        waiver = AuthorLimitWaiver.objects.get(normalized_author_name="a")
        self.assertTrue(waiver.is_valid_for_count(4))

    def test_post_workflows_for_verify_not_publishing_formatting_and_exports(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(paper_id_verified=False, verification_status="pending")

        response = self.client.post(
            reverse("submissions:verify_paper_ids"),
            {"submission_id": submission.pk, "corrected_paper_id": "P001"},
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)

        response = self.client.post(
            reverse("submissions:verify_paper_ids"),
            {"submission_id": submission.pk, "action": "unverify"},
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)
        verify_submission(submission, "P001")

        response = self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": submission.pk,
                "action": "mark_not_publishing",
                "publication_exclusion_reason": "unpaid",
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.excluded_from_publication)

        response = self.client.post(
            reverse("submissions:not_publishing_list"),
            {"submission_id": submission.pk, "action": "undo_not_publishing"},
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.excluded_from_publication)
        verify_submission(submission, "P001")

        response = self.client.post(
            reverse("submissions:formatting"),
            {
                "submission_id": submission.pk,
                "format_status": "review_ok",
                "format_notes": "corrected",
                "corrected_source": self.uploaded_file("fixed.docx", b"fixed source"),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.formatted_source_file)

        response = self.client.get(reverse("submissions:final_submission_edit", args=[submission.pk]))
        self.assertEqual(response.status_code, 200)

        self.assertNotContains(response, "plagiarism_report_path")

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            {
                "final_submission_id": submission.final_submission_id,
                "start2_paper_id_raw": submission.start2_paper_id_raw,
                "paper_id_filled": submission.paper_id_filled,
                "final_submission_title": submission.final_submission_title,
                "final_submission_authors": submission.final_submission_authors,
                "upload_date": submission.upload_date.strftime("%Y-%m-%dT%H:%M:%S"),
                "extracted_title": submission.extracted_title,
                "extracted_authors": submission.extracted_authors,
                "title_author_source": submission.title_author_source,
                "title_author_extraction_message": submission.title_author_extraction_message,
                "title_author_review_status": submission.title_author_review_status,
                "duplicate_author_review_status": submission.duplicate_author_review_status,
                "duplicate_author_review_notes": submission.duplicate_author_review_notes,
                "extracted_title_match_message": submission.extracted_title_match_message,
                "extracted_title_verified": "on",
                "similarity_score": "2",
                "single_similarity_score": "1",
                "processing_message": submission.processing_message,
                "publication_exclusion_reason": submission.publication_exclusion_reason,
                "publication_exclusion_notes": submission.publication_exclusion_notes,
                "plagiarism_report_file": SimpleUploadedFile(
                    "report.pdf", b"%PDF report", content_type="application/pdf"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.plagiarism_report_path.endswith("_report.pdf"))
        self.assertFalse(submission.plagiarism_report_stale)
        self.assertEqual(submission.similarity_score, 2)
        self.assertEqual(submission.single_similarity_score, 1)
        report_path = submission.plagiarism_report_path

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            {
                "final_submission_id": submission.final_submission_id,
                "start2_paper_id_raw": submission.start2_paper_id_raw,
                "paper_id_filled": submission.paper_id_filled,
                "final_submission_title": submission.final_submission_title,
                "final_submission_authors": submission.final_submission_authors,
                "upload_date": submission.upload_date.strftime("%Y-%m-%dT%H:%M:%S"),
                "extracted_title": submission.extracted_title,
                "extracted_authors": submission.extracted_authors,
                "title_author_source": submission.title_author_source,
                "title_author_extraction_message": submission.title_author_extraction_message,
                "title_author_review_status": submission.title_author_review_status,
                "duplicate_author_review_status": submission.duplicate_author_review_status,
                "duplicate_author_review_notes": submission.duplicate_author_review_notes,
                "extracted_title_match_message": submission.extracted_title_match_message,
                "extracted_title_verified": "on",
                "similarity_score": "3",
                "single_similarity_score": "2",
                "processing_message": submission.processing_message,
                "publication_exclusion_reason": submission.publication_exclusion_reason,
                "publication_exclusion_notes": submission.publication_exclusion_notes,
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.plagiarism_report_path, report_path)
        self.assertTrue(submission.plagiarism_report_stale)

        response = self.client.get(reverse("submissions:final_submission_edit", args=[submission.pk]))
        self.assertContains(response, "Old report")

        verify_title_author(submission)
        verify_extracted_title(submission)
        submission.format_status = "review_ok"
        submission.plagiarism_report_stale = False
        submission.save(update_fields=["format_status", "plagiarism_report_stale"])

        response = self.client.post(reverse("submissions:export_reports"), {"action": "publication_package"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/zip", response["Content-Type"])

        unverify_title_author(submission)
        response = self.client.post(reverse("submissions:export_reports"), {"action": "publication_package"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Publication package is not ready")
        self.assertContains(response, "Unverified Title/Author Extraction")
        self.assertContains(response, "Open Readiness Issues")

    def test_identical_title_auto_verifies_without_manual_click(self):
        self.make_master_paper("P001", title="Exactly Matching Title")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Exactly Matching Title",
            extracted_title="Exactly Matching Title",
            paper_id_verified=False,
            verification_status="pending",
            auto_verify_blocked=False,
        )

        rows = verification_rows(FinalSubmission.objects.filter(pk=submission.pk))

        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "pending")
        self.assertEqual(rows[0]["score"], 100)
        self.assertTrue(rows[0]["is_verified"])
        self.assertFalse(rows[0]["needs_verification"])
        self.assertFalse(
            any(row["category"] == "Unverified Paper ID" for row in publication_readiness_rows())
        )

    def test_manual_editing_paper_id_resets_verification_and_recomputes_active_versions(self):
        self.make_master_paper("P001", title="Original Paper")
        self.make_master_paper("P002", title="Different Master Paper")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Original Paper",
            extracted_title="Original Paper",
            paper_id_verified=True,
            verification_status="verified",
        )

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            {
                "final_submission_id": submission.final_submission_id,
                "start2_paper_id_raw": "P002",
                "paper_id_filled": "P002",
                "final_submission_title": "Original Paper",
                "final_submission_authors": submission.final_submission_authors,
                "upload_date": submission.upload_date.strftime("%Y-%m-%dT%H:%M:%S"),
                "extracted_title": submission.extracted_title,
                "extracted_authors": submission.extracted_authors,
                "title_author_source": submission.title_author_source,
                "title_author_extraction_message": submission.title_author_extraction_message,
                "title_author_review_status": submission.title_author_review_status,
                "duplicate_author_review_status": submission.duplicate_author_review_status,
                "duplicate_author_review_notes": submission.duplicate_author_review_notes,
                "extracted_title_match_message": submission.extracted_title_match_message,
                "extracted_title_verified": "on",
                "similarity_score": "1",
                "single_similarity_score": "1",
                "processing_message": submission.processing_message,
                "publication_exclusion_reason": submission.publication_exclusion_reason,
                "publication_exclusion_notes": submission.publication_exclusion_notes,
            },
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.paper_id_filled, "P002")
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "title_mismatch")
        self.assert_publication_blocked("Final Title / Paper Master Title Mismatch")

    def test_manual_edit_title_diff_requires_editor_verification_then_can_publish(self):
        self.make_master_paper("P001", title="Original Accepted Title")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Original Accepted Title",
            extracted_title="Original Accepted Title",
            paper_id_verified=True,
            verification_status="verified",
        )

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                final_submission_title="Final Revised Title For Publication",
                extracted_title="Final Revised Title For Publication",
            ),
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "title_mismatch")
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assert_publication_blocked("Unverified Paper ID")

        verify_submission(submission, "P001")
        verify_title_author(submission)
        verify_extracted_title(submission)

        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "verified")
        self.assertIn("manually verified", submission.verification_message)
        self.assertEqual(publication_readiness_rows(), [])
        self.assertTrue(Path(export_publication_package()).exists())

    def test_manual_edit_final_id_recalculates_active_version_and_duplicates(self):
        self.make_master_paper("P001", title="Ready Paper")
        older = self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            active_version=False,
            duplicate_submission=True,
        )
        newer = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            active_version=True,
            duplicate_submission=False,
        )
        determine_active_versions()
        _mark_duplicate_submissions()

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[older.pk]),
            self.final_submission_form_data(older, final_submission_id="11"),
        )

        self.assertEqual(response.status_code, 302)
        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertTrue(older.active_version)
        self.assertFalse(older.duplicate_submission)
        self.assertFalse(newer.active_version)
        self.assertTrue(newer.duplicate_submission)

    def test_manual_edit_pdf_change_resets_downstream_state_and_invalidates_corrected_files(self):
        self.make_master_paper("P001", title="Ready Paper")
        submission = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            plagiarism_report_path=str(self.make_pdf_file("20_report.pdf", b"%PDF report")),
        )
        submission.pdf_file.save("original.pdf", ContentFile(b"%PDF original"), save=False)
        submission.formatted_pdf_file.save("corrected.pdf", ContentFile(b"%PDF corrected"), save=False)
        submission.formatted_source_file.save("corrected.docx", ContentFile(b"corrected source"), save=False)
        submission.formatted_pdf_uploaded_at = timezone.now()
        submission.formatted_source_uploaded_at = timezone.now()
        submission.save()

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                pdf_file=SimpleUploadedFile(
                    "replacement.pdf", b"%PDF replacement", content_type="application/pdf"
                ),
            ),
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)
        self.assertFalse(submission.formatted_source_file)
        self.assertEqual(submission.processing_status, "pending")
        self.assertIsNone(submission.page_count)
        self.assertEqual(submission.pdf_hash, "")
        self.assertEqual(submission.extracted_title, "")
        self.assertEqual(submission.extracted_authors, "")
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertEqual(submission.format_status, "pending")
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)
        self.assertEqual(submission.plagiarism_report_path, "")
        self.assertIn("replacement", submission.current_file_path)
        self.assert_publication_blocked("PDF Not Processed")

    def test_manual_edit_source_change_keeps_pdf_processing_but_resets_reviews(self):
        self.make_master_paper("P001", title="Ready Paper")
        submission = self.make_final_submission(
            final_submission_id="21",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
        )
        original_page_count = submission.page_count
        original_pdf_hash = submission.pdf_hash
        submission.source_file.save("original.docx", ContentFile(b"source original"), save=False)
        submission.formatted_source_file.save("corrected.docx", ContentFile(b"corrected source"), save=False)
        submission.formatted_source_uploaded_at = timezone.now()
        submission.save()

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                source_file=SimpleUploadedFile(
                    "replacement.docx", b"source replacement", content_type="application/octet-stream"
                ),
            ),
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.processing_status, "processed")
        self.assertEqual(submission.page_count, original_page_count)
        self.assertEqual(submission.pdf_hash, original_pdf_hash)
        self.assertFalse(submission.formatted_source_file)
        self.assertIn("replacement", submission.source_current_file_path)
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertFalse(submission.extracted_title_verified)
        self.assertEqual(submission.format_status, "pending")
        self.assert_publication_blocked("Unverified Title/Author Extraction")

    def test_manual_edit_guards_review_fields_and_undo_not_publishing(self):
        self.make_master_paper("P001", title="Ready Paper")
        submission = self.make_final_submission(
            final_submission_id="22",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Ready Paper",
            extracted_title="",
            extracted_authors="",
            title_author_review_status="pending",
            title_author_verified=False,
            extracted_title_verified=False,
        )

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                title_author_review_status="review_ok",
                extracted_title_verified="on",
            ),
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertFalse(submission.title_author_verified)
        self.assertFalse(submission.extracted_title_verified)
        self.assert_publication_blocked("Missing Extracted Title")

        submission.extracted_title = "Ready Paper"
        submission.extracted_authors = "Ada Lovelace; Alan Turing"
        submission.title_author_review_status = "review_ok"
        submission.extracted_title_verified = True
        submission.excluded_from_publication = True
        submission.publication_exclusion_reason = "unpaid"
        submission.publication_excluded_at = timezone.now()
        submission.paper_id_verified = False
        submission.auto_verify_blocked = True
        submission.save()

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                excluded_from_publication="",
                publication_exclusion_reason="",
                publication_exclusion_notes="",
            ),
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.excluded_from_publication)
        self.assertFalse(submission.paper_id_verified)
        self.assertTrue(submission.auto_verify_blocked)
        self.assert_publication_blocked("Unverified Paper ID")

    def test_import_reset_allows_identical_title_to_auto_verify_again(self):
        self.make_master_paper("P001", title="Corrected Exact Title")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Old Title",
            extracted_title="Corrected Exact Title",
            paper_id_verified=False,
            verification_status="pending",
            auto_verify_blocked=True,
        )

        token_payload = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,Corrected Exact Title,Ada,2026-05-07 09:00:00\n",
            )
        )
        apply_import_preview(token_payload["token"])

        submission.refresh_from_db()
        rows = verification_rows(FinalSubmission.objects.filter(pk=submission.pk))
        self.assertFalse(submission.auto_verify_blocked)
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "verified")
        self.assertTrue(rows[0]["is_identical"])
        self.assertFalse(rows[0]["needs_verification"])
        self.assertFalse(
            any(row["category"] == "Unverified Paper ID" for row in publication_readiness_rows())
        )

    def test_manually_unverified_identical_title_stays_blocked(self):
        self.make_master_paper("P001", title="Exactly Matching Title")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Exactly Matching Title",
            extracted_title="Exactly Matching Title",
            paper_id_verified=True,
            verification_status="verified",
        )
        unverify_submission(submission)

        rows = verification_rows(FinalSubmission.objects.filter(pk=submission.pk))

        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "pending")
        self.assertTrue(rows[0]["needs_verification"])
        self.assertTrue(
            any(row["category"] == "Unverified Paper ID" for row in publication_readiness_rows())
        )

    def test_built_in_title_author_source_is_valid_in_edit_form(self):
        submission = self.make_final_submission(
            title_author_source="built_in_extractor",
            title_author_extraction_status="extracted",
        )

        form = FinalSubmissionForm(instance=submission)
        source_choices = dict(form.fields["title_author_source"].choices)

        self.assertEqual(source_choices["built_in_extractor"], "Built-in extractor")

    def test_organized_list_needs_attention_uses_editorial_priority(self):
        self.make_master_paper("P001", "Missing Final", "Ada")
        self.make_master_paper("P002", "Needs ID Review", "Ada")
        self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Needs ID Review",
            extracted_title="Needs ID Review",
            paper_id_verified=False,
            auto_verify_blocked=True,
        )
        self.make_master_paper("P003", "No PDF", "Ada")
        self.make_final_submission(
            final_submission_id="3",
            paper_id_filled="P003",
            final_submission_title="No PDF",
            extracted_title="No PDF",
            current_file_path="",
        )
        self.make_master_paper("P004", "Old Plagiarism Report", "Ada")
        self.make_final_submission(
            final_submission_id="4",
            paper_id_filled="P004",
            final_submission_title="Old Plagiarism Report",
            extracted_title="Old Plagiarism Report",
            plagiarism_report_path=str(self.make_pdf_file("reports/P004.pdf")),
            plagiarism_report_stale=True,
        )
        self.make_master_paper("P005", "Clean Paper", "Ada")
        self.make_final_submission(
            final_submission_id="5",
            paper_id_filled="P005",
            final_submission_title="Clean Paper",
            extracted_title="Clean Paper",
        )
        self.make_master_paper("P006", "Master Title", "Ada")
        self.make_final_submission(
            final_submission_id="6",
            paper_id_filled="P006",
            final_submission_title="Verified Different Title",
            extracted_title="Verified Different Title",
            paper_id_verified=True,
            verification_status="verified",
            verification_message="Paper ID manually verified; title differs.",
        )
        self.make_master_paper("P007", "Format Pending", "Ada")
        self.make_final_submission(
            final_submission_id="7",
            paper_id_filled="P007",
            final_submission_title="Format Pending",
            extracted_title="Format Pending",
            format_status="pending",
        )
        self.make_master_paper("P008", "Soft Title", "Ada")
        self.make_final_submission(
            final_submission_id="8",
            paper_id_filled="P008",
            final_submission_title="Soft Title",
            extracted_title="Soft: Title",
            extracted_title_verified=True,
        )
        self.make_master_paper("P009", "Unverified Title", "Ada")
        self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P009",
            final_submission_title="Unverified Title",
            extracted_title="Unverified Title",
            extracted_title_verified=False,
        )
        self.make_master_paper("P010", "Missing Extracted Title", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P010",
            final_submission_title="Missing Extracted Title",
            extracted_title="",
            extracted_title_verified=False,
        )

        rows, summary, _settings_obj, current_filter, current_sort = organized_list_rows()
        ordered_ids = [
            row["paper"].paper_id if row["paper"] else row["submission"].paper_id_filled
            for row in rows
        ]

        self.assertEqual(current_filter, "all")
        self.assertEqual(current_sort, "needs_attention")
        self.assertEqual(
            ordered_ids,
            ["P001", "P002", "P003", "P010", "P009", "P004", "P007", "P006", "P008", "P005"],
        )
        self.assertEqual(summary["missing_final"], 1)
        self.assertEqual(summary["unverified"], 2)
        self.assertEqual(summary["page_errors"], 1)
        self.assertEqual(summary["missing_plagiarism"], 0)

        needs_attention_rows, _summary, _settings_obj, current_filter, current_sort = organized_list_rows(
            current_filter="needs_attention"
        )
        needs_attention_ids = [
            row["paper"].paper_id if row["paper"] else row["submission"].paper_id_filled
            for row in needs_attention_rows
        ]
        self.assertEqual(current_filter, "needs_attention")
        self.assertEqual(current_sort, "needs_attention")
        self.assertEqual(
            needs_attention_ids,
            ["P001", "P002", "P003", "P010", "P009", "P004", "P007", "P006", "P008"],
        )
        self.assertNotIn("P005", needs_attention_ids)

        title_issue_rows, _summary, _settings_obj, current_filter, current_sort = organized_list_rows(
            current_filter="title_issues"
        )
        title_issue_ids = [
            row["paper"].paper_id if row["paper"] else row["submission"].paper_id_filled
            for row in title_issue_rows
        ]
        self.assertEqual(current_filter, "title_issues")
        self.assertEqual(title_issue_ids, ["P010", "P009", "P006", "P008"])
        self.assertLess(title_issue_ids.index("P009"), title_issue_ids.index("P006"))
        self.assertLess(title_issue_ids.index("P006"), title_issue_ids.index("P008"))

        response = self.client.get(reverse("submissions:organized_list"))
        self.assertContains(response, "Clean Paper")
        self.assertContains(response, "Verified Different Title")

    def test_excel_exports_handle_nat_datetime_values(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        self.make_master_paper("P002", "Second Paper", "Grace")
        first = self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            title_author_verified_at=timezone.now(),
        )
        self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Second Paper",
            extracted_title="Second Paper",
            title_author_verified_at=None,
        )
        first.publication_excluded_at = None
        first.save(update_fields=["publication_excluded_at"])

        active_path = export_active_versions()
        workbook_path = export_all_reports()

        self.assertTrue(Path(active_path).exists())
        self.assertTrue(Path(workbook_path).exists())

    def test_editor_visible_data_matches_publication_package_contents(self):
        self.make_master_paper("P001", "Main Ready Paper", "Ada Lovelace; Alan Turing")
        self.make_master_paper("P002", "Corrected Source Paper", "Grace Hopper")
        self.make_master_paper("P003", "Late Fixed Paper", "Katherine Johnson")
        old = self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P001",
            final_submission_title="Main Ready Paper",
            extracted_title="Main Ready Paper",
            current_file_path=str(self.make_pdf_file("p001-old.pdf", b"old p001 pdf")),
            source_current_file_path=str(self.make_source_file("p001-old.docx", b"old p001 source")),
        )
        active = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Main Ready Paper",
            extracted_title="Main Ready Paper",
            current_file_path=str(self.make_pdf_file("p001-active.pdf", b"active p001 pdf")),
            source_current_file_path=str(self.make_source_file("p001-active.docx", b"active p001 source")),
            page_count=8,
            similarity_score=4,
            single_similarity_score=1,
        )
        corrected_source = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P002",
            final_submission_title="Corrected Source Paper",
            extracted_title="Corrected Source Paper",
            final_submission_authors="Grace Hopper",
            extracted_authors="Grace Hopper",
            current_file_path=str(self.make_pdf_file("p002.pdf", b"p002 pdf")),
            source_current_file_path=str(self.make_source_file("p002-original.docx", b"p002 original source")),
            page_count=9,
            similarity_score=5,
            single_similarity_score=2,
        )
        update_formatting_submission(
            corrected_source,
            {
                "corrected_pdf": None,
                "corrected_source": self.uploaded_file("p002-corrected.docx", b"p002 corrected source"),
                "format_status": "review_ok",
                "format_notes": "source correction approved",
            },
        )
        corrected_source.refresh_from_db()
        verify_title_author(corrected_source)
        verify_extracted_title(corrected_source)
        corrected_source.format_status = "review_ok"
        corrected_source.save(update_fields=["format_status"])
        excluded = self.make_final_submission(
            final_submission_id="40",
            paper_id_filled="NOTMASTER",
            start2_paper_id_raw="NOTMASTER",
            final_submission_title="Unpaid Paper",
            extracted_title="Unpaid Paper",
            current_file_path=str(self.make_pdf_file("unpaid.pdf", b"unpaid pdf")),
            source_current_file_path=str(self.make_source_file("unpaid.docx", b"unpaid source")),
        )
        determine_active_versions()
        _mark_duplicate_submissions()
        old.refresh_from_db()
        active.refresh_from_db()
        self.assertFalse(old.active_version)
        self.assertTrue(active.active_version)

        organized = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        self.assertContains(organized, "Main Ready Paper")
        self.assertContains(organized, "Corrected Source Paper")
        self.assertContains(organized, "P003")
        self.assertContains(organized, "No final")
        self.assertContains(organized, "NOTMASTER")
        self.assertContains(organized, "Not in Paper Master")
        self.assertContains(organized, "Corrected")
        self.assertContains(organized, "P 4%")
        self.assertContains(organized, "S 1%")

        error_page = self.client.get(reverse("submissions:error_report"))
        self.assertContains(error_page, "Missing Final Submission")
        self.assertContains(error_page, "Unclassified Final Not In Master")
        self.assertContains(error_page, "Replaced Final Submission")

        blocked = self.client.post(
            reverse("submissions:export_reports"),
            {"action": "publication_package"},
            follow=True,
        )
        self.assertContains(blocked, "Publication package is not ready")
        self.assertContains(blocked, "Missing Final Submission")
        self.assertContains(blocked, "Unclassified Final Not In Master")
        self.assertContains(blocked, "Download Draft Package Anyway")

        draft = self.client.post(
            reverse("submissions:export_reports"),
            {"action": "publication_package_force"},
        )
        self.assertEqual(draft.status_code, 200)
        self.assertIn("publication_package_draft_", draft["Content-Disposition"])

        response = self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": excluded.pk,
                "action": "mark_not_publishing",
                "publication_exclusion_reason": "unpaid",
                "publication_exclusion_notes": "payment not received",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.make_final_submission(
            final_submission_id="30",
            paper_id_filled="P003",
            start2_paper_id_raw="P003",
            final_submission_title="Late Fixed Paper",
            extracted_title="Late Fixed Paper",
            final_submission_authors="Katherine Johnson",
            extracted_authors="Katherine Johnson",
            current_file_path=str(self.make_pdf_file("p003.pdf", b"p003 pdf")),
            source_current_file_path=str(self.make_source_file("p003.docx", b"p003 source")),
            page_count=7,
            similarity_score=6,
            single_similarity_score=3,
        )
        determine_active_versions()
        _mark_duplicate_submissions()
        self.assertEqual(publication_readiness_rows(), [])

        dashboard = self.client.get(reverse("submissions:dashboard"))
        self.assertContains(dashboard, "3")
        self.assertContains(dashboard, "1 not publishing")
        self.assertContains(dashboard, "0 missing")
        self.assertContains(dashboard, "0 over threshold")
        clean_organized = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        self.assertContains(clean_organized, "Main Ready Paper")
        self.assertContains(clean_organized, "Corrected Source Paper")
        self.assertContains(clean_organized, "Late Fixed Paper")
        self.assertNotContains(clean_organized, "Unpaid Paper")
        clean_errors = self.client.get(reverse("submissions:error_report"))
        self.assertContains(clean_errors, "Replaced Final Submission")
        self.assertNotContains(clean_errors, "Missing Final Submission")
        self.assertNotContains(clean_errors, "Unclassified Final Not In Master")

        package_response = self.client.post(
            reverse("submissions:export_reports"),
            {"action": "publication_package"},
        )
        self.assertEqual(package_response.status_code, 200)
        self.assertIn("application/zip", package_response["Content-Type"])
        package_bytes = b"".join(package_response.streaming_content)
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            manifest_name = next(name for name in names if name.startswith("publication_manifest_"))
            rows = list(csv.DictReader(io.StringIO(archive.read(manifest_name).decode("utf-8-sig"))))
            self.assertEqual([row["ID"] for row in rows], ["P001", "P002", "P003"])
            manifest_by_id = {row["ID"]: row for row in rows}
            self.assertEqual(manifest_by_id["P001"]["Extracted Title"], "Main Ready Paper")
            self.assertEqual(manifest_by_id["P001"]["Page Number"], "8")
            self.assertEqual(manifest_by_id["P001"]["Similarity (P)"], "4")
            self.assertEqual(manifest_by_id["P002"]["Extracted Title"], "Corrected Source Paper")
            self.assertEqual(manifest_by_id["P002"]["Author Number"], "1")
            self.assertEqual(manifest_by_id["P002"]["Similarity (S)"], "2")
            self.assertEqual(archive.read("PDF/P001-Main Ready Paper.pdf"), b"active p001 pdf")
            self.assertEqual(archive.read("Source/P001-Main Ready Paper.docx"), b"active p001 source")
            self.assertEqual(archive.read("PDF/P002-Corrected Source Paper.pdf"), b"p002 pdf")
            self.assertEqual(archive.read("Source/P002-Corrected Source Paper.docx"), b"p002 corrected source")
            self.assertEqual(archive.read("PDF/P003-Late Fixed Paper.pdf"), b"p003 pdf")
            self.assertEqual(archive.read("Source/P003-Late Fixed Paper.docx"), b"p003 source")
            self.assertFalse(any("Unpaid" in name or "NOTMASTER" in name for name in names))
            self.assertFalse(any(archive.read(name) == b"old p001 pdf" for name in names if name.startswith("PDF/")))

    def test_error_report_grouping_and_title_author_verification_toggles(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(title_author_verified=False, extracted_title_verified=False)

        rows = error_report_rows()
        categories = {row["category"] for row in rows}
        self.assertIn("Unverified Title/Author Extraction", categories)
        self.assertIn("Unverified Extracted Title Match", categories)
        self.assertTrue(all("group" in row and "level" in row for row in rows))

        verify_title_author(submission)
        verify_extracted_title(submission)
        submission.refresh_from_db()
        self.assertTrue(submission.title_author_verified)
        self.assertTrue(submission.extracted_title_verified)
        unverify_title_author(submission)
        unverify_extracted_title(submission)
        submission.refresh_from_db()
        self.assertFalse(submission.title_author_verified)
        self.assertFalse(submission.extracted_title_verified)

    def test_import_preview_service_is_covered_by_view_ready_data(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        response = self.client.post(
            reverse("submissions:import_final_submissions"),
            {
                "file": self.uploaded_csv(
                    "final.csv",
                    "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                    "88,P001,Ready Paper,Ada,2026-05-07 09:00:00\n",
                ),
            },
        )
        self.assertEqual(response.status_code, 200)


class PerformanceRegressionTests(EditorialAcceptanceTestCase):
    WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE")

    def setUp(self):
        super().setUp()
        for index in range(1, 4):
            paper_id = f"P{index:03d}"
            self.make_master_paper(
                paper_id=paper_id,
                title=f"Ready Paper {index}",
                authors="Ada Lovelace; Alan Turing",
            )
            self.make_final_submission(
                final_submission_id=str(100 + index),
                paper_id_filled=paper_id,
                final_submission_title=f"Ready Paper {index}",
                extracted_title=f"Ready Paper {index}",
                extracted_authors="Ada Lovelace; Alan Turing",
            )
        rebuild_paper_authors()

    def assert_get_query_budget(self, url_name, max_queries):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse(url_name))
        self.assertEqual(response.status_code, 200)
        writes = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith(self.WRITE_PREFIXES)
        ]
        self.assertEqual(writes, [], f"{url_name} GET must not write to the database.")
        self.assertLessEqual(
            len(captured),
            max_queries,
            f"{url_name} used {len(captured)} queries; budget is {max_queries}.",
        )

    def test_main_review_pages_are_read_only_and_query_bounded(self):
        budgets = {
            "submissions:dashboard": 50,
            "submissions:verify_paper_ids": 80,
            "submissions:title_author_extraction": 100,
            "submissions:exceptions_center": 80,
            "submissions:error_report": 100,
        }
        for url_name, max_queries in budgets.items():
            with self.subTest(url_name=url_name):
                self.assert_get_query_budget(url_name, max_queries)

    def test_title_author_get_does_not_auto_persist_title_match(self):
        submission = FinalSubmission.objects.first()
        submission.extracted_title_verified = False
        submission.extracted_title_match_status = "pending"
        submission.save(
            update_fields=[
                "extracted_title_verified",
                "extracted_title_match_status",
                "updated_at",
            ]
        )

        response = self.client.get(reverse("submissions:title_author_extraction"))
        self.assertEqual(response.status_code, 200)

        submission.refresh_from_db()
        self.assertFalse(submission.extracted_title_verified)
        self.assertEqual(submission.extracted_title_match_status, "pending")
