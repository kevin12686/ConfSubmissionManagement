import csv
import io
import json
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import quote
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

from submissions.forms import CrossCheckExportForm, FinalSubmissionForm, default_crosscheck_token
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
from submissions.services.import_preview import (
    apply_import_preview,
    preview_final_import,
    preview_initial_import,
)
from submissions.services.editor_uploads import (
    create_editor_submission,
    discard_submission,
    editor_conflict_count,
    undo_discard_submission,
)
from submissions.services.organized_list import organized_list_rows
from submissions.services.pdf_processor import calculate_pdf_hash, determine_active_versions, process_all_pdfs
from submissions.services.reports import (
    author_count_frame,
    export_active_versions,
    export_all_reports,
    export_old_versions,
    export_publication_package,
)
from submissions.services.crosscheck import (
    CROSSCHECK_EXPORT_MISSING_RESULTS,
    import_crosscheck_results,
    prepare_crosscheck_upload,
    upload_crosscheck_reports,
)
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
    sync_publication_pdf_debug_folder,
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
from submissions.services.audit import audit_log_path, read_audit_log, write_audit_event


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
        self.audit_root = self.root / "logs"
        self.system_state_reports_root.mkdir()
        self.system_state_restore_root.mkdir()
        self.storage_cleanup_root.mkdir()
        self.audit_root.mkdir()
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
        self.audit_root_patcher = patch(
            "submissions.services.audit.audit_log_root",
            lambda: self.audit_root,
        )
        self.system_state_audit_root_patcher = patch(
            "submissions.services.system_state.audit_log_root",
            lambda: self.audit_root,
        )
        self.system_state_reports_patcher.start()
        self.system_state_restore_patcher.start()
        self.storage_cleanup_patcher.start()
        self.audit_root_patcher.start()
        self.system_state_audit_root_patcher.start()
        self.addCleanup(self.system_state_reports_patcher.stop)
        self.addCleanup(self.system_state_restore_patcher.stop)
        self.addCleanup(self.storage_cleanup_patcher.stop)
        self.addCleanup(self.audit_root_patcher.stop)
        self.addCleanup(self.system_state_audit_root_patcher.stop)

        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(self.root / "reports")
        settings_obj.active_final_folder = str(self.root / "active")
        settings_obj.old_versions_folder = str(self.root / "old")
        settings_obj.publication_pdf_debug_folder = str(self.root / "publication_debug")
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

    def make_master_paper(self, paper_id="P001", title="Ready Paper", authors="Ada Lovelace; Alan Turing", notes=""):
        return InitialPaper.objects.create(
            paper_id=paper_id,
            acceptance_status="accepted",
            title=title,
            authors=authors,
            notes=notes,
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
        default_pdf_path = self.make_pdf_file(f"{final_id}.pdf")
        default_source_path = self.make_source_file(f"{final_id}.docx")
        thumbnail_folder = self.root / "thumbnails" / str(final_id)
        thumbnail_folder.mkdir(parents=True, exist_ok=True)
        (thumbnail_folder / "page-1.png").write_bytes(b"thumbnail")
        values = {
            "final_submission_id": final_id,
            "start2_paper_id_raw": paper_id,
            "paper_id_filled": paper_id,
            "final_submission_title": title,
            "final_submission_authors": "Ada Lovelace; Alan Turing",
            "upload_date": timezone.now(),
            "current_file_path": str(default_pdf_path),
            "source_current_file_path": str(default_source_path),
            "extracted_title": title,
            "extracted_authors": "Ada Lovelace; Alan Turing",
            "page_count": 8,
            "processing_status": "processed",
            "processing_message": "Ready.",
            "pdf_hash": calculate_pdf_hash(default_pdf_path),
            "thumbnail_folder": str(thumbnail_folder),
            "thumbnail_status": "processed",
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
        if "pdf_file" not in overrides:
            current_path_value = values.get("current_file_path") or ""
            current_path = Path(current_path_value) if current_path_value else None
            if current_path and current_path.exists():
                media_pdf = self.media_root / "final_submissions" / f"{final_id}_{current_path.name}"
                media_pdf.parent.mkdir(parents=True, exist_ok=True)
                media_pdf.write_bytes(current_path.read_bytes())
                values["pdf_file"] = f"final_submissions/{media_pdf.name}"
                values.setdefault("original_file_name", current_path.name)
                if "pdf_hash" not in overrides:
                    values["pdf_hash"] = calculate_pdf_hash(media_pdf)
        if "source_file" not in overrides:
            current_source_path_value = values.get("source_current_file_path") or ""
            current_source_path = Path(current_source_path_value) if current_source_path_value else None
            if current_source_path and current_source_path.exists():
                media_source = self.media_root / "source_submissions" / f"{final_id}_{current_source_path.name}"
                media_source.parent.mkdir(parents=True, exist_ok=True)
                media_source.write_bytes(current_source_path.read_bytes())
                values["source_file"] = f"source_submissions/{media_source.name}"
                values.setdefault("source_original_file_name", current_source_path.name)
        if "title_author_review_status" not in overrides:
            values["title_author_review_status"] = (
                "review_ok" if values.get("title_author_verified") else "pending"
            )
        return FinalSubmission.objects.create(**values)

    def mark_submission_publication_ready(self, submission, title=None, authors="Ada Lovelace; Alan Turing"):
        title = title or submission.final_submission_title or "Ready Paper"
        pdf_info = publication_pdf_info(submission)
        thumbnail_folder = self.root / "thumbnails" / str(submission.final_submission_id)
        thumbnail_folder.mkdir(parents=True, exist_ok=True)
        (thumbnail_folder / "page-1.png").write_bytes(b"thumbnail")
        submission.extracted_title = title
        submission.extracted_authors = authors
        submission.title_author_source = "manual"
        submission.title_author_review_status = "review_ok"
        submission.title_author_verified = True
        submission.extracted_title_verified = True
        submission.duplicate_author_review_status = "review_ok"
        submission.page_count = 8
        submission.processing_status = "processed"
        submission.processing_message = "Ready."
        submission.pdf_hash = calculate_pdf_hash(pdf_info["path"]) if pdf_info["exists"] else "ready-hash"
        submission.thumbnail_folder = str(thumbnail_folder)
        submission.thumbnail_status = "processed"
        submission.paper_id_verified = True
        submission.verification_status = "verified"
        submission.format_status = "review_ok"
        submission.similarity_score = 1
        submission.single_similarity_score = 1
        submission.plagiarism_report_stale = False
        submission.save()
        return submission

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

    def latest_audit_event(self, action):
        events = read_audit_log(query=action, limit=50)
        self.assertTrue(events, f"Expected audit event for {action}.")
        return events[0]

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
    def test_audit_log_jsonl_view_and_download_are_available(self):
        write_audit_event(
            action="unit_test_event",
            status="success",
            message="Audit trail smoke test.",
            paper_id="P001",
            final_submission_id="F001",
            file_changes={"temp_path": "/private/var/folders/example/upload.pdf"},
        )

        line = audit_log_path().read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(line)
        self.assertEqual(event["action"], "unit_test_event")
        self.assertEqual(event["paper_id"], "P001")
        self.assertNotIn("/private/var", json.dumps(event))
        self.assertEqual(read_audit_log(query="P001", limit=10)[0]["action"], "unit_test_event")

        response = self.client.get(reverse("submissions:audit_log"), {"q": "P001"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unit_test_event")
        response = self.client.get(reverse("submissions:download_audit_log"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"unit_test_event", b"".join(response.streaming_content))

    def test_audit_log_sanitizes_paths_and_survives_unparseable_lines(self):
        project_file = self.root / "reports" / "publication.zip"
        media_file = self.media_root / "uploads" / "paper.pdf"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_bytes(b"zip")
        media_file.write_bytes(b"pdf")

        with override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.media_root):
            write_audit_event(
                action="portable_path_event",
                status="success",
                file_changes={
                    "project_file": project_file,
                    "media_file": media_file,
                    "scratch_file": "/private/var/folders/session/upload.pdf",
                },
            )
        with audit_log_path().open("a", encoding="utf-8") as handle:
            handle.write("{broken json\n")

        events = read_audit_log(limit=10)
        self.assertEqual(events[0]["action"], "unparseable_log_line")
        event = next(row for row in events if row["action"] == "portable_path_event")
        event_text = json.dumps(event, ensure_ascii=False)
        self.assertIn("project:reports/publication.zip", event_text)
        self.assertIn("media/uploads/paper.pdf", event_text)
        self.assertNotIn(str(self.root), event_text)
        self.assertNotIn("/private/var", event_text)

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

    def test_clear_database_preserves_audit_log_by_default(self):
        write_audit_event(
            action="before_clear_marker",
            status="success",
            message="Should survive default clear.",
        )

        with override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.media_root):
            response = self.client.post(
                reverse("submissions:clear_database"),
                {"confirmation": "CLEAR DATABASE"},
            )

        self.assertEqual(response.status_code, 302)
        text = audit_log_path().read_text(encoding="utf-8")
        self.assertIn("before_clear_marker", text)
        self.assertIn("clear_database_requested", text)
        self.assertIn("clear_database_applied", text)
        self.assertFalse((self.audit_root / "archive").exists())

    def test_clear_database_can_archive_and_clear_audit_log(self):
        write_audit_event(
            action="before_archive_marker",
            status="success",
            message="Should move into archive.",
        )

        with override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.media_root):
            response = self.client.post(
                reverse("submissions:clear_database"),
                {"confirmation": "CLEAR DATABASE", "clear_audit_log": "on"},
            )

        self.assertEqual(response.status_code, 302)
        archived = list((self.audit_root / "archive").glob("audit_before_clear_database_*.log"))
        self.assertEqual(len(archived), 1)
        self.assertIn("before_archive_marker", archived[0].read_text(encoding="utf-8"))
        new_log = audit_log_path().read_text(encoding="utf-8")
        self.assertNotIn("before_archive_marker", new_log)
        self.assertIn("audit_log_archived_and_cleared", new_log)
        self.assertIn("clear_database_applied", new_log)

    def test_process_pdfs_writes_audit_event(self):
        self.make_master_paper(paper_id="LOG1")
        self.make_final_submission(final_submission_id="7001", paper_id_filled="LOG1")

        process_all_pdfs()

        text = audit_log_path().read_text(encoding="utf-8")
        self.assertIn("process_pdfs", text)

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
                and row["role"] == "Legacy processed PDF path"
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
        response = self.client.get(reverse("submissions:settings"))
        self.assertContains(response, "Preview conservative cleanup")
        self.assertContains(response, "Preview generated reports/exports cleanup")
        self.assertContains(response, "referenced thumbnails, and previews are kept")
        submission = self.make_final_submission(final_submission_id="8102", paper_id_filled="S002")
        submission.pdf_file.save("protected-original.pdf", ContentFile(b"original"), save=True)
        submission.formatted_pdf_file.save("protected-corrected.pdf", ContentFile(b"corrected"), save=True)
        referenced_thumbnail = self.media_root / "pdf_thumbnails" / "8102" / "page-1.png"
        referenced_thumbnail.parent.mkdir(parents=True, exist_ok=True)
        referenced_thumbnail.write_bytes(b"thumb")
        submission.thumbnail_folder = str(referenced_thumbnail.parent)
        submission.save(update_fields=["thumbnail_folder", "updated_at"])
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
        self.assertNotIn(orphan_output.resolve(), selected_paths)
        self.assertNotIn(original_path.resolve(), selected_paths)
        self.assertNotIn(corrected_path.resolve(), selected_paths)
        self.assertNotIn(referenced_thumbnail.resolve(), selected_paths)
        with self.assertRaises(ValueError):
            apply_storage_cleanup(preview["token"], "wrong")
        self.assertTrue(cache_file.exists())

        result = apply_storage_cleanup(preview["token"], CLEANUP_CONFIRMATION_TEXT)

        self.assertGreaterEqual(result["deleted_count"], 1)
        self.assertFalse(cache_file.exists())
        self.assertTrue(orphan_output.exists())
        self.assertTrue(original_path.exists())
        self.assertTrue(corrected_path.exists())
        self.assertTrue(referenced_thumbnail.exists())

    def test_storage_cleanup_reports_exports_policy_preserves_reference_files(self):
        settings_obj = AppSetting.load()
        reports_root = Path(settings_obj.reports_folder)
        reports_root.mkdir(parents=True, exist_ok=True)
        report_excel = reports_root / "active_publishable_versions.xlsx"
        report_zip = reports_root / "publication_package_draft.zip"
        report_text = reports_root / "notes.txt"
        for path in [report_excel, report_zip, report_text]:
            path.write_bytes(b"report")
        crosscheck_zip = self.root / "data" / "crosscheck_upload" / "TOKEN" / "crosscheck_upload_TOKEN.zip"
        crosscheck_zip.parent.mkdir(parents=True, exist_ok=True)
        crosscheck_zip.write_bytes(b"crosscheck")
        plagiarism_report = Path(settings_obj.plagiarism_reports_folder) / "P001_TOKEN.pdf"
        plagiarism_report.parent.mkdir(parents=True, exist_ok=True)
        plagiarism_report.write_bytes(b"plagiarism report")
        system_backup = self.root / "data" / "system_state_backups" / "system_state.zip"
        system_backup.parent.mkdir(parents=True, exist_ok=True)
        system_backup.write_bytes(b"backup")
        submission = self.make_final_submission(final_submission_id="8103", paper_id_filled="S003")
        referenced_thumbnail = self.media_root / "pdf_thumbnails" / "8103" / "page-1.png"
        referenced_thumbnail.parent.mkdir(parents=True, exist_ok=True)
        referenced_thumbnail.write_bytes(b"thumb")
        submission.thumbnail_folder = str(referenced_thumbnail.parent)
        submission.save(update_fields=["thumbnail_folder", "updated_at"])

        with override_settings(BASE_DIR=self.root):
            preview = preview_storage_cleanup("generated_reports_exports")

        selected_paths = {Path(row["path"]).resolve() for row in preview["files"]}
        self.assertIn(report_excel.resolve(), selected_paths)
        self.assertIn(report_zip.resolve(), selected_paths)
        self.assertIn(crosscheck_zip.resolve(), selected_paths)
        self.assertNotIn(report_text.resolve(), selected_paths)
        self.assertNotIn(plagiarism_report.resolve(), selected_paths)
        self.assertNotIn(system_backup.resolve(), selected_paths)
        self.assertNotIn(referenced_thumbnail.resolve(), selected_paths)

        with override_settings(BASE_DIR=self.root):
            result = apply_storage_cleanup(preview["token"], CLEANUP_CONFIRMATION_TEXT)

        self.assertGreaterEqual(result["deleted_count"], 3)
        self.assertFalse(report_excel.exists())
        self.assertFalse(report_zip.exists())
        self.assertFalse(crosscheck_zip.exists())
        self.assertTrue(report_text.exists())
        self.assertTrue(plagiarism_report.exists())
        self.assertTrue(system_backup.exists())
        self.assertTrue(referenced_thumbnail.exists())

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

    def test_publication_debug_sync_uses_same_pdf_bytes_as_publication_package(self):
        self.make_master_paper("P001", "Debug Sync Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="8301",
            paper_id_filled="P001",
            final_submission_title="Debug Sync Paper",
            extracted_title="Debug Sync Paper",
            current_file_path=str(self.make_pdf_file("debug-original.pdf", b"original pdf")),
            source_current_file_path=str(self.make_source_file("debug-source.docx", b"source")),
        )
        submission.formatted_pdf_file.save("debug-corrected.pdf", ContentFile(b"corrected pdf"), save=True)
        submission.pdf_hash = calculate_pdf_hash(submission.formatted_pdf_file.path)
        submission.save(update_fields=["pdf_hash", "updated_at"])

        debug_result = sync_publication_pdf_debug_folder()
        zip_path = export_publication_package()

        self.assertEqual(debug_result["synced_count"], 1)
        debug_file = Path(debug_result["folder"]) / "P001-Debug Sync Paper.pdf"
        self.assertTrue(debug_file.exists())
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.read("PDF/P001-Debug Sync Paper.pdf"), debug_file.read_bytes())
        self.assertEqual(debug_file.read_bytes(), b"corrected pdf")


class SystemStateTests(EditorialAcceptanceTestCase):
    def test_system_state_restore_round_trips_settings_records_and_files(self):
        settings_obj = AppSetting.load()
        settings_obj.conference_name = "DSA 2026"
        settings_obj.page_limit = 10
        settings_obj.save()
        self.make_master_paper(
            paper_id="R001",
            title="Restored Paper",
            notes="  Restore note  \n\n\n  keep this  ",
        )
        pdf_path = self.media_root / "active" / "R001.pdf"
        source_path = self.media_root / "source" / "R001.docx"
        report_path = self.media_root / "reports" / "R001_report.pdf"
        verification_image = (
            self.media_root
            / "title_author_verification"
            / "9001"
            / "R001.pdf.png"
        )
        thumbnail_path = self.media_root / "pdf_thumbnails" / "9001" / "page-1.png"
        format_preview = self.media_root / "format_previews" / "9001-preview.png"
        temp_formatting_preview = (
            self.media_root
            / "formatting_upload_previews"
            / "temp-token"
            / "corrected_pdf.pdf"
        )
        for path, payload in [
            (pdf_path, b"publication pdf"),
            (source_path, b"publication source"),
            (report_path, b"plagiarism report"),
            (verification_image, b"title author review image"),
            (thumbnail_path, b"thumbnail image"),
            (format_preview, b"format preview"),
            (temp_formatting_preview, b"temporary formatting upload"),
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
            title_author_verification_image=(
                f"{django_settings.MEDIA_URL}title_author_verification/9001/R001.pdf.png"
            ),
            thumbnail_folder="pdf_thumbnails/9001",
            title_author_review_status="review_ok",
            title_author_verified=True,
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
        with zipfile.ZipFile(snapshot["path"]) as archive:
            names = set(archive.namelist())
            self.assertIn(
                "files/media/title_author_verification/9001/R001.pdf.png",
                names,
            )
            self.assertIn("files/media/pdf_thumbnails/9001/page-1.png", names)
            self.assertIn("files/media/format_previews/9001-preview.png", names)
            self.assertNotIn(
                "files/media/formatting_upload_previews/temp-token/corrected_pdf.pdf",
                names,
            )
        FinalSubmission.objects.create(final_submission_id="temp", paper_id_filled="TEMP")
        pdf_path.unlink()
        verification_image.unlink()
        thumbnail_path.unlink()
        format_preview.unlink()
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
        restored_paper = InitialPaper.objects.get()
        self.assertEqual(restored_paper.paper_id, "R001")
        self.assertEqual(restored_paper.notes, "Restore note\n\nkeep this")
        restored = FinalSubmission.objects.get()
        self.assertEqual(restored.final_submission_id, "9001")
        self.assertTrue(Path(restored.current_file_path).exists())
        self.assertEqual(Path(restored.current_file_path).read_bytes(), b"publication pdf")
        self.assertTrue(Path(restored.source_current_file_path).exists())
        self.assertTrue(Path(restored.plagiarism_report_path).exists())
        self.assertTrue(Path(restored.title_author_verification_image).exists())
        self.assertEqual(
            Path(restored.title_author_verification_image).read_bytes(),
            b"title author review image",
        )
        self.assertTrue((Path(restored.thumbnail_folder) / "page-1.png").exists())
        self.assertEqual(
            (Path(restored.thumbnail_folder) / "page-1.png").read_bytes(),
            b"thumbnail image",
        )
        self.assertTrue((self.media_root / "format_previews" / "9001-preview.png").exists())
        self.assertEqual(restored.title_author_review_status, "review_ok")
        self.assertTrue(restored.title_author_verified)
        self.assertEqual(PaperAuthor.objects.count(), 1)
        self.assertEqual(AuthorLimitWaiver.objects.count(), 1)
        self.assertTrue(Path(result["pre_restore_backup"]).exists())

    def test_system_state_manifest_contains_app_and_archive_versions(self):
        write_audit_event(
            action="snapshot_marker",
            status="success",
            message="Audit trail should be included in system state.",
        )
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            state = json.loads(archive.read("state.json").decode("utf-8"))
            archive_text = json.dumps({"manifest": manifest, "state": state})
            self.assertIn("files/project/data/logs/audit.log", archive.namelist())
            self.assertIn(b"snapshot_marker", archive.read("files/project/data/logs/audit.log"))
        self.assertEqual(manifest["app_name"], "Conference Final Manager")
        self.assertEqual(manifest["app_version"], django_settings.APP_VERSION)
        self.assertGreaterEqual(manifest["artifact_counts"]["audit_logs"], 1)
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
                "paper_id,acceptance_status,title,authors,notes\n"
                "R001,accepted,Original Title,Ada,Original note\n"
                ",accepted,Blank Row,Ignored,Ignored note\n",
            )
        )
        self.assertEqual(result, {"created": 1, "updated": 0})

        result = import_initial_papers(
            self.uploaded_csv(
                "initial.csv",
                "paper_id,acceptance_status,title,authors,notes\n"
                'R001,accepted,Updated Title,Ada; Alan," Updated note  \n\n\n  second line "\n',
            )
        )
        paper = InitialPaper.objects.get(paper_id="R001")
        self.assertEqual(result, {"created": 0, "updated": 1})
        self.assertEqual(paper.title, "Updated Title")
        self.assertEqual(paper.notes, "Updated note\n\nsecond line")

    def test_paper_master_notes_preview_apply_does_not_reset_verification(self):
        self.make_master_paper("P001", "Stable Title", "Ada", notes="Old note")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Stable Title",
            extracted_title="Stable Title",
            paper_id_verified=True,
            verification_status="verified",
        )

        preview = preview_initial_import(
            self.uploaded_csv(
                "master.csv",
                'paper_id,acceptance_status,title,authors,notes\n'
                'P001,accepted,Stable Title,Ada,"  New note  \n\n\n  keep this  "\n',
            )
        )
        row = preview["rows"][0]
        self.assertEqual(row["status"], "changed")
        self.assertEqual([change["field"] for change in row["changes"]], ["notes"])
        self.assertFalse(row["paper_id_review_reset"])

        apply_import_preview(preview["token"])
        paper = InitialPaper.objects.get(paper_id="P001")
        submission.refresh_from_db()
        self.assertEqual(paper.notes, "Old note")
        self.assertTrue(submission.paper_id_verified)

        preview = preview_initial_import(
            self.uploaded_csv(
                "master.csv",
                'paper_id,acceptance_status,title,authors,notes\n'
                'P001,accepted,Stable Title,Ada,"  New note  \n\n\n  keep this  "\n',
            )
        )
        apply_import_preview(preview["token"], notes_policy="apply_imported_notes")
        paper.refresh_from_db()
        submission.refresh_from_db()
        self.assertEqual(paper.notes, "New note\n\nkeep this")
        self.assertTrue(submission.paper_id_verified)

    def test_paper_master_import_view_preserves_or_applies_notes_by_choice(self):
        self.make_master_paper("P001", "Stable Title", "Ada", notes="System note")

        response = self.client.post(
            reverse("submissions:import_initial_papers"),
            {
                "file": self.uploaded_csv(
                    "master.csv",
                    "paper_id,acceptance_status,title,authors,notes\n"
                    "P001,accepted,Stable Title,Ada,Imported note\n"
                    "P002,accepted,New Title,Grace,New row note\n",
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Notes import choice")
        self.assertContains(response, "Preserve existing system notes")
        self.assertContains(response, "Apply imported notes")
        self.assertContains(response, "Default: will preserve existing note")
        token = response.context["preview"]["token"]

        response = self.client.post(
            reverse("submissions:import_initial_papers"),
            {
                "action": "apply_preview",
                "preview_token": token,
                "notes_policy": "preserve_existing_notes",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InitialPaper.objects.get(paper_id="P001").notes, "System note")
        self.assertEqual(InitialPaper.objects.get(paper_id="P002").notes, "New row note")

        response = self.client.post(
            reverse("submissions:import_initial_papers"),
            {
                "file": self.uploaded_csv(
                    "master.csv",
                    "paper_id,acceptance_status,title,authors,notes\n"
                    "P001,accepted,Stable Title,Ada,Imported note\n",
                ),
            },
        )
        token = response.context["preview"]["token"]
        response = self.client.post(
            reverse("submissions:import_initial_papers"),
            {
                "action": "apply_preview",
                "preview_token": token,
                "notes_policy": "apply_imported_notes",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InitialPaper.objects.get(paper_id="P001").notes, "Imported note")

    def test_paper_master_import_preview_sorts_attention_rows_first(self):
        self.make_master_paper("P001", "Old Title", "Ada")
        self.make_master_paper("P002", "Same Title", "Ada")
        self.make_master_paper("P003", "Notes Title", "Ada", notes="System note")

        preview = preview_initial_import(
            self.uploaded_csv(
                "master.csv",
                "paper_id,acceptance_status,title,authors,notes\n"
                "P002,accepted,Same Title,Ada,\n"
                "P004,accepted,New Title,Grace,New note\n"
                "P003,accepted,Notes Title,Ada,Imported note\n"
                "P001,accepted,New Title,Ada,\n",
            )
        )
        ordered_ids = [row["identifier"] for row in preview["rows"]]
        self.assertEqual(ordered_ids, ["P001", "P004", "P003", "P002"])

        response = self.client.post(
            reverse("submissions:import_initial_papers"),
            {
                "file": self.uploaded_csv(
                    "master.csv",
                    "paper_id,acceptance_status,title,authors,notes\n"
                    "P002,accepted,Same Title,Ada,\n"
                    "P004,accepted,New Title,Grace,New note\n"
                    "P003,accepted,Notes Title,Ada,Imported note\n"
                    "P001,accepted,New Title,Ada,\n",
                ),
            },
        )
        self.assertContains(response, "Rows are sorted by attention needed; unchanged rows are shown last.")

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

    def test_final_import_preview_sorts_attention_rows_first(self):
        self.make_master_paper("P001", "Reset Paper", "Ada")
        self.make_master_paper("P002", "File Paper", "Ada")
        self.make_master_paper("P003", "New Paper", "Ada")
        self.make_master_paper("P004", "Metadata Paper", "Ada")
        self.make_master_paper("P005", "Same Paper", "Ada")
        self.make_master_paper("P006", "Editor Paper", "Ada")

        reset_submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Reset Paper",
            extracted_title="Reset Paper",
        )
        file_submission = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P002",
            final_submission_title="File Paper",
            extracted_title="File Paper",
        )
        metadata_submission = self.make_final_submission(
            final_submission_id="40",
            paper_id_filled="P004",
            final_submission_title="Metadata Paper",
            final_submission_authors="Old Author",
            extracted_title="Metadata Paper",
        )
        self.make_final_submission(
            final_submission_id="50",
            paper_id_filled="P005",
            final_submission_title="Same Paper",
            extracted_title="Same Paper",
        )
        self.make_final_submission(
            final_submission_id="60",
            paper_id_filled="P006",
            final_submission_title="Editor Paper",
            extracted_title="Editor Paper",
            submission_origin="editor_upload",
        )
        reset_submission.paper_id_verified = True
        reset_submission.verification_status = "verified"
        reset_submission.save(update_fields=["paper_id_verified", "verification_status", "updated_at"])
        metadata_submission.paper_id_verified = True
        metadata_submission.save(update_fields=["paper_id_verified", "updated_at"])

        preview = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "50,P005,Same Paper,Ada,2026-05-01 09:00:00\n"
                "30,P003,New Paper,Ada,2026-05-03 09:00:00\n"
                "40,P004,Metadata Paper,New Author,2026-05-01 09:00:00\n"
                "10,P001,Changed Reset Title,Ada,2026-05-01 09:00:00\n"
                "70,UNKNOWN,Unknown Paper,Ada,2026-05-07 09:00:00\n"
                "20,P002,File Paper,Ada,2026-05-01 09:00:00\n"
                "60,P006,Editor Paper,Ada,2026-05-01 09:00:00\n",
            ),
            submission_files=[
                self.uploaded_file("20_file_Submit_PDF.pdf", b"%PDF changed"),
            ],
        )
        ordered_ids = [row["identifier"] for row in preview["rows"]]
        self.assertEqual(ordered_ids, ["60", "70", "10", "20", "30", "40", "50"])

        apply_import_preview(preview["token"])
        metadata_submission.refresh_from_db()
        file_submission.refresh_from_db()
        reset_submission.refresh_from_db()
        self.assertEqual(metadata_submission.final_submission_authors, "New Author")
        self.assertEqual(file_submission.processing_status, "pending")
        self.assertFalse(reset_submission.paper_id_verified)


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
        pdf_info = publication_pdf_info(submission)
        self.assertEqual(pdf_info["source"], "original")
        self.assertEqual(Path(pdf_info["path"]).read_bytes(), b"original pdf")

        submission.formatted_source_file.delete(save=True)
        submission.refresh_from_db()
        source_info = publication_source_info(submission)
        self.assertEqual(source_info["source"], "original")
        self.assertEqual(Path(source_info["path"]).read_bytes(), b"original source")

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
        newest_pdf_info = publication_pdf_info(newest)
        self.assertEqual(newest_pdf_info["source"], "original")
        self.assertEqual(Path(newest_pdf_info["path"]).read_bytes(), b"round3 pdf")
        self.assertNotEqual(newest_pdf_info["path"], newest.current_file_path)

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

    def test_crosscheck_missing_results_export_uses_current_publication_pdfs(self):
        self.make_master_paper("P001", "Needs CrossCheck Corrected", "Ada")
        corrected = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Needs CrossCheck Corrected",
            extracted_title="Needs CrossCheck Corrected",
            current_file_path=str(self.make_pdf_file("p001-active.pdf", b"active-final p001")),
            similarity_score=None,
            single_similarity_score=2,
        )
        corrected.formatted_pdf_file.save(
            "corrected-p001.pdf",
            ContentFile(b"corrected publication p001"),
            save=True,
        )
        self.make_master_paper("P002", "Needs CrossCheck Active Final", "Ada")
        self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P002",
            final_submission_title="Needs CrossCheck Active Final",
            extracted_title="Needs CrossCheck Active Final",
            current_file_path=str(self.make_pdf_file("p002-active.pdf", b"active-final p002")),
            similarity_score=3,
            single_similarity_score=None,
        )
        self.make_master_paper("P003", "Already Checked", "Ada")
        self.make_final_submission(
            final_submission_id="30",
            paper_id_filled="P003",
            final_submission_title="Already Checked",
            extracted_title="Already Checked",
            current_file_path=str(self.make_pdf_file("p003-active.pdf", b"active-final p003")),
            similarity_score=4,
            single_similarity_score=1,
        )
        self.make_master_paper("P004", "Discarded Missing", "Ada")
        self.make_final_submission(
            final_submission_id="40",
            paper_id_filled="P004",
            final_submission_title="Discarded Missing",
            extracted_title="Discarded Missing",
            discarded=True,
            similarity_score=None,
            single_similarity_score=None,
        )
        self.make_master_paper("P005", "Excluded Missing", "Ada")
        self.make_final_submission(
            final_submission_id="50",
            paper_id_filled="P005",
            final_submission_title="Excluded Missing",
            extracted_title="Excluded Missing",
            excluded_from_publication=True,
            similarity_score=None,
            single_similarity_score=None,
        )

        all_result = prepare_crosscheck_upload("TOKEN")
        missing_result = prepare_crosscheck_upload("TOKEN", scope=CROSSCHECK_EXPORT_MISSING_RESULTS)

        self.assertTrue(Path(all_result["zip_path"]).exists())
        self.assertTrue(Path(missing_result["zip_path"]).exists())
        self.assertNotEqual(all_result["zip_path"], missing_result["zip_path"])
        with zipfile.ZipFile(all_result["zip_path"]) as archive:
            self.assertIn("P003_TOKEN.pdf", archive.namelist())
        with zipfile.ZipFile(missing_result["zip_path"]) as archive:
            names = set(archive.namelist())
            self.assertIn("P001_TOKEN.pdf", names)
            self.assertIn("P002_TOKEN.pdf", names)
            self.assertNotIn("P003_TOKEN.pdf", names)
            self.assertNotIn("P004_TOKEN.pdf", names)
            self.assertNotIn("P005_TOKEN.pdf", names)
            self.assertEqual(archive.read("P001_TOKEN.pdf"), b"corrected publication p001")
            self.assertEqual(archive.read("P002_TOKEN.pdf"), b"active-final p002")
            manifest_name = f"crosscheck_missing_manifest_TOKEN.csv"
            rows = list(
                csv.DictReader(io.StringIO(archive.read(manifest_name).decode("utf-8-sig")))
            )
        by_id = {row["paper_id"]: row for row in rows}
        self.assertEqual(by_id["P001"]["export_scope"], CROSSCHECK_EXPORT_MISSING_RESULTS)
        self.assertEqual(by_id["P001"]["missing_plagiarism_percent"], "True")
        self.assertEqual(by_id["P001"]["missing_single_percent"], "False")
        self.assertEqual(by_id["P002"]["missing_plagiarism_percent"], "False")
        self.assertEqual(by_id["P002"]["missing_single_percent"], "True")

    def test_crosscheck_export_form_uses_dynamic_default_token(self):
        self.assertEqual(default_crosscheck_token(date(2026, 5, 10)), "MAY102026_1")
        self.assertEqual(default_crosscheck_token(date(2026, 12, 3)), "DEC032026_1")
        self.assertEqual(CrossCheckExportForm().fields["token"].initial(date(2026, 5, 10)), "MAY102026_1")

    def test_crosscheck_integration_page_has_all_and_missing_export_actions(self):
        self.make_master_paper("P001", "Needs Missing Export", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Needs Missing Export",
            extracted_title="Needs Missing Export",
            current_file_path=str(self.make_pdf_file("p001-crosscheck.pdf", b"p001 pdf")),
            similarity_score=None,
            single_similarity_score=None,
        )

        page = self.client.get(reverse("submissions:integration"))
        self.assertContains(page, "Prepare All Publication PDFs ZIP")
        self.assertContains(page, "Prepare Missing Results Only ZIP")

        response = self.client.post(
            reverse("submissions:integration"),
            {"action": "prepare_crosscheck_missing", "token": "TOKEN"},
        )
        self.assertContains(response, "Missing CrossCheck results only")
        self.assertContains(response, "Download Missing Results ZIP")
        self.assertContains(response, "1 exported")

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

    def test_blocked_publication_export_writes_audit_event_with_blocker_context(self):
        self.make_master_paper("P001", "Needs Editorial Review", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Needs Editorial Review",
            extracted_title="Needs Editorial Review",
            title_author_verified=False,
        )

        self.assert_publication_blocked("Unverified Title/Author Extraction")

        event = self.latest_audit_event("publication_package_export")
        self.assertEqual(event["status"], "blocked")
        self.assertEqual(event["result_counts"]["blockers"], 1)
        self.assertEqual(event["extra"]["blockers"][0]["category"], "Unverified Title/Author Extraction")
        self.assertEqual(event["extra"]["blockers"][0]["paper_id"], "P001")
        self.assertEqual(event["extra"]["blockers"][0]["final_submission_id"], "10")
        self.assertNotIn(str(self.root), json.dumps(event, ensure_ascii=False))

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
        missing_source.source_file.delete(save=True)

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

    def test_editor_upload_is_prioritized_but_conflict_blocks_final_export_until_discarded(self):
        paper = self.make_master_paper("P001", "Email Replacement", "Ada")
        start2 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Email Replacement",
            extracted_title="Email Replacement",
            current_file_path=str(self.make_pdf_file("start2.pdf", b"start2 pdf")),
            source_current_file_path=str(self.make_source_file("start2.docx", b"start2 source")),
        )

        editor = create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Use author email version.",
        )
        start2.refresh_from_db()
        editor.refresh_from_db()

        self.assertFalse(start2.active_version)
        self.assertTrue(editor.active_version)
        self.assertEqual(editor.submission_origin, "editor_upload")
        self.assertEqual(editor_conflict_count(), 1)
        self.assert_publication_blocked("Start2/Editor Version Conflict")

        draft_path = export_publication_package(force=True)
        with zipfile.ZipFile(draft_path) as archive:
            warning_name = next(
                name for name in archive.namelist() if name.startswith("publication_package_warnings_")
            )
            self.assertIn(
                "Start2/Editor Version Conflict",
                archive.read(warning_name).decode("utf-8-sig"),
            )
            self.assertEqual(archive.read("PDF/P001-UNTITLED.pdf"), b"editor pdf")
            self.assertEqual(archive.read("Source/P001-UNTITLED.docx"), b"editor source")

        discard_submission(start2, "Author asked us to discard the Start2 version.")
        start2.refresh_from_db()
        editor.refresh_from_db()

        self.assertTrue(start2.discarded)
        self.assertTrue(editor.active_version)
        self.assertEqual(editor_conflict_count(), 0)
        self.assertNotIn(
            "Start2/Editor Version Conflict",
            {row["category"] for row in publication_readiness_rows()},
        )

    def test_editor_upload_title_guard_and_process_alert(self):
        paper = self.make_master_paper("P010", "Guarded Editor Paper", "Ada")

        with patch(
            "submissions.services.editor_uploads.get_title_author",
            return_value=("Guarded Editor Paper", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Guarded Editor Paper",
                    "final_submission_authors": "Ada",
                    "notes": "Author emailed replacement.\nUse this version.",
                    "pdf_file": self.uploaded_file("guarded.pdf", b"%PDF guarded"),
                    "source_file": self.uploaded_file("guarded.docx", b"source"),
                },
            )

        self.assertEqual(response.status_code, 302)
        editor = FinalSubmission.objects.get(submission_origin="editor_upload", paper_id_filled="P010")
        self.assertTrue(editor.paper_id_verified)
        self.assertEqual(editor.processing_status, "pending")
        self.make_master_paper("P012", "Missing PDF Paper", "Grace")
        self.make_final_submission(
            final_submission_id="1200",
            paper_id_filled="P012",
            final_submission_title="Missing PDF Paper",
            extracted_title="Missing PDF Paper",
            current_file_path="",
            processing_status="pending",
            page_count=None,
            pdf_hash="",
        )
        self.make_master_paper("P014", "Pending Start2 Paper", "Grace")
        start2_pending = self.make_final_submission(
            final_submission_id="1400",
            paper_id_filled="P014",
            final_submission_title="Pending Start2 Paper",
            extracted_title="Pending Start2 Paper",
            current_file_path=str(self.make_pdf_file("pending-start2.pdf", b"%PDF pending")),
            processing_status="pending",
            page_count=None,
            pdf_hash="",
        )

        page = self.client.get(reverse("submissions:dashboard"))
        self.assertContains(page, "Process PDFs needed")
        self.assertContains(page, "2 active PDFs")
        self.assertContains(page, "2 need processing")
        self.assertContains(page, "1 missing PDFs")
        self.assertContains(page, "Active PDFs Need Process")

        process_page = self.client.get(reverse("submissions:process"))
        self.assertContains(process_page, "Active PDFs not processed")
        self.assertContains(process_page, editor.final_submission_id)
        self.assertContains(process_page, start2_pending.final_submission_id)

        final_list = self.client.get(reverse("submissions:final_submission_list"), {"filter": "editor_uploads"})
        self.assertContains(final_list, "Editor note")
        self.assertContains(final_list, "Author emailed replacement.")

        organized = self.client.get(reverse("submissions:organized_list"))
        self.assertContains(organized, "Editor note")
        self.assertContains(organized, "Author emailed replacement.")

    def test_editor_upload_title_diff_requires_confirmation_and_manual_id_review(self):
        paper = self.make_master_paper("P011", "Master Editor Paper", "Ada")

        with patch(
            "submissions.services.editor_uploads.get_title_author",
            return_value=("Master Editor Paper", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Different Final Title",
                    "final_submission_authors": "Ada",
                    "notes": "Email version with a title mismatch.",
                    "pdf_file": self.uploaded_file("mismatch.pdf", b"%PDF mismatch"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Editor upload title check")
        self.assertContains(response, "PDF title differs from Final")
        self.assertEqual(FinalSubmission.objects.filter(submission_origin="editor_upload").count(), 0)

        token = response.context["editor_upload_confirmation"]["token"]
        response = self.client.post(
            reverse("submissions:editor_upload"),
            {
                "action": "confirm_editor_upload",
                "preview_token": token,
            },
        )

        self.assertEqual(response.status_code, 302)
        editor = FinalSubmission.objects.get(submission_origin="editor_upload", paper_id_filled="P011")
        self.assertFalse(editor.paper_id_verified)
        self.assertTrue(editor.auto_verify_blocked)
        self.assertEqual(editor.verification_status, "pending")

    def test_editor_upload_title_extraction_error_requires_confirmation(self):
        paper = self.make_master_paper("P013", "Extraction Error Editor Paper", "Ada")

        with patch(
            "submissions.services.editor_uploads.get_title_author",
            side_effect=ValueError("cannot read title"),
        ):
            response = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Extraction Error Editor Paper",
                    "final_submission_authors": "Ada",
                    "notes": "Email version with unreadable title.",
                    "pdf_file": self.uploaded_file("bad.pdf", b"%PDF bad"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Editor upload title check")
        self.assertContains(response, "Title extraction failed")
        self.assertEqual(FinalSubmission.objects.filter(submission_origin="editor_upload").count(), 0)

    def test_discarding_editor_upload_returns_start2_to_active_version(self):
        paper = self.make_master_paper("P001", "Back To Start2", "Ada")
        start2 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Back To Start2",
            extracted_title="Back To Start2",
        )
        editor = create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Temporary email version.",
        )

        discard_submission(editor, "Email version was superseded.")
        start2.refresh_from_db()
        editor.refresh_from_db()

        self.assertTrue(start2.active_version)
        self.assertFalse(editor.active_version)
        self.assertTrue(editor.discarded)
        self.assertEqual(editor_conflict_count(), 0)

        undo_discard_submission(editor)
        start2.refresh_from_db()
        editor.refresh_from_db()
        self.assertFalse(start2.active_version)
        self.assertTrue(editor.active_version)
        self.assertEqual(editor_conflict_count(), 1)

    def test_start2_import_preview_preserves_discard_and_protects_editor_upload(self):
        paper = self.make_master_paper("P001", "Protected Editor", "Ada")
        start2 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Protected Editor",
            extracted_title="Protected Editor",
        )
        discard_submission(start2, "Do not use Start2.")
        editor = create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Use email version.",
        )

        preview = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,Changed Start2,Ada,2026-05-07 09:00:00\n"
                f"{editor.final_submission_id},P001,Should Not Change,Ada,2026-05-07 09:00:00\n",
            )
        )
        start2_row = next(row for row in preview["rows"] if row["identifier"] == "10")
        editor_row = next(row for row in preview["rows"] if row["identifier"] == editor.final_submission_id)

        self.assertTrue(start2_row["currently_discarded"])
        self.assertTrue(editor_row["skip_apply"])
        apply_import_preview(preview["token"])
        start2.refresh_from_db()
        editor.refresh_from_db()
        self.assertTrue(start2.discarded)
        self.assertNotEqual(editor.final_submission_title, "Should Not Change")

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
        self.assertContains(response, "Verified, title differs")

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

    def test_author_count_paper_ids_link_to_current_publication_pdfs(self):
        for paper_id, title in [
            ("P010", "Corrected Link"),
            ("P011", "Active Link"),
            ("P012", "Original Link"),
            ("P013", "Missing Link"),
        ]:
            self.make_master_paper(paper_id, title, "Link Author")

        corrected = self.make_final_submission(
            final_submission_id="1010",
            paper_id_filled="P010",
            final_submission_title="Corrected Link",
            extracted_title="Corrected Link",
            extracted_authors="Link Author",
        )
        corrected.formatted_pdf_file.save(
            "corrected-link.pdf",
            ContentFile(b"corrected publication pdf"),
            save=True,
        )
        active_final = self.make_final_submission(
            final_submission_id="1011",
            paper_id_filled="P011",
            final_submission_title="Active Link",
            extracted_title="Active Link",
            extracted_authors="Link Author",
        )
        original = self.make_final_submission(
            final_submission_id="1012",
            paper_id_filled="P012",
            final_submission_title="Original Link",
            extracted_title="Original Link",
            extracted_authors="Link Author",
            current_file_path="",
        )
        original.pdf_file.save(
            "original-link.pdf",
            ContentFile(b"original publication pdf"),
            save=True,
        )
        missing = self.make_final_submission(
            final_submission_id="1013",
            paper_id_filled="P013",
            final_submission_title="Missing Link",
            extracted_title="Missing Link",
            extracted_authors="Link Author",
            current_file_path="",
            pdf_file=None,
        )
        inactive_old = self.make_final_submission(
            final_submission_id="1009",
            paper_id_filled="P010",
            final_submission_title="Corrected Link",
            extracted_title="Corrected Link",
            extracted_authors="Link Author",
            active_version=False,
            current_file_path=str(self.make_pdf_file("old-link.pdf", b"old pdf")),
        )

        rebuild_paper_authors()
        author_row = next(row for row in author_count_rows() if row["normalized_author_name"] == "link author")
        link_by_paper_id = {link["paper_id"]: link for link in author_row["paper_links"]}

        self.assertEqual(link_by_paper_id["P010"]["source"], "corrected")
        self.assertEqual(link_by_paper_id["P011"]["source"], "original")
        self.assertEqual(link_by_paper_id["P012"]["source"], "original")
        self.assertFalse(link_by_paper_id["P013"]["exists"])
        self.assertNotEqual(link_by_paper_id["P010"]["url"], reverse("submissions:publication_pdf", args=[inactive_old.pk]))

        response = self.client.get(reverse("submissions:author_count"))
        self.assertContains(response, reverse("submissions:publication_pdf", args=[corrected.pk]))
        self.assertContains(response, reverse("submissions:publication_pdf", args=[active_final.pk]))
        self.assertContains(response, reverse("submissions:publication_pdf", args=[original.pk]))
        self.assertContains(response, "No PDF")
        self.assertNotContains(response, reverse("submissions:publication_pdf", args=[inactive_old.pk]))

    def test_unclassified_final_blocks_until_marked_not_publishing(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        self.make_final_submission(final_submission_id="ready", paper_id_filled="P001")
        submission = self.make_final_submission(paper_id_filled="UNKNOWN")
        self.assert_publication_blocked("Unclassified Final Not In Master")

        mark_not_publishing(submission, "not_in_master", "wrong upload")
        self.assertEqual(publication_readiness_rows(), [])


class EditorPublicationVersionMatrixTests(EditorialAcceptanceTestCase):
    def organized_row_for(self, paper_id):
        rows, _summary, _settings_obj, _current_filter, _current_sort = organized_list_rows()
        return next(row for row in rows if row["paper"].paper_id == paper_id)

    def assert_organized_files(self, paper_id, submission, pdf_bytes, source_bytes):
        row = self.organized_row_for(paper_id)
        self.assertEqual(row["submission"].pk, submission.pk)
        self.assertEqual(Path(row["publication_pdf"]["path"]).read_bytes(), pdf_bytes)
        self.assertEqual(Path(row["publication_source"]["path"]).read_bytes(), source_bytes)

    def test_editor_discard_undo_matrix_keeps_organized_list_and_package_on_active_version(self):
        paper = self.make_master_paper("P001", "Matrix Paper", "Ada")
        old_start2 = self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P001",
            final_submission_title="Matrix Paper",
            extracted_title="Matrix Paper",
            current_file_path=str(self.make_pdf_file("start2-v9.pdf", b"start2 v9 pdf")),
            source_current_file_path=str(self.make_source_file("start2-v9.docx", b"start2 v9 source")),
        )
        current_start2 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Matrix Paper",
            extracted_title="Matrix Paper",
            current_file_path=str(self.make_pdf_file("start2-v10.pdf", b"start2 v10 pdf")),
            source_current_file_path=str(self.make_source_file("start2-v10.docx", b"start2 v10 source")),
        )
        determine_active_versions()
        old_start2.refresh_from_db()
        current_start2.refresh_from_db()
        self.assertFalse(old_start2.active_version)
        self.assertTrue(current_start2.active_version)
        self.assert_organized_files("P001", current_start2, b"start2 v10 pdf", b"start2 v10 source")

        editor = create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Use email replacement.",
        )
        self.mark_submission_publication_ready(editor, title="Matrix Paper")
        determine_active_versions()
        editor.refresh_from_db()
        current_start2.refresh_from_db()

        self.assertTrue(editor.active_version)
        self.assertFalse(current_start2.active_version)
        self.assert_organized_files("P001", editor, b"editor pdf", b"editor source")
        self.assert_publication_blocked("Start2/Editor Version Conflict")

        draft_path = export_publication_package(force=True)
        with zipfile.ZipFile(draft_path) as archive:
            self.assertEqual(archive.read("PDF/P001-Matrix Paper.pdf"), b"editor pdf")
            self.assertEqual(archive.read("Source/P001-Matrix Paper.docx"), b"editor source")
            self.assertNotEqual(archive.read("PDF/P001-Matrix Paper.pdf"), b"start2 v10 pdf")

        discard_submission(editor, "Email replacement rejected; use Start2 v10.")
        editor.refresh_from_db()
        current_start2.refresh_from_db()

        self.assertTrue(current_start2.active_version)
        self.assertFalse(editor.active_version)
        self.assert_organized_files("P001", current_start2, b"start2 v10 pdf", b"start2 v10 source")
        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.read("PDF/P001-Matrix Paper.pdf"), b"start2 v10 pdf")
            self.assertEqual(archive.read("Source/P001-Matrix Paper.docx"), b"start2 v10 source")

        undo_discard_submission(editor)
        editor.refresh_from_db()
        current_start2.refresh_from_db()

        self.assertTrue(editor.active_version)
        self.assertFalse(current_start2.active_version)
        self.assert_organized_files("P001", editor, b"editor pdf", b"editor source")
        self.assert_publication_blocked("Start2/Editor Version Conflict")

    def test_inactive_corrected_files_never_leak_to_organized_list_or_publication_package(self):
        self.make_master_paper("P001", "No Leak Paper", "Ada")
        old = self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P001",
            final_submission_title="No Leak Paper",
            extracted_title="No Leak Paper",
            current_file_path=str(self.make_pdf_file("old-original.pdf", b"old original pdf")),
            source_current_file_path=str(self.make_source_file("old-original.docx", b"old original source")),
        )
        old.formatted_pdf_file.save("old-corrected.pdf", ContentFile(b"old corrected pdf"), save=True)
        old.formatted_source_file.save("old-corrected.docx", ContentFile(b"old corrected source"), save=True)
        active = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="No Leak Paper",
            extracted_title="No Leak Paper",
            current_file_path=str(self.make_pdf_file("active.pdf", b"active pdf")),
            source_current_file_path=str(self.make_source_file("active.docx", b"active source")),
        )
        determine_active_versions()
        old.refresh_from_db()
        active.refresh_from_db()

        self.assertFalse(old.active_version)
        self.assertTrue(active.active_version)
        self.assert_organized_files("P001", active, b"active pdf", b"active source")

        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            pdf_bytes = archive.read("PDF/P001-No Leak Paper.pdf")
            source_bytes = archive.read("Source/P001-No Leak Paper.docx")
            self.assertEqual(pdf_bytes, b"active pdf")
            self.assertEqual(source_bytes, b"active source")
            self.assertNotEqual(pdf_bytes, b"old corrected pdf")
            self.assertNotEqual(source_bytes, b"old corrected source")

    def test_active_editor_corrected_files_are_used_after_start2_is_discarded(self):
        paper = self.make_master_paper("P001", "Corrected Editor", "Ada")
        start2 = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Corrected Editor",
            extracted_title="Corrected Editor",
            current_file_path=str(self.make_pdf_file("start2.pdf", b"start2 pdf")),
            source_current_file_path=str(self.make_source_file("start2.docx", b"start2 source")),
        )
        editor = create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor-original.pdf", b"editor original pdf"),
            source_file=self.uploaded_file("editor-original.docx", b"editor original source"),
            notes="Publisher supplied final correction.",
        )
        editor.formatted_pdf_file.save("editor-corrected.pdf", ContentFile(b"editor corrected pdf"), save=False)
        editor.formatted_source_file.save(
            "editor-corrected.docx", ContentFile(b"editor corrected source"), save=False
        )
        self.mark_submission_publication_ready(editor, title="Corrected Editor")
        discard_submission(start2, "Editor corrected package is the publication version.")
        editor.refresh_from_db()
        start2.refresh_from_db()

        self.assertTrue(editor.active_version)
        self.assertTrue(start2.discarded)
        self.assertEqual(publication_readiness_rows(), [])
        self.assert_organized_files(
            "P001",
            editor,
            b"editor corrected pdf",
            b"editor corrected source",
        )

        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.read("PDF/P001-Corrected Editor.pdf"), b"editor corrected pdf")
            self.assertEqual(archive.read("Source/P001-Corrected Editor.docx"), b"editor corrected source")


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
        event = self.latest_audit_event("publication_package_export")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["result_counts"]["paper_count"], 2)
        self.assertEqual(event["result_counts"]["skipped_count"], 0)
        self.assertEqual(event["result_counts"]["readiness_blockers"], 0)
        self.assertIn("publication_package_", event["file_changes"]["zip_path"])
        self.assertIn("publication_manifest_", event["file_changes"]["manifest_path"])
        self.assertNotIn(str(self.root), json.dumps(event, ensure_ascii=False))


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

    def test_final_submission_list_files_show_corrected_then_original(self):
        self.make_master_paper("P001", "Display Files", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Display Files",
            current_file_path=str(self.make_pdf_file("active-final.pdf", b"active final pdf")),
            source_current_file_path=str(self.make_source_file("active-final.docx", b"active source")),
            original_file_name="10_file_Submit_PDF.pdf",
            source_original_file_name="10_file_Submit_Source.zip",
        )
        submission.pdf_file.save("10_file_Submit_PDF.pdf", ContentFile(b"original pdf"), save=True)
        submission.source_file.save("10_file_Submit_Source.zip", ContentFile(b"original source"), save=True)
        submission.formatted_pdf_file.save("corrected.pdf", ContentFile(b"corrected pdf"), save=True)
        submission.formatted_source_file.save("corrected.docx", ContentFile(b"corrected source"), save=True)

        page = self.client.get(reverse("submissions:final_submission_list"))
        pdf_url = reverse("submissions:final_submission_display_pdf", args=[submission.pk])
        source_url = reverse("submissions:final_submission_display_source", args=[submission.pk])
        self.assertContains(page, "Original or Corrected files")
        self.assertContains(page, pdf_url)
        self.assertContains(page, source_url)
        self.assertContains(page, "corrected.pdf")
        self.assertContains(page, "corrected.docx")
        self.assertContains(page, "Corrected")
        self.assertNotContains(page, "active-final.pdf")
        self.assertNotContains(page, "active-final.docx")
        self.assertNotContains(page, reverse("submissions:publication_pdf", args=[submission.pk]))
        self.assertNotContains(page, reverse("submissions:publication_source", args=[submission.pk]))

        pdf_response = self.client.get(pdf_url)
        self.assertEqual(b"".join(pdf_response.streaming_content), b"corrected pdf")
        source_response = self.client.get(source_url)
        self.assertEqual(b"".join(source_response.streaming_content), b"corrected source")

        submission.formatted_pdf_file.delete(save=True)
        submission.formatted_source_file.delete(save=True)
        page = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(page, "10_file_Submit_PDF.pdf")
        self.assertContains(page, "10_file_Submit_Source.zip")
        self.assertContains(page, "Original")
        self.assertNotContains(page, "active-final.pdf")
        self.assertNotContains(page, "active-final.docx")
        self.assertEqual(b"".join(self.client.get(pdf_url).streaming_content), b"original pdf")
        self.assertEqual(b"".join(self.client.get(source_url).streaming_content), b"original source")

    def test_final_submission_display_file_links_404_when_only_current_paths_exist(self):
        self.make_master_paper("P001", "Missing Row Files", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Missing Row Files",
            current_file_path="",
            source_current_file_path="",
        )
        page = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(page, "No PDF")
        self.assertContains(page, "No source")
        self.assertEqual(
            self.client.get(reverse("submissions:final_submission_display_pdf", args=[submission.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("submissions:final_submission_display_source", args=[submission.pk])).status_code,
            404,
        )

    def test_replaced_final_submission_file_links_stay_on_that_row_originals(self):
        self.make_master_paper("P001", "Replaced Files", "Ada")
        replaced = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Replaced Files",
            active_version=False,
            current_file_path=str(self.make_pdf_file("replaced-current.pdf", b"replaced pdf")),
            source_current_file_path=str(self.make_source_file("replaced-current.docx", b"replaced source")),
            original_file_name="replaced-original.pdf",
            source_original_file_name="replaced-original.docx",
        )
        replaced.pdf_file.save("replaced-original.pdf", ContentFile(b"replaced original pdf"), save=True)
        replaced.source_file.save("replaced-original.docx", ContentFile(b"replaced original source"), save=True)
        active = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P001",
            final_submission_title="Replaced Files",
            active_version=True,
            current_file_path=str(self.make_pdf_file("active-current.pdf", b"active pdf")),
            source_current_file_path=str(self.make_source_file("active-current.docx", b"active source")),
        )

        page = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(page, reverse("submissions:final_submission_display_pdf", args=[replaced.pk]))
        self.assertContains(page, reverse("submissions:final_submission_display_pdf", args=[active.pk]))
        self.assertContains(page, "replaced-original.pdf")
        self.assertNotContains(page, "replaced-current.pdf")
        self.assertContains(page, "active-current.pdf")
        self.assertEqual(
            b"".join(
                self.client.get(
                    reverse("submissions:final_submission_display_pdf", args=[replaced.pk])
                ).streaming_content
            ),
            b"replaced original pdf",
        )
        self.assertEqual(
            b"".join(
                self.client.get(
                    reverse("submissions:final_submission_display_source", args=[replaced.pk])
                ).streaming_content
            ),
            b"replaced original source",
        )

    def test_old_versions_are_classified_and_exported(self):
        self.make_master_paper("P001", "Versioned Paper", "Ada")
        replaced = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Old Start2 Version",
            active_version=False,
            duplicate_submission=True,
        )
        self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P001",
            final_submission_title="Active Replacement",
            active_version=True,
        )
        discarded = self.make_final_submission(
            final_submission_id="30",
            paper_id_filled="P002",
            final_submission_title="Discarded Version",
            active_version=False,
            discarded=True,
            discard_notes="Author asked us not to use this file.",
        )
        editor_old = self.make_final_submission(
            final_submission_id="EDITOR-P003-001",
            paper_id_filled="P003",
            final_submission_title="Old Editor Upload",
            active_version=False,
            duplicate_submission=True,
            submission_origin="editor_upload",
            editor_upload_notes="Email replacement was superseded.",
        )
        not_publishing = self.make_final_submission(
            final_submission_id="40",
            paper_id_filled="P004",
            final_submission_title="Not Publishing Old Version",
            active_version=False,
            excluded_from_publication=True,
            publication_exclusion_notes="Unpaid paper retained for record.",
        )
        other = self.make_final_submission(
            final_submission_id="50",
            paper_id_filled="P005",
            final_submission_title="Other Inactive",
            active_version=False,
            duplicate_submission=False,
        )

        page = self.client.get(reverse("submissions:old_versions"))
        self.assertContains(page, "Inactive final submissions retained for traceability")
        self.assertContains(page, "Replaced")
        self.assertContains(page, "Final 20")
        self.assertContains(page, "Discarded")
        self.assertContains(page, "Author asked us not to use this file.")
        self.assertContains(page, "Editor Upload")
        self.assertContains(page, "Email replacement was superseded.")
        self.assertContains(page, "Not publishing flag")
        self.assertContains(page, "Unpaid paper retained for record.")
        self.assertContains(page, "Other inactive")
        self.assertNotContains(page, "Not Publishing</div>")
        self.assertNotContains(page, "filter=not_publishing")

        filter_expectations = {
            "replaced": replaced.final_submission_id,
            "discarded": discarded.final_submission_id,
            "editor_uploads": editor_old.final_submission_id,
            "start2": replaced.final_submission_id,
            "other": other.final_submission_id,
        }
        for filter_name, final_id in filter_expectations.items():
            with self.subTest(filter=filter_name):
                filtered = self.client.get(
                    reverse("submissions:old_versions"),
                    {"filter": filter_name},
                )
                self.assertContains(filtered, final_id)

        export_path = export_old_versions()
        frame = pd.read_excel(export_path)
        self.assertIn("old_version_status", frame.columns)
        self.assertIn("inactive_reason", frame.columns)
        self.assertIn("active_replacement_final_id", frame.columns)
        exported = {
            str(row["final_submission_id"]): row
            for row in frame.to_dict("records")
        }
        self.assertEqual(exported["10"]["old_version_status"], "Replaced")
        self.assertEqual(int(exported["10"]["active_replacement_final_id"]), 20)
        self.assertEqual(exported["30"]["old_version_status"], "Discarded")
        self.assertEqual(exported["40"]["old_version_status"], "Other inactive")
        self.assertTrue(exported["40"]["excluded_from_publication"])

        not_publishing_page = self.client.get(reverse("submissions:not_publishing_list"))
        self.assertContains(not_publishing_page, "Publication decisions")
        self.assertContains(not_publishing_page, "Inactive old version")
        self.assertContains(not_publishing_page, not_publishing.final_submission_id)

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

    def test_formatting_review_shows_source_file_type_labels(self):
        self.make_master_paper("P001", "Word Source", "Ada")
        self.make_master_paper("P002", "Zip Source", "Grace")
        self.make_master_paper("P003", "Tex Source", "Alan")
        self.make_master_paper("P004", "Unknown Source", "Katherine")
        word = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Word Source",
            extracted_title="Word Source",
        )
        zip_submission = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="Zip Source",
            extracted_title="Zip Source",
        )
        tex = self.make_final_submission(
            final_submission_id="103",
            paper_id_filled="P003",
            final_submission_title="Tex Source",
            extracted_title="Tex Source",
        )
        unknown = self.make_final_submission(
            final_submission_id="104",
            paper_id_filled="P004",
            final_submission_title="Unknown Source",
            extracted_title="Unknown Source",
        )
        word.source_file.save("word_source.docx", ContentFile(b"word"), save=True)
        zip_submission.source_file.save("zip_source.docx", ContentFile(b"zip original"), save=True)
        zip_submission.formatted_source_file.save("corrected_source.zip", ContentFile(b"zip"), save=True)
        tex.source_file.save("source.tex", ContentFile(b"tex"), save=True)
        unknown.source_file.save("source.pages", ContentFile(b"unknown"), save=True)

        response = self.client.get(reverse("submissions:formatting"), {"filter": "all"})
        self.assertContains(response, "Original Source (Word)")
        self.assertContains(response, "Corrected Source (ZIP)")
        self.assertContains(response, "Original Source (TeX)")
        self.assertContains(response, "Original Source (Unknown)")
        self.assertNotContains(response, "data-formatting-single-form")

    def test_formatting_review_ok_no_edit_filter(self):
        self.make_master_paper("P001", "Review OK Original", "Ada")
        self.make_master_paper("P002", "Review OK Source Edited", "Grace")
        self.make_master_paper("P003", "Pending Original", "Alan")
        self.make_master_paper("P004", "Review OK PDF Edited", "Katherine")
        original = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Review OK Original",
            extracted_title="Review OK Original",
            format_status="review_ok",
        )
        source_edited = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="Review OK Source Edited",
            extracted_title="Review OK Source Edited",
            format_status="review_ok",
        )
        pending = self.make_final_submission(
            final_submission_id="103",
            paper_id_filled="P003",
            final_submission_title="Pending Original",
            extracted_title="Pending Original",
            format_status="pending",
        )
        pdf_edited = self.make_final_submission(
            final_submission_id="104",
            paper_id_filled="P004",
            final_submission_title="Review OK PDF Edited",
            extracted_title="Review OK PDF Edited",
            format_status="review_ok",
        )
        source_edited.formatted_source_file.save("corrected_source.docx", ContentFile(b"fixed"), save=True)
        pdf_edited.formatted_pdf_file.save("corrected_pdf.pdf", ContentFile(b"%PDF fixed"), save=True)

        response = self.client.get(
            reverse("submissions:formatting"),
            {"filter": "review_ok_no_edit"},
        )

        self.assertContains(response, "Review OK, no edit")
        self.assertContains(response, original.final_submission_title)
        self.assertNotContains(response, source_edited.final_submission_title)
        self.assertNotContains(response, pdf_edited.final_submission_title)
        self.assertNotContains(response, pending.final_submission_title)

        response = self.client.get(
            reverse("submissions:formatting"),
            {"filter": "review_ok_no_edit", "q": "Original"},
        )
        self.assertContains(response, original.final_submission_title)
        self.assertNotContains(response, source_edited.final_submission_title)
        self.assertNotContains(response, pdf_edited.final_submission_title)

    def test_formatting_single_mode_shows_one_paper_and_saves_without_advancing(self):
        self.make_master_paper("P001", "First Format Paper", "Ada")
        self.make_master_paper("P002", "Second Format Paper", "Grace")
        first = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="First Format Paper",
            extracted_title="First Format Paper",
            format_status="pending",
        )
        second = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="Second Format Paper",
            extracted_title="Second Format Paper",
            format_status="pending",
        )

        response = self.client.get(
            reverse("submissions:formatting"),
            {"mode": "single", "filter": "all", "submission": first.pk},
        )
        self.assertContains(response, "Single Paper Mode")
        self.assertContains(response, "Previous")
        self.assertContains(response, "Next")
        self.assertContains(response, "Back to list")
        self.assertContains(response, "Go next")
        self.assertContains(response, "data-unsaved-check")
        self.assertContains(response, "data-formatting-single-form")
        self.assertNotContains(response, "Save and go next")
        self.assertContains(response, "First Format Paper")
        self.assertNotContains(response, "Second Format Paper</div>")
        self.assertContains(response, f"submission={second.pk}")

        with patch("submissions.services.formatting.get_title_author") as extractor:
            response = self.client.post(
                reverse("submissions:formatting"),
                {
                    "submission_id": first.pk,
                    "mode": "single",
                    "filter": "all",
                    "format_status": "pending",
                    "format_notes": "source only",
                    "corrected_source": self.uploaded_file("fixed.docx", b"fixed source"),
                },
            )
        extractor.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"submission={first.pk}", response["Location"])
        self.assertNotIn(f"submission={second.pk}", response["Location"])

    def test_formatting_corrected_pdf_title_guard_requires_confirmation_before_saving(self):
        self.make_master_paper("P001", "Correct Paper Title", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Correct Paper Title",
            extracted_title="Existing Extracted Title",
            title_author_review_status="review_ok",
            title_author_verified=True,
        )

        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Wrong Paper Title", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                {
                    "submission_id": submission.pk,
                    "mode": "single",
                    "filter": "all",
                    "format_status": "pending",
                    "format_notes": "corrected pdf",
                    "corrected_pdf": self.uploaded_file("wrong.pdf", b"%PDF wrong"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corrected PDF title check")
        self.assertContains(response, "Confirm save corrected files anyway")
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)
        self.assertEqual(submission.extracted_title, "Existing Extracted Title")
        self.assertEqual(submission.title_author_review_status, "review_ok")

        token = response.context["formatting_confirmation"]["token"]
        response = self.client.post(
            reverse("submissions:formatting"),
            {
                "action": "confirm_formatting_upload",
                "preview_token": token,
                "submission_id": submission.pk,
                "mode": "single",
                "filter": "all",
                "format_status": "pending",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"submission={submission.pk}", response["Location"])
        submission.refresh_from_db()
        self.assertTrue(submission.formatted_pdf_file)
        self.assertEqual(submission.extracted_title, "Existing Extracted Title")
        self.assertEqual(submission.title_author_review_status, "pending")

    def test_formatting_corrected_pdf_matching_title_saves_without_confirmation(self):
        self.make_master_paper("P001", "Matching Paper Title", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Matching Paper Title",
            extracted_title="Existing Extracted Title",
        )

        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Matching Paper Title", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                {
                    "submission_id": submission.pk,
                    "filter": "all",
                    "format_status": "pending",
                    "format_notes": "matching corrected pdf",
                    "corrected_source": self.uploaded_file("actually_pdf.pdf", b"%PDF corrected"),
                },
            )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.formatted_pdf_file)
        self.assertEqual(submission.extracted_title, "Existing Extracted Title")
        self.assertEqual(submission.title_author_review_status, "pending")

    def test_formatting_corrected_pdf_extraction_error_requires_confirmation(self):
        self.make_master_paper("P001", "Extraction Error Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Extraction Error Paper",
            extracted_title="Existing Extracted Title",
        )

        with patch(
            "submissions.services.formatting.get_title_author",
            side_effect=ValueError("cannot read title"),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                {
                    "submission_id": submission.pk,
                    "filter": "all",
                    "format_status": "pending",
                    "format_notes": "bad corrected pdf",
                    "corrected_pdf": self.uploaded_file("bad.pdf", b"%PDF bad"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Title extraction failed")
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)

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
        event = self.latest_audit_event("final_submission_manual_edit")
        self.assertEqual(event["status"], "success")
        self.assertIn("final_submission_title", event["changed_fields"])
        self.assertEqual(event["before"]["final_submission_title"], "Original Accepted Title")
        self.assertEqual(event["after"]["final_submission_title"], "Final Revised Title For Publication")
        self.assertTrue(event["reset_flags"]["identity_recalculated"])
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
        event = self.latest_audit_event("final_submission_manual_edit")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["paper_id"], "P001")
        self.assertEqual(event["final_submission_id"], "20")
        self.assertIn("pdf_file", event["changed_fields"])
        self.assertIn("original.pdf", event["before"]["pdf_file"])
        self.assertIn("replacement", event["after"]["pdf_file"])
        self.assertIsNone(event["after"]["page_count"])
        self.assertEqual(event["after"]["pdf_hash"], "")
        self.assertTrue(event["reset_flags"]["pdf_reset"])
        self.assertTrue(event["reset_flags"]["corrected_files_archived"])
        self.assertTrue(event["file_changes"]["pdf_changed"])
        self.assertFalse(event["file_changes"]["source_changed"])
        self.assertRegex(event["file_hashes"]["pdf_file_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.root), json.dumps(event, ensure_ascii=False))
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

    def test_organized_list_details_download_current_publication_files(self):
        self.make_master_paper("P001", "Download Files", "Ada")
        active = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Download Files",
            extracted_title="Download Files",
            current_file_path=str(self.make_pdf_file("active-publication.pdf", b"active pdf")),
            source_current_file_path=str(self.make_source_file("active-source.zip", b"active source")),
        )
        inactive = self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P001",
            final_submission_title="Download Files",
            extracted_title="Download Files",
            active_version=False,
            current_file_path=str(self.make_pdf_file("old-publication.pdf", b"old pdf")),
            source_current_file_path=str(self.make_source_file("old-source.zip", b"old source")),
        )

        response = self.client.get(reverse("submissions:organized_list"))
        self.assertContains(response, "Download publication files")
        self.assertContains(response, "Download PDF")
        self.assertContains(response, "Download Source")
        self.assertContains(response, reverse("submissions:publication_pdf", args=[active.pk]))
        self.assertContains(response, reverse("submissions:publication_source", args=[active.pk]))
        self.assertNotContains(response, reverse("submissions:publication_pdf", args=[inactive.pk]))
        self.assertNotContains(response, reverse("submissions:publication_source", args=[inactive.pk]))

        source_response = self.client.get(reverse("submissions:publication_source", args=[active.pk]))
        self.assertEqual(source_response.status_code, 200)
        self.assertIn("attachment", source_response["Content-Disposition"])
        self.assertEqual(b"".join(source_response.streaming_content), b"active source")

    def test_settings_active_version_rule_change_reports_changed_papers_without_resetting_flags(self):
        self.make_master_paper("P001", "Rule Change", "Ada")
        older_newer_date = timezone.now()
        older = self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="P001",
            final_submission_title="Rule Change",
            extracted_title="Rule Change",
            upload_date=older_newer_date,
            paper_id_verified=True,
            verification_status="verified",
        )
        newer = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Rule Change",
            extracted_title="Rule Change",
            upload_date=older_newer_date - timezone.timedelta(days=1),
            paper_id_verified=True,
            verification_status="verified",
        )
        determine_active_versions()
        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertFalse(older.active_version)
        self.assertTrue(newer.active_version)

        settings_obj = AppSetting.load()
        post_data = {
            "action": "save_settings",
            "conference_name": settings_obj.conference_name,
            "page_minimum": settings_obj.page_minimum,
            "page_limit": settings_obj.page_limit,
            "author_paper_limit": settings_obj.author_paper_limit,
            "max_authors_per_paper": settings_obj.max_authors_per_paper,
            "title_words_for_filename": settings_obj.title_words_for_filename,
            "active_version_rule": "upload_date",
            "time_zone": settings_obj.time_zone,
            "publication_pdf_debug_folder": settings_obj.publication_pdf_debug_folder,
            "reports_folder": settings_obj.reports_folder,
            "extraction_results_folder": settings_obj.extraction_results_folder,
            "plagiarism_reports_folder": settings_obj.plagiarism_reports_folder,
            "plagiarism_percent_threshold": settings_obj.plagiarism_percent_threshold,
            "single_similarity_threshold": settings_obj.single_similarity_threshold,
        }
        response = self.client.post(reverse("submissions:settings"), post_data, follow=True)

        older.refresh_from_db()
        newer.refresh_from_db()
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.active_version_rule, "final_id")
        self.assertFalse(older.active_version)
        self.assertTrue(newer.active_version)
        self.assertContains(response, "Active Version Rule Change Preview")
        self.assertContains(response, "1 paper would change active final version")
        self.assertContains(response, "P001")
        self.assertContains(response, "10")
        self.assertContains(response, "9")

        preview = self.client.session["active_version_rule_preview"]
        response = self.client.post(
            reverse("submissions:settings"),
            {
                "action": "confirm_active_rule_change",
                "preview_token": preview["token"],
            },
            follow=True,
        )

        older.refresh_from_db()
        newer.refresh_from_db()
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.active_version_rule, "upload_date")
        self.assertTrue(older.active_version)
        self.assertFalse(newer.active_version)
        self.assertTrue(older.paper_id_verified)
        self.assertTrue(newer.paper_id_verified)
        self.assertContains(response, "Active final version rule changed.")
        self.assertContains(response, "1 paper changed active final version")
        self.assertContains(response, "P001")
        self.assertContains(response, "10")
        self.assertContains(response, "9")
        self.assertContains(response, "Existing review flags were not reset")

    def test_active_version_rule_preview_zero_changed_and_stale_confirm(self):
        self.make_master_paper("P001", "No Change Rule", "Ada")
        first = self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="No Change Rule",
            extracted_title="No Change Rule",
            upload_date=timezone.now() - timezone.timedelta(days=1),
        )
        second = self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P001",
            final_submission_title="No Change Rule",
            extracted_title="No Change Rule",
            upload_date=timezone.now(),
        )
        determine_active_versions()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.active_version)
        self.assertTrue(second.active_version)

        settings_obj = AppSetting.load()
        post_data = {
            "action": "save_settings",
            "conference_name": settings_obj.conference_name,
            "page_minimum": settings_obj.page_minimum,
            "page_limit": settings_obj.page_limit,
            "author_paper_limit": settings_obj.author_paper_limit,
            "max_authors_per_paper": settings_obj.max_authors_per_paper,
            "title_words_for_filename": settings_obj.title_words_for_filename,
            "active_version_rule": "upload_date",
            "time_zone": settings_obj.time_zone,
            "publication_pdf_debug_folder": settings_obj.publication_pdf_debug_folder,
            "reports_folder": settings_obj.reports_folder,
            "extraction_results_folder": settings_obj.extraction_results_folder,
            "plagiarism_reports_folder": settings_obj.plagiarism_reports_folder,
            "plagiarism_percent_threshold": settings_obj.plagiarism_percent_threshold,
            "single_similarity_threshold": settings_obj.single_similarity_threshold,
        }
        response = self.client.post(reverse("submissions:settings"), post_data, follow=True)
        self.assertContains(response, "0 papers would change active final version")
        self.assertContains(response, "both rules currently select the same active final versions")
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.active_version_rule, "final_id")

        preview = self.client.session["active_version_rule_preview"]
        first.active_version = True
        first.save(update_fields=["active_version", "updated_at"])
        second.active_version = False
        second.save(update_fields=["active_version", "updated_at"])

        response = self.client.post(
            reverse("submissions:settings"),
            {
                "action": "confirm_active_rule_change",
                "preview_token": preview["token"],
            },
            follow=True,
        )
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.active_version_rule, "final_id")
        self.assertContains(response, "Active versions changed after the preview was created")

    def test_final_submission_edit_separates_publication_and_original_files(self):
        self.make_master_paper("P001", "Edit Files", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Edit Files",
            extracted_title="Edit Files",
            current_file_path=str(self.make_pdf_file("edit-active.pdf", b"active pdf")),
            source_current_file_path=str(self.make_source_file("edit-active.docx", b"active source")),
            original_file_name="10_file_Submit_PDF.pdf",
            source_original_file_name="10_file_Submit_Source.zip",
        )

        response = self.client.get(reverse("submissions:final_submission_edit", args=[submission.pk]))
        self.assertContains(response, "Current Publication Files")
        self.assertContains(response, "Original Submission Files")
        self.assertContains(response, "Open PDF")
        self.assertContains(response, "Download Source")
        self.assertContains(response, reverse("submissions:publication_pdf", args=[submission.pk]))
        self.assertContains(response, reverse("submissions:publication_source", args=[submission.pk]))
        self.assertContains(response, "10_file_Submit_PDF.pdf")
        self.assertContains(response, "10_file_Submit_Source.zip")
        self.assertContains(response, "Uploads here replace original submission files only")

    def test_final_submission_edit_returns_to_organized_list_when_next_is_safe(self):
        self.make_master_paper("P001", "Return To Organized", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Return To Organized",
            extracted_title="Return To Organized",
        )
        organized_url = (
            reverse("submissions:organized_list")
            + "?filter=all&sort=needs_attention&q=P001"
        )

        organized = self.client.get(
            reverse("submissions:organized_list"),
            {"filter": "all", "sort": "needs_attention", "q": "P001"},
        )
        self.assertContains(organized, f"next={quote(organized_url, safe='/')}")

        edit_page = self.client.get(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            {"next": organized_url},
        )
        self.assertContains(
            edit_page,
            f'<input type="hidden" name="next" value="{organized_url}">',
            html=True,
        )
        self.assertContains(edit_page, f'<a class="btn btn-outline-secondary" href="{organized_url}">Back</a>', html=True)

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                next=organized_url,
                final_submission_title="Return To Organized Updated",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], organized_url)

    def test_final_submission_edit_falls_back_without_safe_next(self):
        self.make_master_paper("P001", "Return Fallback", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Return Fallback",
            extracted_title="Return Fallback",
        )

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                final_submission_title="Final Submissions Return",
            ),
        )
        self.assertRedirects(response, reverse("submissions:final_submission_list"))

        malicious = "https://example.com/steal"
        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(submission, next=malicious),
        )
        self.assertRedirects(response, reverse("submissions:final_submission_list"))

        invalid = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(
                submission,
                next=reverse("submissions:organized_list"),
                final_submission_id="",
            ),
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(
            invalid,
            f'<input type="hidden" name="next" value="{reverse("submissions:organized_list")}">',
            html=True,
        )

    def test_excel_exports_handle_nat_datetime_values(self):
        self.make_master_paper("P001", "Ready Paper", "Ada", notes="Internal editorial note")
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
        active_frame = pd.read_excel(active_path)
        self.assertIn("paper_master_notes", active_frame.columns)
        self.assertEqual(active_frame.loc[0, "paper_master_notes"], "Internal editorial note")
        workbook = pd.ExcelFile(workbook_path)
        self.assertIn("Paper Master", workbook.sheet_names)
        master_frame = pd.read_excel(workbook, sheet_name="Paper Master")
        self.assertIn("notes", master_frame.columns)
        self.assertIn("Internal editorial note", set(master_frame["notes"].fillna("")))
        not_publishing_frame = pd.read_excel(workbook, sheet_name="Not Publishing")
        self.assertIn("active_version", not_publishing_frame.columns)
        self.assertIn("version_state", not_publishing_frame.columns)
        self.assertIn("submission_origin", not_publishing_frame.columns)
        self.assertIn("active_replacement_final_id", not_publishing_frame.columns)

    def test_editor_conflict_alert_and_final_submission_filters(self):
        paper = self.make_master_paper("P001", "Conflict UI", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Conflict UI",
            extracted_title="Conflict UI",
        )
        create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Use email version.",
        )

        dashboard = self.client.get(reverse("submissions:dashboard"))
        self.assertContains(dashboard, "Start2/Editor version decision needed")
        self.assertContains(dashboard, "Start2/Editor Conflicts")

        filtered = self.client.get(
            reverse("submissions:final_submission_list"),
            {"filter": "version_conflicts"},
        )
        self.assertContains(filtered, "Version conflict")
        self.assertContains(filtered, "Editor Upload")
        self.assertContains(filtered, 'data-bs-target="#discard-row-')
        self.assertContains(filtered, "Discard reason required")
        self.assertContains(filtered, "Confirm discard version")

    def test_final_submission_discard_requires_note_and_uses_expanded_panel(self):
        self.make_master_paper("P001", "Discard UI", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Discard UI",
            extracted_title="Discard UI",
        )

        page = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(page, f'id="discard-row-{submission.pk}"')
        self.assertContains(page, "Discard reason required")
        self.assertContains(page, "Confirm discard version")
        self.assertContains(page, "<th>Files</th>", html=True)
        self.assertNotContains(page, "<th>PDF</th>", html=True)
        self.assertNotContains(page, "<th>Source</th>", html=True)
        self.assertContains(page, 'colspan="9"')

        missing_note = self.client.post(
            reverse("submissions:final_submission_list"),
            {"submission_id": submission.pk, "action": "discard_submission", "discard_notes": ""},
            follow=True,
        )
        submission.refresh_from_db()
        self.assertFalse(submission.discarded)
        self.assertContains(missing_note, "Discard requires a note.")

        self.client.post(
            reverse("submissions:final_submission_list"),
            {
                "submission_id": submission.pk,
                "action": "discard_submission",
                "discard_notes": "Author requested this version be discarded.",
            },
        )
        submission.refresh_from_db()
        self.assertTrue(submission.discarded)
        self.assertFalse(submission.active_version)

    def test_paper_note_summary_and_final_submission_tabs_render(self):
        noted = self.make_master_paper(
            "P001",
            "Noted Paper",
            "Ada",
            notes="Check special session placement.",
        )
        self.make_master_paper("P002", "Plain Paper", "Grace")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Noted Paper",
            extracted_title="Noted Paper",
        )
        create_editor_submission(
            paper=noted,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Use editor copy.",
        )

        master_page = self.client.get(reverse("submissions:initial_paper_list"))
        self.assertContains(master_page, "Note Summary (1)")
        self.assertContains(master_page, "Check special session placement.")
        self.assertContains(master_page, "cfm-title-cell")

        organized = self.client.get(reverse("submissions:organized_list"))
        self.assertContains(organized, "Note Summary (1)")
        self.assertContains(organized, "Note")

        final_page = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(final_page, 'class="nav nav-tabs')
        self.assertContains(final_page, "Editor uploads")
        self.assertContains(final_page, "Start2")

        editor_tab = self.client.get(
            reverse("submissions:final_submission_list"),
            {"filter": "editor_uploads"},
        )
        self.assertContains(editor_tab, "Editor Upload")
        self.assertNotContains(editor_tab, ">10<")

    def test_editor_visible_data_matches_publication_package_contents(self):
        self.make_master_paper(
            "P001",
            "Main Ready Paper",
            "Ada Lovelace; Alan Turing",
            notes="Do not send this editorial note to publisher.",
        )
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
        self.assertContains(dashboard, "1 current not publishing")
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
            self.assertNotIn("paper_master_notes", manifest_by_id["P001"])
            self.assertNotIn("Do not send this editorial note", archive.read(manifest_name).decode("utf-8-sig"))
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

    def test_error_report_includes_discarded_and_not_publishing_as_info(self):
        self.make_master_paper("P001", "Discarded Info", "Ada")
        discarded = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Discarded Info",
            extracted_title="Discarded Info",
        )
        discard_submission(discarded, "Use editor-uploaded email version instead.")

        self.make_master_paper("P002", "Not Publishing Info", "Grace")
        not_publishing = self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="P002",
            final_submission_title="Not Publishing Info",
            extracted_title="Not Publishing Info",
        )
        mark_not_publishing(not_publishing, "unpaid", "No publication fee received.")

        rows = error_report_rows()
        by_category = {row["category"]: row for row in rows}

        self.assertEqual(by_category["Discarded Final Submission"]["severity"], "info")
        self.assertEqual(by_category["Not Publishing Final Submission"]["severity"], "info")
        self.assertEqual(by_category["Discarded Final Submission"]["group"], "Version Tracking")
        self.assertIn("Use editor-uploaded email version instead.", by_category["Discarded Final Submission"]["message"])
        self.assertIn("No publication fee received.", by_category["Not Publishing Final Submission"]["message"])

        page = self.client.get(reverse("submissions:error_report"))
        self.assertContains(page, "Discarded Final Submission")
        self.assertContains(page, "Not Publishing Final Submission")
        self.assertContains(page, "Version Tracking")

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
