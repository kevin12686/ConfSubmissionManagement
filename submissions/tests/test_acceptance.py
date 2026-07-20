import csv
import hashlib
import io
import importlib.util
import json
import os
import shutil
import tempfile
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

import pandas as pd
from django.conf import settings as django_settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models.query import QuerySet
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
from submissions.services.file_inspection import (
    FileChangedDuringInspection,
    FileInspectionContext,
    clear_file_hash_cache,
)
from submissions.services.formatting import (
    formatting_filter_counts,
    formatting_preview_info,
    update_formatting_submission,
)
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
from submissions.services import publication_read
from submissions.services.publication_read import PublicationReadContext
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
    build_storage_reference_index,
    preview_storage_cleanup,
    repair_publication_paths,
    sync_publication_pdf_debug_folder,
)
from submissions.services.title_author_extraction import (
    ManualOverrideError,
    apply_title_author_manual_override,
    extract_grobid_for_suspicious_rows,
    extract_title_author_for_submission,
    extract_title_author_with_grobid,
    generate_text_verification_image,
    is_grobid_suspicious,
    unverify_extracted_title,
    unverify_title_author,
    verification_image_dimensions,
    verification_image_url,
    verify_extracted_title,
    verify_title_author,
)
from submissions.services.grobid_extractor import (
    GrobidExtractionError,
    GrobidExtractionResult,
    check_grobid_api,
    parse_grobid_tei,
)
from submissions.services.verification import (
    build_title_guard_context,
    mark_not_publishing,
    text_diff_html,
    undo_not_publishing,
    unverify_submission,
    verification_rows,
    verify_submission,
)
from submissions.services.workflow_evidence import (
    exception_review_evidence,
    final_submission_edit_evidence,
    formatting_issue_evidence,
    make_evidence_token,
    require_evidence_token,
)
from submissions.services.audit import (
    _tail_utf8_lines,
    audit_log_path,
    read_audit_log,
    write_audit_event,
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
        self.crosscheck_root = self.root / "crosscheck_upload"
        self.audit_root = self.root / "logs"
        self.system_state_reports_root.mkdir()
        self.system_state_restore_root.mkdir()
        self.storage_cleanup_root.mkdir()
        self.crosscheck_root.mkdir()
        self.audit_root.mkdir()
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
            lambda create=True: self.storage_cleanup_root,
        )
        self.audit_root_patcher = patch(
            "submissions.services.audit.audit_log_root",
            lambda: self.audit_root,
        )
        self.crosscheck_root_patcher = patch(
            "submissions.services.crosscheck.crosscheck_export_root",
            lambda: self.crosscheck_root,
        )
        self.system_state_audit_root_patcher = patch(
            "submissions.services.system_state.audit_log_root",
            lambda: self.audit_root,
        )
        self.system_state_reports_patcher.start()
        self.system_state_restore_patcher.start()
        self.storage_cleanup_patcher.start()
        self.audit_root_patcher.start()
        self.crosscheck_root_patcher.start()
        self.system_state_audit_root_patcher.start()
        self.addCleanup(self.system_state_reports_patcher.stop)
        self.addCleanup(self.system_state_restore_patcher.stop)
        self.addCleanup(self.storage_cleanup_patcher.stop)
        self.addCleanup(self.audit_root_patcher.stop)
        self.addCleanup(self.crosscheck_root_patcher.stop)
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
                values.setdefault(
                    "source_hash",
                    hashlib.sha256(media_source.read_bytes()).hexdigest(),
                )
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
        source_info = publication_source_info(submission)
        submission.source_hash = (
            hashlib.sha256(Path(source_info["path"]).read_bytes()).hexdigest()
            if source_info["exists"]
            else ""
        )
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
            "evidence_token": make_evidence_token(
                "final-submission-edit",
                final_submission_edit_evidence(submission),
            ),
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

    def exception_evidence_token(self, exception_key):
        rebuild_paper_authors()
        rows, _status = exception_rows("all", hydrate=False)
        row = next(item for item in rows if item["key"] == exception_key)
        return make_evidence_token(
            "exception-review",
            exception_review_evidence(row),
        )

    def settings_evidence_token(self):
        response = self.client.get(reverse("submissions:settings"))
        self.assertEqual(response.status_code, 200)
        return response.context["settings_evidence_token"]

    def paper_id_review_token(self, submission, token_key):
        response = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {
                "filter": "all",
                "submission": submission.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        row = next(
            item
            for item in response.context["rows"]
            if item["submission"].pk == submission.pk
        )
        return row[token_key]

    def publication_decision_token(self, submission):
        response = self.client.get(
            reverse("submissions:not_publishing_list"),
            {"submission": submission.pk},
        )
        self.assertEqual(response.status_code, 200)
        return response.context[
            "focused_submission"
        ].publication_decision_evidence_token

    def version_decision_token(self, submission):
        response = self.client.get(
            reverse("submissions:final_submission_list"),
            {"q": submission.final_submission_id},
        )
        self.assertEqual(response.status_code, 200)
        row = next(
            item
            for item in response.context["submissions"]
            if item.pk == submission.pk
        )
        return row.version_decision_evidence_token

    def duplicate_author_review_token(self, submission):
        response = self.client.get(
            reverse("submissions:organized_list"),
            {
                "paper_id": submission.paper_id_filled,
                "filter": "all",
            },
        )
        self.assertEqual(response.status_code, 200)
        row = next(
            item
            for item in response.context["rows"]
            if item.get("submission")
            and item["submission"].pk == submission.pk
        )
        return row["duplicate_author_evidence_token"]

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
    def setUp(self):
        super().setUp()
        self.base_dir_override = override_settings(
            BASE_DIR=self.root,
        )
        self.base_dir_override.enable()
        self.addCleanup(self.base_dir_override.disable)

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
        self.assertContains(response, 'class="cfm-code-block small mb-0"')
        self.assertNotContains(response, 'class="small bg-light border p-2 mb-0"')
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

    def test_audit_log_default_read_returns_utf8_tail_without_full_scan(self):
        lines = [
            json.dumps(
                {
                    "action": f"event_{index}",
                    "message": f"履歷 {index}",
                },
                ensure_ascii=False,
            )
            for index in range(1000)
        ]
        audit_log_path().write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

        with patch(
            "submissions.services.audit._tail_utf8_lines",
            wraps=_tail_utf8_lines,
        ) as tail_reader:
            events = read_audit_log(limit=10)

        tail_reader.assert_called_once()
        self.assertEqual(
            [event["action"] for event in events],
            [f"event_{index}" for index in range(999, 989, -1)],
        )
        self.assertEqual(events[0]["message"], "履歷 999")
        self.assertEqual(
            [
                json.loads(line)["action"]
                for line in _tail_utf8_lines(
                    audit_log_path(),
                    10,
                    chunk_size=13,
                )
            ],
            [f"event_{index}" for index in range(990, 1000)],
        )

    def test_clear_database_wipes_app_state_settings_and_managed_files(self):
        settings_obj = AppSetting.load()
        settings_obj.conference_name = "Conference To Wipe"
        settings_obj.reports_folder = str(
            self.root / "data" / "reports"
        )
        settings_obj.active_final_folder = str(
            self.root / "data" / "active"
        )
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
            self.root / "data" / "reports" / "report.xlsx",
            self.root / "data" / "active" / "active.pdf",
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

    def test_clear_database_preserves_external_configured_folder(self):
        shared_folder = self.root / "shared-editor-files"
        unrelated_file = shared_folder / "do-not-delete.txt"
        unrelated_file.parent.mkdir(parents=True)
        unrelated_file.write_bytes(b"editor-owned file")
        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(shared_folder)
        settings_obj.save(update_fields=["reports_folder"])
        self.make_master_paper("EXT1")
        self.make_final_submission(
            final_submission_id="EXT1",
            paper_id_filled="EXT1",
        )

        with override_settings(
            BASE_DIR=self.root,
            MEDIA_ROOT=self.media_root,
        ):
            response = self.client.post(
                reverse("submissions:clear_database"),
                {"confirmation": "CLEAR DATABASE"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InitialPaper.objects.count(), 0)
        self.assertEqual(FinalSubmission.objects.count(), 0)
        self.assertTrue(unrelated_file.exists())
        self.assertEqual(
            unrelated_file.read_bytes(),
            b"editor-owned file",
        )
        self.assertContains(
            response,
            "configured external folder(s) were preserved",
        )

    def test_clear_database_restores_files_when_database_delete_fails(self):
        self.make_master_paper("ROLLBACK1")
        submission = self.make_final_submission(
            final_submission_id="ROLLBACK1",
            paper_id_filled="ROLLBACK1",
        )
        publication_input = Path(submission.pdf_file.path)
        before_bytes = publication_input.read_bytes()
        original_delete = QuerySet.delete

        def fail_paper_delete(queryset):
            if queryset.model is InitialPaper:
                raise RuntimeError("injected database failure")
            return original_delete(queryset)

        with override_settings(
            BASE_DIR=self.root,
            MEDIA_ROOT=self.media_root,
        ), patch.object(
            QuerySet,
            "delete",
            new=fail_paper_delete,
        ):
            response = self.client.post(
                reverse("submissions:clear_database"),
                {"confirmation": "CLEAR DATABASE"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InitialPaper.objects.count(), 1)
        self.assertEqual(FinalSubmission.objects.count(), 1)
        self.assertTrue(publication_input.exists())
        self.assertEqual(publication_input.read_bytes(), before_bytes)
        self.assertContains(
            response,
            "Database changes were rolled back",
        )
        event = self.latest_audit_event("clear_database_apply")
        self.assertEqual(event["status"], "failed")

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

    def test_clear_database_completion_audit_failure_does_not_hide_committed_wipe(self):
        self.make_master_paper("AUDIT1")

        with patch(
            "submissions.controllers.settings.audit_success",
            side_effect=OSError("audit storage unavailable"),
        ):
            response = self.client.post(
                reverse("submissions:clear_database"),
                {"confirmation": "CLEAR DATABASE"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(InitialPaper.objects.exists())
        self.assertContains(
            response,
            "completion Audit Log entry could not be written",
        )
        events = read_audit_log()
        self.assertTrue(
            any(
                event["action"] == "clear_database_apply"
                and event["status"] == "requested"
                for event in events
            )
        )

    def test_clear_database_blocks_configured_data_root_without_touching_state(self):
        self.make_master_paper("SAFE1")
        settings_obj = AppSetting.load()
        settings_obj.reports_folder = "data"
        settings_obj.save(update_fields=["reports_folder"])
        marker = self.root / "data" / "must-stay.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"preserve data root")
        write_audit_event(
            action="before_unsafe_clear",
            status="success",
        )

        response = self.client.post(
            reverse("submissions:clear_database"),
            {"confirmation": "CLEAR DATABASE"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Clear Database failed. Database changes were rolled back",
        )
        self.assertTrue(InitialPaper.objects.filter(paper_id="SAFE1").exists())
        self.assertEqual(marker.read_bytes(), b"preserve data root")
        self.assertIn(
            "before_unsafe_clear",
            audit_log_path().read_text(encoding="utf-8"),
        )

    def test_clear_database_blocks_folder_inside_audit_root(self):
        self.make_master_paper("SAFE2")
        settings_obj = AppSetting.load()
        settings_obj.reports_folder = "data/logs/exports"
        settings_obj.save(update_fields=["reports_folder"])
        write_audit_event(
            action="before_audit_overlap_clear",
            status="success",
        )

        response = self.client.post(
            reverse("submissions:clear_database"),
            {"confirmation": "CLEAR DATABASE"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Clear Database failed. Database changes were rolled back",
        )
        self.assertTrue(InitialPaper.objects.filter(paper_id="SAFE2").exists())
        self.assertIn(
            "before_audit_overlap_clear",
            audit_log_path().read_text(encoding="utf-8"),
        )

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
        response = self.client.get(
            reverse("submissions:storage_inventory_panel")
        )
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

    def test_storage_cleanup_apply_rechecks_referenced_cache_directories(self):
        submission = self.make_final_submission(
            final_submission_id="8102-REFRESH",
            paper_id_filled="S002",
        )
        cache_file = (
            self.media_root
            / "pdf_thumbnails"
            / "new-reference"
            / "nested"
            / "page-1.png"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"cache")

        preview = preview_storage_cleanup()
        selected_paths = {
            Path(row["path"]).resolve() for row in preview["files"]
        }
        self.assertIn(cache_file.resolve(), selected_paths)

        submission.thumbnail_folder = str(
            self.media_root / "pdf_thumbnails" / "new-reference"
        )
        submission.save(update_fields=["thumbnail_folder", "updated_at"])
        reference_index = build_storage_reference_index()
        self.assertTrue(reference_index.is_referenced(cache_file))

        result = apply_storage_cleanup(
            preview["token"],
            CLEANUP_CONFIRMATION_TEXT,
        )

        self.assertTrue(cache_file.exists())
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("now referenced", result["skipped"][0]["message"])

    def test_storage_cleanup_apply_skips_file_replaced_after_preview(self):
        cache_file = (
            self.media_root
            / "format_previews"
            / "replaced-after-preview.png"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"old cache")
        preview = preview_storage_cleanup()

        cache_file.unlink()
        cache_file.write_bytes(b"new important cache")
        result = apply_storage_cleanup(
            preview["token"],
            CLEANUP_CONFIRMATION_TEXT,
        )

        self.assertTrue(cache_file.exists())
        self.assertEqual(cache_file.read_bytes(), b"new important cache")
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("changed after preview", result["skipped"][0]["message"])

    def test_storage_cleanup_apply_rechecks_category_after_folder_change(self):
        cache_file = (
            self.media_root
            / "format_previews"
            / "becomes-report.zip"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"same publication bytes")
        preview = preview_storage_cleanup()
        self.assertIn(
            cache_file.resolve(),
            {
                Path(row["path"]).resolve()
                for row in preview["files"]
            },
        )

        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(cache_file.parent)
        settings_obj.save(update_fields=["reports_folder"])
        result = apply_storage_cleanup(
            preview["token"],
            CLEANUP_CONFIRMATION_TEXT,
        )

        self.assertTrue(cache_file.exists())
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("no longer eligible", result["skipped"][0]["message"])

    def test_storage_references_use_identity_on_case_insensitive_filesystem(self):
        tree_actual = self.media_root / "pdf_thumbnails" / "MixedCase"
        tree_actual.mkdir(parents=True)
        tree_file = tree_actual / "page-1.png"
        tree_file.write_bytes(b"thumbnail")
        tree_alternate = tree_actual.with_name("mixedcase")
        exact_actual = (
            self.media_root
            / "title_author_verification"
            / "ExactCase.png"
        )
        exact_actual.parent.mkdir(parents=True)
        exact_actual.write_bytes(b"verification")
        exact_alternate = exact_actual.with_name("exactcase.png")
        try:
            same_tree = tree_actual.samefile(tree_alternate)
            same_exact = exact_actual.samefile(exact_alternate)
        except OSError:
            same_tree = False
            same_exact = False
        if not (same_tree and same_exact):
            self.skipTest("Filesystem is case-sensitive.")

        submission = self.make_final_submission(
            final_submission_id="CASE-REF",
            paper_id_filled="CASE",
        )
        submission.thumbnail_folder = str(tree_alternate)
        submission.title_author_verification_image = str(
            exact_alternate
        )
        submission.save(
            update_fields=[
                "thumbnail_folder",
                "title_author_verification_image",
                "updated_at",
            ]
        )

        reference_index = build_storage_reference_index()
        inventory = build_storage_inventory()
        cleanup_paths = {
            Path(row["path"]).resolve()
            for row in inventory["cleanup_candidates"]
        }

        self.assertTrue(reference_index.is_referenced(tree_file))
        self.assertTrue(reference_index.is_referenced(exact_actual))
        self.assertNotIn(tree_file.resolve(), cleanup_paths)
        self.assertNotIn(exact_actual.resolve(), cleanup_paths)

    def test_storage_cleanup_continues_and_audits_unlink_failure(self):
        first = self.media_root / "format_previews" / "a-delete.png"
        second = self.media_root / "format_previews" / "b-keep.png"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        preview = preview_storage_cleanup()
        original_unlink = Path.unlink

        def fail_second(path, *args, **kwargs):
            if Path(path).resolve() == second.resolve():
                raise PermissionError("read only")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_second):
            result = apply_storage_cleanup(
                preview["token"],
                CLEANUP_CONFIRMATION_TEXT,
            )

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("could not be deleted", result["skipped"][0]["message"])
        event = self.latest_audit_event("storage_cleanup_apply")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["result_counts"]["deleted_count"], 1)
        self.assertEqual(event["result_counts"]["skipped_count"], 1)

    def test_storage_cleanup_view_reports_skipped_candidates(self):
        cache_file = (
            self.media_root
            / "format_previews"
            / "changed-before-view-apply.png"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"previewed")
        preview = preview_storage_cleanup()
        cache_file.unlink()
        cache_file.write_bytes(b"replacement")

        response = self.client.post(
            reverse("submissions:settings"),
            {
                "action": "apply_storage_cleanup",
                "cleanup_token": preview["token"],
                "cleanup_confirmation": CLEANUP_CONFIRMATION_TEXT,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(cache_file.exists())
        self.assertContains(response, "Kept 1 candidate file.")
        self.assertContains(response, "Review Audit Log")

    def test_unreadable_storage_root_is_reported_and_blocks_preview(self):
        denied_root = self.media_root / "format_previews"
        denied_root.mkdir(parents=True, exist_ok=True)
        original_scandir = os.scandir

        def deny_target(path):
            if Path(path).resolve() == denied_root.resolve():
                raise PermissionError("permission denied")
            return original_scandir(path)

        with patch(
            "submissions.services.storage_inventory.os.scandir",
            side_effect=deny_target,
        ):
            inventory = build_storage_inventory()
            with self.assertRaisesMessage(
                ValueError,
                "could not be fully scanned",
            ):
                preview_storage_cleanup()
            response = self.client.get(
                reverse("submissions:storage_inventory_panel")
            )

        self.assertEqual(len(inventory["scan_errors"]), 1)
        self.assertEqual(
            inventory["scan_errors"][0]["error"],
            "PermissionError",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Storage scan incomplete")
        self.assertContains(response, "Cleanup preview is disabled")
        event = self.latest_audit_event("storage_cleanup_preview")
        self.assertEqual(event["status"], "failed")

    def test_cleanup_apply_fails_closed_if_scan_becomes_unreadable(self):
        denied_root = self.media_root / "format_previews"
        cache_file = denied_root / "keep-on-incomplete-scan.png"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"keep")
        preview = preview_storage_cleanup()
        original_scandir = os.scandir

        def deny_target(path):
            if Path(path).resolve() == denied_root.resolve():
                raise PermissionError("permission denied")
            return original_scandir(path)

        with patch(
            "submissions.services.storage_inventory.os.scandir",
            side_effect=deny_target,
        ), self.assertRaisesMessage(
            ValueError,
            "could not be fully scanned",
        ):
            apply_storage_cleanup(
                preview["token"],
                CLEANUP_CONFIRMATION_TEXT,
            )

        self.assertTrue(cache_file.exists())
        event = self.latest_audit_event("storage_cleanup_apply")
        self.assertEqual(event["status"], "failed")

    def test_cleanup_housekeeping_failure_does_not_hide_deleted_files(self):
        cache_file = (
            self.media_root
            / "format_previews"
            / "delete-before-housekeeping.png"
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"cache")
        preview = preview_storage_cleanup()

        with patch(
            "submissions.services.storage_inventory."
            "_remove_empty_generated_cache_dirs",
            side_effect=PermissionError("directory is busy"),
        ):
            result = apply_storage_cleanup(
                preview["token"],
                CLEANUP_CONFIRMATION_TEXT,
            )

        self.assertFalse(cache_file.exists())
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["maintenance_warning_count"], 1)
        event = self.latest_audit_event("storage_cleanup_apply")
        self.assertEqual(event["status"], "success")
        self.assertEqual(
            event["result_counts"]["maintenance_warning_count"],
            1,
        )

    def test_storage_cleanup_refreshes_references_during_batch(self):
        first = (
            self.media_root
            / "format_previews"
            / "a-first"
            / "preview.png"
        )
        second = (
            self.media_root
            / "format_previews"
            / "b-second"
            / "preview.png"
        )
        for path in (first, second):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.parent.name.encode("ascii"))
        submission = self.make_final_submission(
            final_submission_id="MID-BATCH",
            paper_id_filled="MID",
        )
        preview = preview_storage_cleanup()
        call_count = 0

        def change_token():
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                submission.thumbnail_folder = str(second.parent)
                submission.save(
                    update_fields=[
                        "thumbnail_folder",
                        "updated_at",
                    ]
                )
                return 2
            return 1

        with patch(
            "submissions.services.storage_inventory._database_change_token",
            side_effect=change_token,
        ):
            result = apply_storage_cleanup(
                preview["token"],
                CLEANUP_CONFIRMATION_TEXT,
            )

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn(
            "now referenced",
            result["skipped"][0]["message"],
        )

    def test_report_cleanup_protects_backup_and_preview_subtrees(self):
        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(self.root / "data")
        settings_obj.save(update_fields=["reports_folder"])
        generated_report = self.root / "data" / "reports" / "generated.zip"
        system_backup = (
            self.root
            / "data"
            / "system_state_backups"
            / "system-state.zip"
        )
        import_workbook = (
            self.root
            / "data"
            / "import_previews"
            / "pending-import.xlsx"
        )
        for path in (generated_report, system_backup, import_workbook):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.name.encode("ascii"))

        with override_settings(BASE_DIR=self.root):
            preview = preview_storage_cleanup(
                "generated_reports_exports"
            )
            selected_paths = {
                Path(row["path"]).resolve()
                for row in preview["files"]
            }
            self.assertIn(generated_report.resolve(), selected_paths)
            self.assertNotIn(system_backup.resolve(), selected_paths)
            self.assertNotIn(import_workbook.resolve(), selected_paths)
            result = apply_storage_cleanup(
                preview["token"],
                CLEANUP_CONFIRMATION_TEXT,
            )

        self.assertEqual(result["deleted_count"], 1)
        self.assertFalse(generated_report.exists())
        self.assertTrue(system_backup.exists())
        self.assertTrue(import_workbook.exists())

    def test_conservative_cleanup_protects_reports_nested_in_cache_root(self):
        settings_obj = AppSetting.load()
        reports_root = (
            self.media_root
            / "format_previews"
            / "nested-reports"
        )
        settings_obj.reports_folder = str(reports_root)
        settings_obj.save(update_fields=["reports_folder"])
        publication_package = reports_root / "publication_package.zip"
        publication_package.parent.mkdir(parents=True, exist_ok=True)
        publication_package.write_bytes(b"publication package")

        inventory = build_storage_inventory()
        cleanup_paths = {
            Path(row["path"]).resolve()
            for row in inventory["cleanup_candidates"]
        }
        preview = preview_storage_cleanup()

        self.assertNotIn(publication_package.resolve(), cleanup_paths)
        self.assertNotIn(
            publication_package.resolve(),
            {
                Path(row["path"]).resolve()
                for row in preview["files"]
            },
        )
        result = apply_storage_cleanup(
            preview["token"],
            CLEANUP_CONFIRMATION_TEXT,
        )
        self.assertEqual(result["deleted_count"], 0)
        self.assertTrue(publication_package.exists())

    def test_settings_defers_storage_inventory_and_grobid_health(self):
        with patch(
            "submissions.controllers.settings.check_grobid_api",
        ) as mocked_grobid, patch(
            "submissions.controllers.settings.build_storage_inventory",
        ) as mocked_inventory:
            response = self.client.get(
                reverse("submissions:settings")
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Title/Author extraction fallback")
        self.assertContains(response, "GROBID API URL")
        self.assertContains(response, reverse("submissions:grobid_health_check"))
        self.assertContains(
            response,
            reverse("submissions:storage_inventory_panel"),
        )
        self.assertContains(response, "data-grobid-health-message")
        self.assertContains(response, "Not checked")
        self.assertContains(response, "Loading storage inventory")
        self.assertContains(response, "runHealthCheck();")
        mocked_grobid.assert_not_called()
        mocked_inventory.assert_not_called()

    def test_settings_and_storage_get_do_not_create_settings_row(self):
        AppSetting.objects.all().delete()

        settings_response = self.client.get(
            reverse("submissions:settings")
        )
        storage_response = self.client.get(
            reverse("submissions:storage_inventory_panel")
        )

        self.assertEqual(settings_response.status_code, 200)
        self.assertEqual(storage_response.status_code, 200)
        self.assertEqual(AppSetting.objects.count(), 0)

    def test_storage_inventory_panel_renders_inventory(self):
        response = self.client.get(
            reverse("submissions:storage_inventory_panel")
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "submissions/storage_inventory.html",
        )
        self.assertContains(response, "Storage Management")
        self.assertContains(response, "Tracked files")
        self.assertContains(response, "Preview conservative cleanup")
        self.assertContains(response, "Back to Settings")

        htmx_response = self.client.get(
            reverse("submissions:storage_inventory_panel"),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(htmx_response.status_code, 200)
        self.assertTemplateUsed(
            htmx_response,
            "submissions/partials/storage_inventory.html",
        )
        self.assertNotContains(htmx_response, "Back to Settings")

    def test_settings_cleanup_preview_is_loaded_by_storage_panel(self):
        response = self.client.post(
            reverse("submissions:settings"),
            {
                "action": "preview_storage_cleanup",
                "cleanup_policy": "generated_cache_or_orphan_output",
            },
        )

        self.assertEqual(response.status_code, 200)
        preview_token = response.context[
            "storage_cleanup_preview_token"
        ]
        self.assertTrue(preview_token)
        self.assertContains(response, f"preview_token={preview_token}")
        self.assertContains(
            response,
            (
                f'href="{reverse("submissions:storage_inventory_panel")}'
                f'?preview_token={preview_token}"'
            ),
        )

        panel_response = self.client.get(
            reverse("submissions:storage_inventory_panel"),
            {"preview_token": preview_token},
        )

        self.assertEqual(panel_response.status_code, 200)
        self.assertContains(panel_response, "Cleanup Preview")
        self.assertContains(panel_response, "No files have been deleted")

    def test_storage_inventory_panel_rejects_invalid_preview_token_cleanly(self):
        response = self.client.get(
            reverse("submissions:storage_inventory_panel"),
            {"preview_token": "x" * 500},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid cleanup preview token")

    def test_creating_cleanup_preview_purges_only_expired_tokens(self):
        active = preview_storage_cleanup()
        expired_token = "a" * 32
        expired_path = (
            self.storage_cleanup_root / f"{expired_token}.json"
        )
        expired_path.write_text(
            json.dumps(
                {
                    "token": expired_token,
                    "policy": "generated_cache_or_orphan_output",
                    "expires_at": (
                        timezone.now() - timedelta(seconds=1)
                    ).isoformat(),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )

        preview_storage_cleanup()

        self.assertFalse(expired_path.exists())
        self.assertTrue(
            (
                self.storage_cleanup_root
                / f"{active['token']}.json"
            ).exists()
        )

    def test_grobid_health_check_endpoint_uses_unsaved_form_url(self):
        with patch(
            "submissions.controllers.settings.check_grobid_api",
            return_value={
                "available": True,
                "level": "success",
                "label": "Available",
                "message": "GROBID API is reachable.",
            },
        ) as mocked_check:
            response = self.client.get(
                reverse("submissions:grobid_health_check"),
                {"api_url": "http://example.test:8070", "timeout": "9"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["label"], "Available")
        mocked_check.assert_called_once_with("http://example.test:8070", 2)

    def test_grobid_health_check_reports_missing_url_without_network(self):
        status = check_grobid_api("")

        self.assertFalse(status["available"])
        self.assertEqual(status["label"], "No URL")

    def test_grobid_health_check_normalizes_raw_true_response(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, _size):
                return b"true"

            def getcode(self):
                return 200

        with patch("submissions.services.grobid_extractor.request.urlopen", return_value=FakeResponse()):
            status = check_grobid_api("http://localhost:8070")

        self.assertTrue(status["available"])
        self.assertEqual(status["label"], "Available")
        self.assertEqual(status["message"], "GROBID API is reachable.")

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
    def setUp(self):
        super().setUp()
        self.base_dir_override = override_settings(BASE_DIR=self.root.resolve())
        self.base_dir_override.enable()
        self.addCleanup(self.base_dir_override.disable)

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
        reviewed_source_hash = submission.source_hash
        self.assertTrue(reviewed_source_hash)
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
        self.assertEqual(restored.source_hash, reviewed_source_hash)
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
        self.assertEqual(
            manifest["state_archive_version"],
            django_settings.STATE_ARCHIVE_VERSION,
        )
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
    def test_reupload_restores_missing_canonical_files_even_when_legacy_paths_match(self):
        self.make_master_paper("P001", "Canonical Recovery", "Ada")
        legacy_pdf = self.make_pdf_file("legacy-recovery.pdf", b"%PDF same bytes")
        legacy_source = self.make_source_file(
            "legacy-recovery.docx",
            b"same source bytes",
        )
        submission = self.make_final_submission(
            final_submission_id="100",
            paper_id_filled="P001",
            final_submission_title="Canonical Recovery",
            pdf_file="",
            source_file="",
            current_file_path=str(legacy_pdf),
            source_current_file_path=str(legacy_source),
        )
        preview = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,"
                "final_submission_title,final_submission_authors\n"
                "100,P001,Canonical Recovery,Ada\n",
            ),
            [
                self.uploaded_file(
                    "100_file_Submit_PDF.pdf",
                    b"%PDF same bytes",
                ),
                self.uploaded_file(
                    "100_file_Submit_Source.docx",
                    b"same source bytes",
                ),
            ],
        )
        row = next(
            item
            for item in preview["rows"]
            if item["identifier"] == "100"
        )

        self.assertEqual(row["file_changes"]["pdf"]["status"], "new")
        self.assertEqual(row["file_changes"]["source"]["status"], "new")
        apply_import_preview(preview["token"])

        submission.refresh_from_db()
        self.assertTrue(submission.pdf_file)
        self.assertTrue(submission.source_file)
        self.assertEqual(
            Path(submission.pdf_file.path).read_bytes(),
            b"%PDF same bytes",
        )
        self.assertEqual(
            Path(submission.source_file.path).read_bytes(),
            b"same source bytes",
        )
        self.assertEqual(
            Path(submission.current_file_path),
            Path(submission.pdf_file.path),
        )
        self.assertEqual(
            Path(submission.source_current_file_path),
            Path(submission.source_file.path),
        )

    def test_final_import_apply_rejects_preview_file_changed_after_review(self):
        self.make_master_paper("P001", "Immutable Import", "Ada")
        preview = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,"
                "final_submission_title,final_submission_authors\n"
                "10,P001,Immutable Import,Ada\n",
            ),
            [
                self.uploaded_file(
                    "10_file_Submit_PDF.pdf",
                    b"%PDF reviewed import",
                )
            ],
        )
        staged_pdf = next(
            (self.preview_root / preview["token"] / "uploads").iterdir()
        )
        staged_pdf.write_bytes(b"%PDF changed after preview")

        with self.assertRaisesMessage(
            ValueError,
            "Import preview file changed after preview",
        ):
            apply_import_preview(preview["token"])

        self.assertFalse(
            FinalSubmission.objects.filter(
                final_submission_id="10"
            ).exists()
        )

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

        prepare_crosscheck_upload("BATCH")
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

    def test_crosscheck_batch_does_not_follow_a_later_active_replacement(self):
        self.make_master_paper("P001", "Crosscheck Bound Version", "Ada")
        old = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Crosscheck Bound Version",
            extracted_title="Crosscheck Bound Version",
            similarity_score=None,
            single_similarity_score=None,
        )
        determine_active_versions()
        prepare_crosscheck_upload("OLD_BATCH")

        replacement = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
            final_submission_title="Crosscheck Bound Version",
            extracted_title="Crosscheck Bound Version",
            similarity_score=None,
            single_similarity_score=None,
        )
        determine_active_versions()

        result = import_crosscheck_results(
            self.uploaded_csv(
                "crosscheck.csv",
                "filename,plagiarism_percent,single_percent\n"
                "P001_OLD_BATCH.pdf,6,2\n",
            )
        )
        report_result = upload_crosscheck_reports(
            [self.uploaded_file("P001_OLD_BATCH.pdf", b"old-version report")]
        )

        old.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["stale"]), 1)
        self.assertEqual(report_result["updated"], 0)
        self.assertEqual(len(report_result["stale"]), 1)
        self.assertIsNone(replacement.similarity_score)
        self.assertIsNone(replacement.single_similarity_score)
        self.assertEqual(replacement.plagiarism_report_path, "")
        self.assert_publication_blocked("Missing Plagiarism Result")

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
        update_formatting_submission(
            submission,
            {
                "corrected_pdf": None,
                "corrected_source": None,
                "format_status": "review_ok",
                "format_notes": "Corrected source reviewed.",
            },
        )

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

    def test_cached_workflow_alerts_never_feed_publication_readiness_or_export(self):
        paper = self.make_master_paper("P001", "Cached Alert Isolation", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Cached Alert Isolation",
            extracted_title="Cached Alert Isolation",
        )
        cache.clear()
        before = self.client.get(reverse("submissions:workflow_alerts"))
        self.assertNotContains(before, "Start2/Editor version decision needed")

        create_editor_submission(
            paper=paper,
            pdf_file=self.uploaded_file("editor.pdf", b"editor pdf"),
            source_file=self.uploaded_file("editor.docx", b"editor source"),
            notes="Cache isolation test.",
        )

        cached = self.client.get(reverse("submissions:workflow_alerts"))
        self.assertNotContains(cached, "Start2/Editor version decision needed")
        self.assertIn(
            "Start2/Editor Version Conflict",
            {row["category"] for row in publication_readiness_rows()},
        )
        self.assert_publication_blocked("Start2/Editor Version Conflict")

        cache.clear()
        refreshed = self.client.get(reverse("submissions:workflow_alerts"))
        self.assertContains(refreshed, "Start2/Editor version decision needed")

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

        cache.clear()
        alerts = self.client.get(reverse("submissions:workflow_alerts"))
        summary = self.client.get(reverse("submissions:dashboard_summary"))
        self.assertContains(alerts, "Process PDFs needed")
        self.assertContains(alerts, "2 active PDFs")
        self.assertContains(summary, "2 need processing")
        self.assertContains(summary, "1 missing PDFs")
        self.assertContains(summary, "PDF, source, and page checks")

        process_page = self.client.get(reverse("submissions:process"))
        self.assertContains(process_page, "Active PDFs not processed")
        self.assertContains(process_page, editor.final_submission_id)
        self.assertContains(process_page, start2_pending.final_submission_id)
        self.assertContains(process_page, 'class="col-lg-6"', count=2)

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
        self.assertContains(response, "Editor Upload Title Safety Check")
        self.assertContains(response, "Uploaded PDF Title")
        self.assertContains(response, "Compared with Paper Master Title")
        self.assertContains(response, "Compared with Final Title")
        self.assertContains(response, "Content differs")
        self.assertContains(response, "Show detailed character diff")
        self.assertContains(response, "Open uploaded PDF")
        self.assertContains(response, "Choose another PDF")
        self.assertContains(response, "Cancel preview")
        self.assertContains(response, "cfm-title-guard-consequence")
        self.assertContains(response, "If you continue")
        self.assertNotContains(response, 'id="id_pdf_file"')
        self.assertEqual(FinalSubmission.objects.filter(submission_origin="editor_upload").count(), 0)

        token = response.context["editor_upload_confirmation"]["token"]
        preview_response = self.client.get(
            reverse("submissions:editor_upload_preview_pdf", args=[token])
        )
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(b"".join(preview_response.streaming_content), b"%PDF mismatch")
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
        self.assertContains(response, "Editor Upload Title Safety Check")
        self.assertContains(response, "Title extraction failed")
        self.assertEqual(FinalSubmission.objects.filter(submission_origin="editor_upload").count(), 0)

    def test_editor_upload_confirmation_rejects_changed_preview_pdf(self):
        paper = self.make_master_paper("P016", "Preview Integrity", "Ada")
        with patch(
            "submissions.services.editor_uploads.get_title_author",
            return_value=("Different Uploaded Title", "Ada", 1),
        ):
            preview = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Preview Integrity",
                    "final_submission_authors": "Ada",
                    "notes": "Changed preview must fail.",
                    "pdf_file": self.uploaded_file(
                        "integrity.pdf",
                        b"%PDF reviewed bytes",
                    ),
                },
            )
        token = preview.context["editor_upload_confirmation"]["token"]
        token_root = self.media_root / "editor_upload_previews" / token
        preview_pdf = next(token_root.glob("editor_pdf-*"))
        preview_pdf.write_bytes(b"%PDF different bytes")

        response = self.client.post(
            reverse("submissions:editor_upload"),
            {
                "action": "confirm_editor_upload",
                "preview_token": token,
            },
        )

        self.assertContains(
            response,
            "preview file changed after title review",
        )
        self.assertFalse(
            FinalSubmission.objects.filter(
                submission_origin="editor_upload"
            ).exists()
        )
        self.assertFalse(token_root.exists())

    def test_editor_upload_confirmation_rejects_changed_paper_master(self):
        paper = self.make_master_paper("P017", "Original Master", "Ada")
        with patch(
            "submissions.services.editor_uploads.get_title_author",
            return_value=("Different Uploaded Title", "Ada", 1),
        ):
            preview = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Original Master",
                    "final_submission_authors": "Ada",
                    "notes": "Stale Master must fail.",
                    "pdf_file": self.uploaded_file(
                        "stale-master.pdf",
                        b"%PDF stale master",
                    ),
                },
            )
        token = preview.context["editor_upload_confirmation"]["token"]
        paper.title = "New Master Title"
        paper.save(update_fields=["title"])

        response = self.client.post(
            reverse("submissions:editor_upload"),
            {
                "action": "confirm_editor_upload",
                "preview_token": token,
            },
        )

        self.assertContains(
            response,
            "Paper Master changed after the Editor Upload preview",
        )
        self.assertFalse(
            FinalSubmission.objects.filter(
                submission_origin="editor_upload"
            ).exists()
        )

    def test_editor_upload_title_guard_deduplicates_references_and_cleans_canceled_preview(self):
        paper = self.make_master_paper("P015", "Same Reference Title", "Ada")

        with patch(
            "submissions.services.editor_uploads.get_title_author",
            return_value=("Different Uploaded Title", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Same Reference Title",
                    "final_submission_authors": "Ada",
                    "notes": "Test title guard.",
                    "pdf_file": self.uploaded_file("different.pdf", b"%PDF different"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compared with Paper Master Title = Final Title")
        self.assertContains(
            response,
            'class="cfm-title-guard-comparison cfm-title-guard-warning"',
            count=1,
        )
        token = response.context["editor_upload_confirmation"]["token"]

        replace_response = self.client.post(
            reverse("submissions:editor_upload"),
            {"action": "replace_editor_upload_pdf", "preview_token": token},
        )
        self.assertEqual(replace_response.status_code, 200)
        self.assertContains(replace_response, "Choose the replacement PDF")
        self.assertContains(replace_response, 'id="id_pdf_file"')
        self.assertEqual(FinalSubmission.objects.filter(submission_origin="editor_upload").count(), 0)
        self.assertEqual(
            self.client.get(
                reverse("submissions:editor_upload_preview_pdf", args=[token])
            ).status_code,
            404,
        )

        with patch(
            "submissions.services.editor_uploads.get_title_author",
            return_value=("Different Uploaded Title", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:editor_upload"),
                {
                    "paper": paper.pk,
                    "final_submission_title": "Same Reference Title",
                    "final_submission_authors": "Ada",
                    "notes": "Cancel this preview.",
                    "pdf_file": self.uploaded_file("cancel.pdf", b"%PDF cancel"),
                },
            )
        token = response.context["editor_upload_confirmation"]["token"]
        cancel_response = self.client.post(
            reverse("submissions:editor_upload"),
            {"action": "cancel_editor_upload", "preview_token": token},
        )
        self.assertRedirects(cancel_response, reverse("submissions:final_submission_list"))
        self.assertEqual(FinalSubmission.objects.filter(submission_origin="editor_upload").count(), 0)
        self.assertEqual(
            self.client.get(
                reverse("submissions:editor_upload_preview_pdf", args=[token])
            ).status_code,
            404,
        )

    def test_title_guard_diff_is_quote_safe_and_uses_word_first_detail(self):
        diff = str(text_diff_html('Reference "R"', 'Uploaded "U"'))
        self.assertIn("&quot;", diff)
        self.assertNotIn('title="Replaces "', diff)

        guard = build_title_guard_context(
            extracted_title="Uploaded PDF Title",
            references=[
                {"label": "Paper Master Title", "title": "Reference Title"},
                {"label": "Final Title", "title": "Reference Title"},
            ],
        )
        self.assertEqual(len(guard["comparisons"]), 1)
        self.assertEqual(
            guard["comparisons"][0]["label"],
            "Paper Master Title = Final Title",
        )
        self.assertTrue(guard["comparisons"][0]["word_diff_html"])
        self.assertTrue(guard["comparisons"][0]["character_diff_html"])

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
        organized = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        self.assertNotContains(organized, "Ready Paper")
        self.assertContains(organized, "Publishing Paper")
        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertIn("PDF/P002-Publishing Paper.pdf", archive.namelist())
            self.assertNotIn("PDF/P001-Ready Paper.pdf", archive.namelist())

        undo_not_publishing(excluded)
        self.assert_publication_blocked("Unverified Paper ID")

    def test_not_publishing_latest_replacement_does_not_resurrect_old_version_or_show_no_final(self):
        self.make_master_paper("P001", "Withdrawn Replacement", "Ada")
        self.make_master_paper("P002", "Publishing Paper", "Grace")
        old = self.make_final_submission(
            final_submission_id="100",
            paper_id_filled="P001",
            final_submission_title="Withdrawn Replacement",
            extracted_title="Withdrawn Replacement",
        )
        latest = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Withdrawn Replacement",
            extracted_title="Withdrawn Replacement",
        )
        included = self.make_final_submission(
            final_submission_id="200",
            paper_id_filled="P002",
            final_submission_title="Publishing Paper",
            extracted_title="Publishing Paper",
            final_submission_authors="Grace Hopper",
            extracted_authors="Grace Hopper",
        )
        determine_active_versions()
        _mark_duplicate_submissions()
        mark_not_publishing(latest, "withdrawn", "Latest replacement withdrawn.")

        old.refresh_from_db()
        latest.refresh_from_db()
        self.assertFalse(old.active_version)
        self.assertTrue(latest.active_version)
        self.assertTrue(latest.excluded_from_publication)
        self.assertNotIn(
            "Missing Final Submission",
            {row["category"] for row in publication_readiness_rows()},
        )

        organized = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        self.assertNotContains(organized, "Withdrawn Replacement")
        self.assertNotContains(organized, "No final")
        self.assertContains(organized, "Publishing Paper")

        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            self.assertIn("PDF/P002-Publishing Paper.pdf", names)
            self.assertNotIn("PDF/P001-Withdrawn Replacement.pdf", names)
            manifest_name = next(name for name in names if name.startswith("publication_manifest_"))
            manifest = archive.read(manifest_name).decode("utf-8-sig")
            self.assertIn(included.paper_id_filled, manifest)
            self.assertNotIn(latest.paper_id_filled, manifest)

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

    def test_cached_hash_never_hides_a_changed_publication_pdf(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
        )
        self.assertEqual(publication_readiness_rows(), [])
        clear_file_hash_cache()
        self.assertEqual(publication_readiness_rows(), [])

        pdf_path = Path(publication_pdf_info(submission)["path"])
        original = pdf_path.read_bytes()
        previous_stat = pdf_path.stat()
        changed = bytes([original[0] ^ 1]) + original[1:]
        pdf_path.write_bytes(changed)
        os.utime(
            pdf_path,
            ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
        )

        categories = {
            row["category"] for row in publication_readiness_rows()
        }
        self.assertIn("PDF Not Processed", categories)
        self.assert_publication_blocked("PDF Not Processed")

    def test_source_changed_after_format_review_blocks_publication(self):
        self.make_master_paper("P001", "Source Integrity", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Source Integrity",
            extracted_title="Source Integrity",
        )
        self.assertEqual(publication_readiness_rows(), [])
        source_path = Path(publication_source_info(submission)["path"])
        original_stat = source_path.stat()
        original = source_path.read_bytes()
        changed = bytes([original[0] ^ 1]) + original[1:]
        source_path.write_bytes(changed)
        os.utime(
            source_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertIn("Source Changed After Review", categories)
        organized_rows, summary, _settings, _filter, _sort = organized_list_rows()
        source_row = next(
            row for row in organized_rows
            if row["submission"].pk == submission.pk
        )
        self.assertEqual(source_row["source_label"], "Changed after review")
        self.assertEqual(source_row["source_level"], "danger")
        self.assertEqual(summary["source_issues"], 1)
        self.assert_publication_blocked("Source Changed After Review")

    def test_source_without_review_hash_blocks_publication(self):
        self.make_master_paper("P001", "Source Review Binding", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Source Review Binding",
            extracted_title="Source Review Binding",
            source_hash="",
        )

        self.assert_publication_blocked("Source Review Hash Missing")
        update_formatting_submission(
            submission,
            {
                "corrected_pdf": None,
                "corrected_source": None,
                "format_status": "review_ok",
                "format_notes": "Current source reviewed.",
            },
        )
        submission.refresh_from_db()

        source_path = Path(publication_source_info(submission)["path"])
        self.assertEqual(
            submission.source_hash,
            hashlib.sha256(source_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(publication_readiness_rows(), [])

    def test_pending_format_review_does_not_report_missing_source_review_hash(self):
        self.make_master_paper("P001", "Pending Source Review", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Pending Source Review",
            extracted_title="Pending Source Review",
            format_status="pending",
            source_hash="",
        )

        categories = {row["category"] for row in publication_readiness_rows()}

        self.assertIn("Formatting Not Review OK", categories)
        self.assertNotIn("Source Review Hash Missing", categories)
        self.assertNotIn("Source Changed After Review", categories)
        self.assert_publication_blocked("Formatting Not Review OK")

    def test_missing_corrected_files_never_fall_back_to_original_files(self):
        self.make_master_paper("P001", "No Silent Fallback", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="No Silent Fallback",
            extracted_title="No Silent Fallback",
        )
        submission.formatted_pdf_file.save(
            "corrected.pdf",
            ContentFile(b"corrected pdf"),
            save=False,
        )
        submission.formatted_source_file.save(
            "corrected.docx",
            ContentFile(b"corrected source"),
            save=False,
        )
        submission.save()
        Path(submission.formatted_pdf_file.path).unlink()
        Path(submission.formatted_source_file.path).unlink()

        pdf_info = publication_pdf_info(submission)
        source_info = publication_source_info(submission)
        self.assertFalse(pdf_info["exists"])
        self.assertEqual(pdf_info["source"], "corrected_missing")
        self.assertFalse(source_info["exists"])
        self.assertEqual(source_info["source"], "corrected_missing")
        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertIn("Missing Corrected PDF", categories)
        self.assertIn("Missing Corrected Source", categories)
        self.assert_publication_blocked("Missing Corrected PDF")

    def test_process_pdfs_resets_reviews_when_processed_pdf_was_replaced_externally(self):
        import fitz

        settings_obj = AppSetting.load()
        settings_obj.page_minimum = 1
        settings_obj.save(update_fields=["page_minimum"])
        original_path = self.root / "original-valid.pdf"
        original_document = fitz.open()
        original_page = original_document.new_page()
        original_page.insert_text((72, 72), "Original reviewed publication")
        original_document.save(original_path)
        original_document.close()
        self.make_master_paper("P001", "Ready Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            current_file_path=str(original_path),
            page_count=1,
        )
        self.assertEqual(publication_readiness_rows(), [])

        replacement_path = self.root / "replacement-valid.pdf"
        replacement_document = fitz.open()
        replacement_page = replacement_document.new_page()
        replacement_page.insert_text((72, 72), "Different unreviewed publication")
        replacement_document.save(replacement_path)
        replacement_document.close()
        publication_path = Path(publication_pdf_info(submission)["path"])
        publication_path.write_bytes(replacement_path.read_bytes())

        self.assert_publication_blocked("PDF Not Processed")
        result = process_all_pdfs()
        submission.refresh_from_db()

        self.assertEqual(result["integrity_resets"], 1)
        self.assertEqual(submission.processing_status, "processed")
        self.assertEqual(submission.page_count, 1)
        self.assertEqual(submission.pdf_hash, calculate_pdf_hash(publication_path))
        self.assertEqual(submission.extracted_title, "")
        self.assertEqual(submission.extracted_authors, "")
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertFalse(submission.title_author_verified)
        self.assertFalse(submission.extracted_title_verified)
        self.assertEqual(submission.format_status, "pending")
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)
        self.assertFalse(PaperAuthor.objects.filter(final_submission=submission).exists())
        categories = {row["category"] for row in publication_readiness_rows()}
        self.assertIn("Missing Extracted Title", categories)
        self.assertIn("Formatting Not Review OK", categories)
        self.assertIn("Missing Plagiarism Result", categories)
        self.assert_publication_blocked("Missing Extracted Title")

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
        with self.assertRaisesMessage(
            ValueError,
            "unresolved version or filename ambiguity",
        ):
            export_publication_package(force=True)

    def test_publishable_and_not_publishing_active_finals_still_block_and_remain_visible(self):
        self.make_master_paper("P001", "Ready Paper", "Ada")
        included = self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
        )
        excluded = self.make_final_submission(
            final_submission_id="11",
            paper_id_filled="P001",
        )
        excluded.excluded_from_publication = True
        excluded.save(
            update_fields=["excluded_from_publication", "updated_at"]
        )

        rows = publication_readiness_rows()
        self.assertIn(
            "Multiple Active Final Submissions",
            {row["category"] for row in rows},
        )
        self.assertIn(
            "Mixed Not Publishing Decision",
            {row["category"] for row in rows},
        )
        organized_rows, _summary, _settings, _filter, _sort = organized_list_rows()
        self.assertEqual(len(organized_rows), 1)
        self.assertIsNone(organized_rows[0]["submission"])
        self.assertEqual(
            set(organized_rows[0]["multiple_active_final_ids"]),
            {included.final_submission_id, excluded.final_submission_id},
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

    def test_plagiarism_percent_exception_allows_publication_until_score_changes(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(similarity_score=42, single_similarity_score=4)
        self.assert_publication_blocked("Plagiarism % Over Threshold")

        rows, _ = exception_rows("all")
        row = next(item for item in rows if item["type"] == "plagiarism_percent")
        approve_exception(row, "Chair approved similarity caused by required template text.")

        submission.refresh_from_db()
        self.assertTrue(submission.plagiarism_percent_exception_approved)
        self.assertEqual(submission.plagiarism_percent_exception_approved_score, 42)
        blocking_categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Plagiarism % Over Threshold", blocking_categories)
        self.assertIn("Allowed Plagiarism % Exception", {row["category"] for row in error_report_rows()})
        self.assertTrue(Path(export_publication_package()).exists())

        submission.similarity_score = 43
        submission.save(update_fields=["similarity_score", "updated_at"])
        self.assert_publication_blocked("Stale Plagiarism % Exception")

        rows, _ = exception_rows("all")
        stale_row = next(item for item in rows if item["type"] == "plagiarism_percent")
        self.assertEqual(stale_row["status"], "stale")
        approve_exception(stale_row, "Chair re-approved updated similarity score.")
        submission.refresh_from_db()
        self.assertEqual(submission.plagiarism_percent_exception_approved_score, 43)
        self.assertTrue(Path(export_publication_package()).exists())

    def test_single_percent_exception_allows_publication_until_score_changes(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(similarity_score=4, single_similarity_score=12)
        self.assert_publication_blocked("Single % Over Threshold")

        rows, _ = exception_rows("all")
        row = next(item for item in rows if item["type"] == "single_percent")
        approve_exception(row, "Chair approved single-source overlap with prior proceedings.")

        submission.refresh_from_db()
        self.assertTrue(submission.single_percent_exception_approved)
        self.assertEqual(submission.single_percent_exception_approved_score, 12)
        blocking_categories = {row["category"] for row in publication_readiness_rows()}
        self.assertNotIn("Single % Over Threshold", blocking_categories)
        self.assertIn("Allowed Single % Exception", {row["category"] for row in error_report_rows()})
        self.assertTrue(Path(export_publication_package()).exists())

        submission.single_similarity_score = 13
        submission.save(update_fields=["single_similarity_score", "updated_at"])
        self.assert_publication_blocked("Stale Single % Exception")

    def test_new_pdf_resets_plagiarism_exceptions_when_scores_are_cleared(self):
        self.make_master_paper("P001")
        submission = self.make_final_submission(similarity_score=42, single_similarity_score=12)
        rows, _ = exception_rows("all")
        approve_exception(
            next(row for row in rows if row["type"] == "plagiarism_percent"),
            "Chair approved P score.",
        )
        rows, _ = exception_rows("all")
        approve_exception(
            next(row for row in rows if row["type"] == "single_percent"),
            "Chair approved S score.",
        )
        submission.refresh_from_db()
        self.assertTrue(submission.plagiarism_percent_exception_approved)
        self.assertTrue(submission.single_percent_exception_approved)

        token_payload = preview_final_import(
            self.uploaded_csv(
                "final.csv",
                "final_submission_id,author_entered_paper_id,final_submission_title,final_submission_authors,upload_date\n"
                "100,P001,Ready Paper,Ada,2026-05-07 09:00:00\n",
            ),
            [self.uploaded_file("100_file_Submit_PDF.pdf", b"new pdf bytes")],
        )
        apply_import_preview(token_payload["token"])

        submission.refresh_from_db()
        self.assertIsNone(submission.similarity_score)
        self.assertIsNone(submission.single_similarity_score)
        self.assertFalse(submission.plagiarism_percent_exception_approved)
        self.assertFalse(submission.single_percent_exception_approved)
        self.assertEqual(submission.plagiarism_percent_exception_reason, "")
        self.assertEqual(submission.single_percent_exception_reason, "")
        self.assert_publication_blocked("PDF Not Processed")

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

    def test_error_report_loads_full_duplicate_group_details_on_demand(self):
        for paper_id, final_id in [("P001", "1"), ("P002", "2")]:
            self.make_master_paper(paper_id, "Duplicate Title", "Ada")
            self.make_final_submission(
                final_submission_id=final_id,
                paper_id_filled=paper_id,
                final_submission_title="Duplicate Title",
                extracted_title="Duplicate Title",
            )

        report = self.client.get(reverse("submissions:error_report"))
        self.assertContains(report, "Show 1 matching record")

        detail = self.client.get(
            reverse("submissions:publication_duplicate_details"),
            {
                "kind": "title",
                "key": "duplicate title",
                "submission_id": FinalSubmission.objects.get(
                    final_submission_id="1"
                ).pk,
            },
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(
            detail,
            "Duplicate title with P002 / Final 2. Key: duplicate ti.",
        )
        self.assertContains(detail, "Back to Error Report")

        partial = self.client.get(
            reverse("submissions:publication_duplicate_details"),
            {
                "kind": "title",
                "key": "duplicate title",
                "submission_id": FinalSubmission.objects.get(
                    final_submission_id="1"
                ).pk,
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(
            partial,
            "Duplicate title with P002 / Final 2. Key: duplicate ti.",
        )
        self.assertNotContains(partial, "Back to Error Report")

    def test_error_report_filters_severity_before_pagination(self):
        rows = [
            {
                "category": "Unverified Paper ID",
                "paper_id": f"P{index:03d}",
                "final_submission_id": str(index),
                "message": "Critical issue.",
            }
            for index in range(101)
        ]
        rows.extend(
            {
                "category": "Formatting Not Review OK",
                "paper_id": f"M{index:03d}",
                "final_submission_id": str(index),
                "message": "Medium issue.",
            }
            for index in range(236)
        )
        rows.append(
            {
                "category": "Replaced Final Submission",
                "paper_id": "I001",
                "final_submission_id": "1",
                "message": "Informational issue.",
            }
        )
        _annotate_error_rows(rows)

        with patch(
            "submissions.controllers.exports.error_report_rows",
            return_value=rows,
        ):
            medium_first = self.client.get(
                reverse("submissions:error_report"),
                {"severity": "medium", "page": 1, "page_size": 25},
            )
            medium_second = self.client.get(
                reverse("submissions:error_report"),
                {"severity": "medium", "page": 2, "page_size": 25},
            )
            all_first = self.client.get(
                reverse("submissions:error_report"),
                {"severity": "all", "page": 1, "page_size": 25},
            )

        self.assertTrue(
            all(
                row["severity"] == "medium"
                for row in medium_first.context["rows"]
            )
        )
        self.assertEqual(len(medium_first.context["rows"]), 25)
        self.assertEqual(medium_first.context["pagination"].total_count, 236)
        self.assertEqual(medium_first.context["pagination"].start_index, 1)
        self.assertEqual(medium_first.context["pagination"].end_index, 25)
        self.assertTrue(
            all(
                row["severity"] == "medium"
                for row in medium_second.context["rows"]
            )
        )
        self.assertEqual(medium_second.context["pagination"].start_index, 26)
        self.assertTrue(
            all(row["severity"] == "critical" for row in all_first.context["rows"])
        )
        total_counts = {
            option["value"]: option["count"]
            for option in medium_first.context["severity_filter_options"]
        }
        self.assertEqual(
            total_counts,
            {"all": 338, "critical": 101, "medium": 236, "info": 1},
        )
        self.assertEqual(medium_first.context["current_severity"], "medium")
        self.assertContains(
            medium_first,
            'aria-label="Top worklist pagination"',
            count=1,
        )
        self.assertContains(
            medium_first,
            'aria-label="Bottom worklist pagination"',
            count=1,
        )

    def test_error_report_area_and_severity_filters_compose(self):
        rows = [
            {
                "category": "Missing PDF",
                "paper_id": "P001",
                "final_submission_id": "1",
                "message": "Critical file issue.",
            },
            {
                "category": "Allowed Page Exception",
                "paper_id": "P002",
                "final_submission_id": "2",
                "message": "Allowed file issue.",
            },
            {
                "category": "Formatting Not Review OK",
                "paper_id": "P003",
                "final_submission_id": "3",
                "message": "Unrelated formatting issue.",
            },
        ]
        _annotate_error_rows(rows)

        with patch(
            "submissions.controllers.exports.error_report_rows",
            return_value=rows,
        ):
            response = self.client.get(
                reverse("submissions:error_report"),
                {
                    "area": "files",
                    "severity": "info",
                    "page_size": 25,
                },
            )

        self.assertEqual(response.context["current_area"], "files")
        self.assertEqual(response.context["current_severity"], "info")
        self.assertEqual(
            [row["paper_id"] for row in response.context["rows"]],
            ["P002"],
        )
        self.assertNotContains(response, "Unrelated formatting issue.")
        info_option = next(
            option
            for option in response.context["severity_filter_options"]
            if option["value"] == "info"
        )
        self.assertIn("area=files", info_option["url"])
        self.assertIn("severity=info", info_option["url"])
        self.assertIn("page_size=25", info_option["url"])

    def test_error_report_invalid_severity_falls_back_to_all(self):
        rows = [
            {
                "category": "Unverified Paper ID",
                "paper_id": "P001",
                "final_submission_id": "1",
                "message": "Critical issue.",
            },
            {
                "category": "Formatting Not Review OK",
                "paper_id": "P002",
                "final_submission_id": "2",
                "message": "Medium issue.",
            },
        ]
        _annotate_error_rows(rows)

        with patch(
            "submissions.controllers.exports.error_report_rows",
            return_value=rows,
        ):
            response = self.client.get(
                reverse("submissions:error_report"),
                {"severity": "not-a-severity"},
            )

        self.assertEqual(response.context["current_severity"], "all")
        self.assertEqual(response.context["pagination"].total_count, 2)
        self.assertEqual(
            [row["severity"] for row in response.context["rows"]],
            ["critical", "medium"],
        )

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

    def test_unicode_equivalent_titles_block_as_publication_duplicates(self):
        title_pairs = [
            ("深度學習", "深度學習"),
            ("Μάθηση", "Μάθηση"),
            ("Café Systems", "Cafe\u0301 Systems"),
        ]
        for case_index, (first_title, second_title) in enumerate(
            title_pairs,
            start=1,
        ):
            with self.subTest(first_title=first_title):
                InitialPaper.objects.all().delete()
                FinalSubmission.objects.all().delete()
                for paper_id, final_id, title in [
                    (f"U{case_index}A", f"{case_index}1", first_title),
                    (f"U{case_index}B", f"{case_index}2", second_title),
                ]:
                    self.make_master_paper(paper_id, title, "Ada")
                    self.make_final_submission(
                        final_submission_id=final_id,
                        paper_id_filled=paper_id,
                        start2_paper_id_raw=paper_id,
                        final_submission_title=title,
                        extracted_title=title,
                    )

                categories = {
                    row["category"]
                    for row in publication_readiness_rows()
                }
                self.assertIn("Duplicate Publication Title", categories)
                self.assert_publication_blocked("Duplicate Publication Title")

    def test_sanitized_publication_filename_collision_blocks_export_and_marks_organized_list(self):
        settings_obj = AppSetting.load()
        shared_prefix = " ".join(
            f"Shared{index}"
            for index in range(1, settings_obj.title_words_for_filename + 1)
        )
        for paper_id, final_id, suffix in [
            ("P/1", "1", "Alpha"),
            ("P:1", "2", "Beta"),
        ]:
            title = f"{shared_prefix} {suffix}"
            self.make_master_paper(paper_id, title, "Ada")
            self.make_final_submission(
                final_submission_id=final_id,
                paper_id_filled=paper_id,
                start2_paper_id_raw=paper_id,
                final_submission_title=title,
                extracted_title=title,
            )

        rows = publication_readiness_rows()
        self.assertEqual(
            sum(row["category"] == "Duplicate Publication Filename" for row in rows),
            2,
        )
        duplicate_map = publication_duplicate_map()
        self.assertEqual(len(duplicate_map), 2)
        self.assertTrue(
            all(
                "Duplicate publication filename" in labels
                for labels in duplicate_map.values()
            )
        )
        organized_rows, _summary, _settings, _filter, _sort = organized_list_rows()
        self.assertTrue(
            all(
                "Duplicate publication filename" in row["duplicate_badges"]
                for row in organized_rows
            )
        )
        self.assert_publication_blocked("Duplicate Publication Filename")
        with self.assertRaisesMessage(
            ValueError,
            "unresolved version or filename ambiguity",
        ):
            export_publication_package(force=True)


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

    def test_package_rejects_file_changed_after_readiness_and_removes_partial_outputs(self):
        self.make_master_paper("P001", "Snapshot Safe Publication", "Ada")
        submission = self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Snapshot Safe Publication",
            extracted_title="Snapshot Safe Publication",
        )
        pdf_path = Path(publication_pdf_info(submission)["path"])
        original_read_snapshot_bytes = FileInspectionContext.read_snapshot_bytes
        mutated = False

        def change_pdf_before_snapshot(context, path):
            nonlocal mutated
            if not mutated and Path(path) == pdf_path:
                mutated = True
                pdf_path.write_bytes(pdf_path.read_bytes() + b" changed")
            return original_read_snapshot_bytes(context, path)

        reports_folder = Path(AppSetting.load().reports_folder)
        before_outputs = set(reports_folder.glob("publication_*"))
        with patch.object(
            FileInspectionContext,
            "read_snapshot_bytes",
            new=change_pdf_before_snapshot,
        ):
            with self.assertRaises(FileChangedDuringInspection):
                export_publication_package()

        self.assertTrue(mutated)
        self.assertEqual(set(reports_folder.glob("publication_*")), before_outputs)
        event = self.latest_audit_event("publication_package_export")
        self.assertEqual(event["status"], "failed")

    def test_package_rejects_database_state_changed_after_readiness(self):
        self.make_master_paper("P001", "Concurrent Editorial Change", "Ada")
        submission = self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Concurrent Editorial Change",
            extracted_title="Concurrent Editorial Change",
        )
        original_read_snapshot_bytes = FileInspectionContext.read_snapshot_bytes
        changed = False

        def change_state_before_snapshot(context, path):
            nonlocal changed
            if not changed:
                changed = True
                FinalSubmission.objects.filter(pk=submission.pk).update(
                    title_author_review_status="pending",
                    title_author_verified=False,
                    updated_at=timezone.now(),
                )
            return original_read_snapshot_bytes(context, path)

        reports_folder = Path(AppSetting.load().reports_folder)
        before_outputs = set(reports_folder.glob("publication_*"))
        with patch.object(
            FileInspectionContext,
            "read_snapshot_bytes",
            new=change_state_before_snapshot,
        ):
            with self.assertRaisesMessage(
                RuntimeError,
                "Publication workflow state changed during export",
            ):
                export_publication_package()

        self.assertTrue(changed)
        self.assertEqual(set(reports_folder.glob("publication_*")), before_outputs)
        submission.refresh_from_db()
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assert_publication_blocked("Unverified Title/Author Extraction")
        event = self.latest_audit_event("publication_package_export")
        self.assertEqual(event["status"], "blocked")

    def test_strict_context_retries_if_settings_change_during_snapshot_load(self):
        settings_obj = AppSetting.load()
        self.assertNotEqual(settings_obj.page_limit, 7)
        original_signature = publication_read.publication_database_signature
        signature_calls = 0

        def change_settings_before_first_post_load_signature():
            nonlocal signature_calls
            signature_calls += 1
            if signature_calls == 2:
                AppSetting.objects.filter(pk=1).update(page_limit=7)
            return original_signature()

        with patch.object(
            publication_read,
            "publication_database_signature",
            side_effect=change_settings_before_first_post_load_signature,
        ):
            context = PublicationReadContext.load(
                require_stable_database=True
            )

        self.assertGreaterEqual(signature_calls, 4)
        self.assertEqual(context.settings.page_limit, 7)
        context.assert_database_unchanged()

    def test_formatting_preview_cache_key_uses_full_file_signature(self):
        self.make_master_paper("P001", "Preview Integrity", "Ada")
        submission = self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Preview Integrity",
            extracted_title="Preview Integrity",
        )
        pdf_path = Path(publication_pdf_info(submission)["path"])

        def write_preview(source, target):
            Path(target).write_bytes(Path(source).read_bytes())

        with patch(
            "submissions.services.formatting._render_first_page_upper_half",
            side_effect=write_preview,
        ):
            first = formatting_preview_info(submission)
            previous_stat = pdf_path.stat()
            original = pdf_path.read_bytes()
            changed_bytes = bytes([original[0] ^ 1]) + original[1:]
            pdf_path.write_bytes(changed_bytes)
            os.utime(
                pdf_path,
                ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
            )
            second = formatting_preview_info(submission)

        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(Path(first["path"]).read_bytes(), original)
        self.assertEqual(Path(second["path"]).read_bytes(), changed_bytes)

    def test_paginated_ui_gets_do_not_change_publication_scope_state_or_package_bytes(self):
        for index in range(1, 106):
            paper_id = f"P{index:03d}"
            title = f"Pagination Safe Paper {index:03d}"
            authors = f"Author {index:03d}"
            self.make_master_paper(paper_id, title, authors)
            self.make_final_submission(
                final_submission_id=str(index),
                paper_id_filled=paper_id,
                start2_paper_id_raw=paper_id,
                final_submission_title=title,
                final_submission_authors=authors,
                extracted_title=title,
                extracted_authors=authors,
            )

        before_state = list(
            FinalSubmission.objects.order_by("pk").values()
        )
        before_active = list(
            FinalSubmission.objects.filter(active_version=True)
            .order_by("paper_id_filled")
            .values_list("pk", flat=True)
        )
        before_readiness = publication_readiness_rows()
        self.assertEqual(before_readiness, [])

        def zip_snapshot(payload):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                return {
                    name: hashlib.sha256(archive.read(name)).hexdigest()
                    for name in sorted(archive.namelist())
                }

        with patch(
            "submissions.services.reports._timestamp",
            return_value="20260718_120000",
        ):
            before_package = Path(export_publication_package()).read_bytes()
        audit_count_before_gets = len(read_audit_log(limit=1000))

        cache.clear()
        ui_requests = [
            (reverse("submissions:initial_paper_list"), {"page": 2, "page_size": 50}),
            (reverse("submissions:final_submission_list"), {"page": 2, "page_size": 50}),
            (reverse("submissions:organized_list"), {"page": 2, "page_size": 50}),
            (reverse("submissions:process"), {"filter": "all", "page": 2, "page_size": 50}),
            (reverse("submissions:verify_paper_ids"), {"filter": "all", "page": 2, "page_size": 50}),
            (
                reverse("submissions:title_author_extraction"),
                {"filter": "all", "page": 2, "page_size": 50},
            ),
            (reverse("submissions:formatting"), {"filter": "all", "page": 2, "page_size": 50}),
            (reverse("submissions:exceptions_center"), {"page": 2, "page_size": 50}),
            (reverse("submissions:error_report"), {"page": 2, "page_size": 50}),
            (reverse("submissions:author_count"), {"page": 2, "page_size": 50}),
            (reverse("submissions:old_versions"), {"page": 2, "page_size": 50}),
            (reverse("submissions:dashboard"), {}),
            (reverse("submissions:dashboard_summary"), {}),
            (reverse("submissions:workflow_alerts"), {}),
            (reverse("submissions:settings"), {}),
            (reverse("submissions:storage_inventory_panel"), {}),
        ]
        for url, params in ui_requests:
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, url)

        self.assertEqual(len(read_audit_log(limit=1000)), audit_count_before_gets)
        self.assertEqual(
            list(FinalSubmission.objects.order_by("pk").values()),
            before_state,
        )
        self.assertEqual(
            list(
                FinalSubmission.objects.filter(active_version=True)
                .order_by("paper_id_filled")
                .values_list("pk", flat=True)
            ),
            before_active,
        )
        self.assertEqual(publication_readiness_rows(), before_readiness)

        with patch(
            "submissions.services.reports._timestamp",
            return_value="20260718_120000",
        ):
            after_package = Path(export_publication_package()).read_bytes()
        self.assertEqual(zip_snapshot(after_package), zip_snapshot(before_package))

    def test_gzip_middleware_bypasses_downloadable_publication_zip(self):
        self.make_master_paper("P001", "GZip Safe Publication", "Ada")
        self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="GZip Safe Publication",
            extracted_title="GZip Safe Publication",
            final_submission_authors="Ada Lovelace",
            extracted_authors="Ada Lovelace",
        )

        response = self.client.post(
            reverse("submissions:export_reports"),
            {"action": "publication_package"},
            HTTP_ACCEPT_ENCODING="gzip",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Content-Encoding", response)
        self.assertIn("Content-Length", response)
        self.assertIn("publication_package_", response["Content-Disposition"])
        payload = b"".join(response.streaming_content)
        self.assertEqual(int(response["Content-Length"]), len(payload))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn("PDF/P001-GZip Safe Publication.pdf", archive.namelist())
            self.assertIn("Source/P001-GZip Safe Publication.docx", archive.namelist())


class ViewWorkflowSmokeTests(EditorialAcceptanceTestCase):
    def open_formatting_review(
        self,
        submission,
        *,
        mode="list",
        status_filter="all",
        query="",
    ):
        params = {"filter": status_filter}
        if query:
            params["q"] = query
        if mode == "single":
            params.update({"mode": "single", "current": submission.pk})
            response = self.client.get(reverse("submissions:formatting"), params)
            self.assertEqual(response.status_code, 302)
            return self.client.get(response["Location"])
        if mode == "focus":
            params = {"mode": "focus", "submission": submission.pk}
        return self.client.get(reverse("submissions:formatting"), params)

    def formatting_post_data(self, response, submission, **overrides):
        row = next(
            row
            for row in response.context["rows"]
            if row["submission"].pk == submission.pk
        )
        navigation = response.context.get("single_navigation") or {}
        data = {
            "submission_id": submission.pk,
            "mode": response.context.get("mode", "list"),
            "filter": response.context.get("current_filter", "all"),
            "q": response.context.get("q", ""),
            "queue": navigation.get("token", ""),
            "review_snapshot": row["review_snapshot"],
            "format_status": submission.format_status,
            "format_notes": submission.format_notes,
        }
        data.update(overrides)
        return data

    def test_gunicorn_deployment_collects_and_serves_local_static_assets(self):
        self.assertIn(
            "whitenoise.middleware.WhiteNoiseMiddleware",
            django_settings.MIDDLEWARE,
        )
        self.assertEqual(
            django_settings.MIDDLEWARE.index(
                "whitenoise.middleware.WhiteNoiseMiddleware"
            ),
            django_settings.MIDDLEWARE.index(
                "django.middleware.security.SecurityMiddleware"
            )
            + 1,
        )
        self.assertEqual(django_settings.STATIC_URL, "/static/")
        self.assertEqual(django_settings.STATIC_ROOT, django_settings.BASE_DIR / "staticfiles")

        entrypoint = (django_settings.BASE_DIR / "docker" / "entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("python manage.py collectstatic --noinput", entrypoint)
        self.assertLess(
            entrypoint.index("python manage.py collectstatic --noinput"),
            entrypoint.index("exec gunicorn"),
        )

    def test_alert_layout_defaults_to_stacked_content_and_keeps_flex_opt_in(self):
        response = self.client.get(reverse("submissions:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ".alert:not(.d-flex) {")
        self.assertContains(response, ".cfm-alert-stack")

        reextract_prompt = self.client.post(
            reverse("submissions:title_author_extraction"),
            {"action": "reextract_all_prompt"},
        )
        self.assertEqual(reextract_prompt.status_code, 200)
        self.assertContains(
            reextract_prompt,
            'class="alert alert-danger border cfm-attention cfm-alert-stack"',
        )
        self.assertContains(reextract_prompt, "Confirm Re-extract All Active PDFs")

        self.make_master_paper("P001", "Missing Final", "Ada")
        blocked_export = self.client.post(
            reverse("submissions:export_reports"),
            {"action": "publication_package"},
        )
        self.assertEqual(blocked_export.status_code, 200)
        self.assertContains(
            blocked_export,
            'class="alert alert-danger border cfm-attention cfm-alert-stack"',
        )
        self.assertContains(blocked_export, "Missing Final Submission")

    def test_organized_list_missing_final_does_not_render_empty_status_badges(self):
        self.make_master_paper("P001", "Missing Final", "Ada")

        response = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<td data-column="source"><span class="text-muted">--</span></td>',
            html=True,
        )
        self.assertContains(
            response,
            '<td data-column="extraction"><span class="text-muted">--</span></td>',
            html=True,
        )

    def test_process_page_uses_full_width_when_only_one_issue_type_exists(self):
        self.make_master_paper("P001", "Pending PDF", "Ada")
        self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Pending PDF",
            processing_status="pending",
            page_count=None,
            pdf_hash="",
            thumbnail_status="pending",
        )

        response = self.client.get(reverse("submissions:process"))

        self.assertContains(response, "Active PDFs not processed")
        self.assertContains(response, 'class="col-12"')
        self.assertContains(response, "cfm-process-issue-content")
        self.assertContains(response, "cfm-process-action-card")
        self.assertNotContains(response, 'class="col-md-6"')

    def test_base_layout_exposes_brand_icon_and_favicons(self):
        response = self.client.get(reverse("submissions:dashboard"))

        self.assertContains(response, 'class="cfm-brand-icon"')
        self.assertContains(response, '/static/submissions/brand/favicon-32.png')
        self.assertContains(response, '/static/submissions/brand/favicon-16.png')
        self.assertContains(response, '/static/submissions/brand/apple-touch-icon.png')
        for asset in (
            "submissions/brand/favicon-32.png",
            "submissions/brand/favicon-16.png",
            "submissions/brand/apple-touch-icon.png",
            "submissions/brand/app-icon-512.png",
        ):
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))

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
                response = self.client.get(path)
                if path == reverse("submissions:active_versions"):
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("view=compact", response["Location"])
                else:
                    self.assertEqual(response.status_code, 200)

    def test_verify_paper_ids_defers_suggestions_for_hidden_rows(self):
        self.make_master_paper("P001", title="Ready Paper")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Ready Paper",
            extracted_title="Ready Paper",
            paper_id_verified=True,
            verification_status="verified",
        )
        self.make_final_submission(
            final_submission_id="20",
            paper_id_filled="BAD",
            final_submission_title="Unknown Paper",
            extracted_title="Unknown Paper",
            paper_id_verified=False,
            verification_status="pending",
        )

        with patch(
            "submissions.services.verification.best_title_match",
            side_effect=AssertionError("Hidden rows should not calculate suggestions."),
        ):
            response = self.client.get(
                reverse("submissions:verify_paper_ids"), {"filter": "identical"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Final ID 10")
        self.assertNotContains(response, "Final ID 20")

    def test_docker_rebuild_script_infers_existing_instance_settings(self):
        script_path = (
            Path(django_settings.BASE_DIR) / "scripts" / "rebuild_docker_instances.py"
        )
        spec = importlib.util.spec_from_file_location("docker_rebuild", script_path)
        docker_rebuild = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(docker_rebuild)
        container = {
            "Name": "/sms-conf-a-web-1",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "sms-conf-a",
                    "com.docker.compose.project.working_dir": str(
                        django_settings.BASE_DIR
                    ),
                    "com.docker.compose.service": "web",
                },
                "Env": [
                    "SMS_SECRET_KEY=secret",
                    "SMS_DEBUG=1",
                    "SMS_ALLOWED_HOSTS=127.0.0.1,localhost",
                ],
            },
            "HostConfig": {
                "PortBindings": {
                    "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000"}]
                }
            },
            "Mounts": [
                {"Destination": "/app/data", "Source": "/srv/sms/conf-a"},
                {"Destination": "/app", "Source": str(django_settings.BASE_DIR)},
            ],
            "State": {"Running": True},
        }

        instances = docker_rebuild.matching_instances(
            [container], Path(django_settings.BASE_DIR), set()
        )

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["project"], "sms-conf-a")
        self.assertEqual(instances[0]["sms_bind_host"], "127.0.0.1")
        self.assertEqual(instances[0]["sms_port"], "9000")
        self.assertEqual(instances[0]["sms_data_dir"], "/srv/sms/conf-a")
        self.assertEqual(instances[0]["env"]["SMS_ALLOWED_HOSTS"], "127.0.0.1,localhost")

    def test_docker_rebuild_script_closes_and_removes_generated_env_file(self):
        script_path = (
            Path(django_settings.BASE_DIR) / "scripts" / "rebuild_docker_instances.py"
        )
        spec = importlib.util.spec_from_file_location("docker_rebuild", script_path)
        docker_rebuild = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(docker_rebuild)
        instance = {
            "project": "sms-conf-a",
            "name": "sms-conf-a-web-1",
            "running": True,
            "sms_bind_host": "127.0.0.1",
            "sms_port": "9000",
            "sms_data_dir": "/srv/sms/conf-a",
            "env": {"SMS_SECRET_KEY": "secret"},
        }
        generated_path = None

        def inspect_generated_env(command, *, cwd, capture):
            nonlocal generated_path
            generated_path = Path(command[command.index("--env-file") + 1])
            with generated_path.open(encoding="utf-8") as handle:
                contents = handle.read()
            self.assertIn("SMS_PORT=9000", contents)
            self.assertIn("SMS_SECRET_KEY=secret", contents)

        with patch.object(docker_rebuild, "run", side_effect=inspect_generated_env):
            docker_rebuild.rebuild_instance(
                instance, Path(django_settings.BASE_DIR), dry_run=False
            )

        self.assertIsNotNone(generated_path)
        self.assertFalse(generated_path.exists())

    def test_final_submission_batch_upload_limit_is_documented(self):
        self.assertEqual(django_settings.DATA_UPLOAD_MAX_NUMBER_FILES, 5000)

        page = self.client.get(reverse("submissions:final_submission_list"))

        self.assertContains(page, "Large batches are supported up to 5000 files per request")

    def test_long_worklists_use_integrated_compact_navigation(self):
        self.make_master_paper("P001", "Compact Workflow Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Compact Workflow Paper",
            extracted_title="Compact Workflow Paper",
            extracted_authors="Ada Lovelace",
            page_count=8,
        )

        final_list = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(final_list, 'data-bs-target="#import-reupload-panel"')
        self.assertContains(final_list, 'id="import-reupload-panel"')
        self.assertContains(final_list, "cfm-table-sticky")

        formatting = self.client.get(reverse("submissions:formatting"), {"filter": "all"})
        self.assertContains(formatting, f'id="format-review-{submission.pk}"')
        self.assertContains(formatting, "Review paper")
        self.assertContains(formatting, 'data-bs-parent="#formatting-queue"')
        self.assertContains(formatting, 'hx-target="#formatting-worklist"')
        self.assertContains(formatting, "PDF")
        self.assertContains(formatting, "Source")

        title_review = self.client.get(
            reverse("submissions:title_author_extraction"),
            {"filter": "all"},
        )
        self.assertContains(title_review, "Workflow")
        self.assertContains(title_review, "Tracked views")

    def test_phase_two_uses_pinned_local_htmx_asset(self):
        page = self.client.get(reverse("submissions:formatting"))

        self.assertContains(page, "/static/submissions/vendor/htmx-2.0.10.min.js")
        asset_path = finders.find("submissions/vendor/htmx-2.0.10.min.js")
        self.assertIsNotNone(asset_path)
        self.assertGreater(Path(asset_path).stat().st_size, 50000)

    def test_modernized_worklists_use_local_assets_and_progressive_get_updates(self):
        page = self.client.get(reverse("submissions:dashboard"))
        self.assertContains(page, "/static/submissions/vendor/tabler-1.4.0.min.css")
        self.assertContains(page, "/static/submissions/vendor/tabler-1.4.0.min.js")
        for asset in (
            "submissions/vendor/tabler-1.4.0.min.css",
            "submissions/vendor/tabler-1.4.0.min.js",
            "submissions/vendor/TABLER-LICENSE.txt",
        ):
            self.assertIsNotNone(finders.find(asset))

        for route_name, target in (
            ("author_count", "author-count-worklist"),
            ("exceptions_center", "exceptions-worklist"),
            ("title_author_extraction", "title-author-worklist"),
            ("verify_paper_ids", "verify-paper-id-worklist"),
            ("process", "process-preview-worklist"),
            ("final_submission_list", "final-submission-worklist"),
            ("organized_list", "organized-worklist"),
        ):
            response = self.client.get(reverse(f"submissions:{route_name}"))
            self.assertContains(response, f'id="{target}"')
            self.assertContains(response, f'hx-target="#{target}"')

    def test_semantic_palette_and_navbar_contrast_are_centralized(self):
        page = self.client.get(reverse("submissions:dashboard"))
        self.assertContains(page, "--cfm-bg: #dfe5eb")
        self.assertContains(page, "--cfm-surface: #eef2f5")
        self.assertContains(page, "--cfm-surface-subtle: #e5ebf0")
        self.assertContains(page, "--cfm-surface-strong: #d4dde6")
        self.assertContains(page, "--cfm-font-body: 0.9375rem")
        self.assertContains(page, "--cfm-font-small: 0.8125rem")
        self.assertContains(page, "--cfm-font-badge: 0.75rem")
        self.assertContains(page, "--cfm-text: #141d2b")
        self.assertContains(page, "--cfm-muted: #31475e")
        self.assertContains(page, "font-size: var(--cfm-font-body)")
        self.assertContains(page, "font-size: var(--cfm-font-small) !important")
        self.assertContains(page, "h6, .h6 { font-size: 0.875rem")
        self.assertContains(page, "code {")
        self.assertContains(page, "color: #203a59")
        self.assertContains(page, ".cfm-code-block {")
        self.assertContains(page, "color: var(--cfm-text)")
        self.assertContains(page, "--cfm-nav-strip: #dfe6ed")
        self.assertContains(page, "--cfm-nav-hover: #d2dce7")
        self.assertContains(page, "--cfm-nav-active: #dbe8f8")
        self.assertContains(page, "--cfm-danger-text: #991b1b")
        self.assertContains(page, "--cfm-info-text: #1e40af")
        self.assertContains(page, "--cfm-success-text: #166534")
        self.assertContains(page, "--tblr-btn-bg: #f1f5f9")
        self.assertContains(page, "--tblr-btn-border-color: #64748b")
        self.assertContains(page, "--tblr-btn-bg: #f0f6ff")
        self.assertContains(page, ".btn-success {")
        self.assertContains(page, "--tblr-btn-bg: #166534")
        self.assertContains(page, ".btn-warning {")
        self.assertContains(page, "--tblr-btn-color: #111827")
        self.assertContains(page, ".btn-danger {")
        self.assertContains(page, "--tblr-btn-bg: #b91c1c")
        self.assertContains(page, "border-radius: 999px")
        self.assertContains(page, ".cfm-status-line .badge")
        self.assertContains(page, ".cfm-title-guard-comparisons")
        self.assertContains(page, "grid-template-columns: minmax(0, 1fr)")
        self.assertContains(page, "overflow-wrap: anywhere")
        self.assertContains(page, "min-height: auto")
        self.assertContains(page, ".cfm-primary-nav .nav-link:hover")
        self.assertContains(page, "Editorial publication workspace")
        self.assertContains(page, "Current conference")
        self.assertContains(page, "submissions/brand/app-icon-512.png")
        self.assertNotContains(page, "badge fs-6")
        self.assertNotContains(page, ">Publication Candidates</a>")

    def test_author_count_supports_stable_sorting(self):
        self.make_master_paper("P001", "One", "Ada")
        self.make_master_paper("P002", "Two", "Ada")
        self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            extracted_authors="Ada Lovelace",
        )
        self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            extracted_authors="Ada Lovelace; Grace Hopper",
        )
        rebuild_paper_authors()

        response = self.client.get(
            reverse("submissions:author_count"),
            {"filter": "all", "sort": "paper_count_desc"},
        )
        self.assertEqual(response.context["current_sort"], "paper_count_desc")
        counts = [row["publication_paper_count"] for row in response.context["rows"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_publication_candidates_are_integrated_into_organized_list(self):
        self.make_master_paper("P001", "Compact Candidate", "Ada")
        self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Compact Candidate",
            extracted_title="Compact Candidate",
            extracted_authors="Ada Lovelace",
        )

        response = self.client.get(
            reverse("submissions:organized_list"), {"view": "compact"}
        )
        self.assertEqual(response.context["view_mode"], "compact")
        self.assertContains(response, "Compact candidates shows")
        self.assertContains(response, "Compact Candidate")

        legacy = self.client.get(reverse("submissions:active_versions"), {"q": "P001"})
        self.assertEqual(legacy.status_code, 302)
        self.assertIn("view=compact", legacy["Location"])
        self.assertIn("q=P001", legacy["Location"])

    def test_import_upload_zones_keep_preview_before_apply(self):
        final_page = self.client.get(reverse("submissions:final_submission_list"))
        self.assertContains(
            final_page, '<div class="cfm-upload-zone" data-upload-zone>', count=2
        )
        self.assertContains(final_page, "0 selected: 0 PDF, 0 source/other.")
        self.assertContains(final_page, "preview every change")

        master_page = self.client.get(reverse("submissions:initial_paper_list"))
        self.assertContains(master_page, '<div class="cfm-upload-zone" data-upload-zone>')
        self.assertContains(master_page, "No metadata file selected.")

    def test_ui_worklists_do_not_change_publication_package_bytes(self):
        self.make_master_paper("P001", "Stable Publication", "Ada Lovelace")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Stable Publication",
        )
        self.mark_submission_publication_ready(
            submission,
            title="Stable Publication",
            authors="Ada Lovelace",
        )
        rebuild_paper_authors()

        def package_fingerprint(path):
            with zipfile.ZipFile(path) as archive:
                file_hashes = {
                    name: hashlib.sha256(archive.read(name)).hexdigest()
                    for name in archive.namelist()
                    if not name.startswith("publication_manifest_")
                }
                manifest_name = next(
                    name for name in archive.namelist()
                    if name.startswith("publication_manifest_")
                )
                manifest_rows = list(
                    csv.DictReader(
                        io.StringIO(archive.read(manifest_name).decode("utf-8-sig"))
                    )
                )
            return file_hashes, manifest_rows

        blockers_before = [row["category"] for row in publication_readiness_rows()]
        baseline = package_fingerprint(export_publication_package())
        for route_name, params in (
            ("organized_list", {"view": "checklist"}),
            ("organized_list", {"view": "compact"}),
            ("final_submission_list", {"filter": "all"}),
            ("verify_paper_ids", {"filter": "all"}),
            ("title_author_extraction", {"filter": "all"}),
            ("formatting", {"filter": "all"}),
            ("process", {"filter": "all"}),
            ("exceptions_center", {"filter": "all"}),
            ("author_count", {"filter": "all"}),
        ):
            self.assertEqual(
                self.client.get(reverse(f"submissions:{route_name}"), params).status_code,
                200,
            )
        after = package_fingerprint(export_publication_package())
        blockers_after = [row["category"] for row in publication_readiness_rows()]

        self.assertEqual(after, baseline)
        self.assertEqual(blockers_after, blockers_before)

    def test_process_preview_keeps_full_rows_and_supports_issue_filtering(self):
        settings_obj = AppSetting.load()
        settings_obj.page_minimum = 6
        settings_obj.page_limit = 12
        settings_obj.save()
        self.make_master_paper("P001", "Short Paper", "Ada")
        self.make_master_paper("P002", "Range Paper", "Grace")
        self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Short Paper",
            page_count=5,
            processing_status="processed",
        )
        self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="Range Paper",
            page_count=8,
            processing_status="processed",
        )

        all_rows = self.client.get(reverse("submissions:process"))
        self.assertContains(all_rows, "Every matching paper remains fully expanded")
        self.assertContains(all_rows, "Jump to paper")
        self.assertContains(all_rows, "thumbnail-preview-modal")
        self.assertContains(all_rows, "P001")
        self.assertContains(all_rows, "P002")

        issues = self.client.get(
            reverse("submissions:process"),
            {"filter": "page_issues"},
        )
        self.assertContains(issues, "P001")
        self.assertNotContains(issues, "P002")

    def test_process_preview_records_formatting_issue_in_existing_workflow(self):
        self.make_master_paper("P001", "Formatting Issue Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Formatting Issue Paper",
            page_count=8,
            format_status="review_ok",
            format_notes="Previously checked title spacing.",
            source_hash="reviewed-source-hash",
            title_author_review_status="review_ok",
            extracted_title_verified=True,
        )
        return_to = (
            f"{reverse('submissions:process')}?filter=all"
            f"#paper-preview-{submission.pk}"
        )

        page = self.client.get(reverse("submissions:process"))
        self.assertContains(page, "Record formatting issue")
        self.assertContains(page, "Open Formatting Review")
        self.assertContains(page, "Current formatting notes")
        self.assertContains(page, "Record issue for this page")
        self.assertContains(page, f'id="formatting-triage-{submission.pk}"')

        response = self.client.post(
            reverse("submissions:process"),
            {
                "action": "record_format_issue",
                "submission_id": submission.pk,
                "evidence_token": make_evidence_token(
                    "process-formatting-issue",
                    formatting_issue_evidence(submission),
                ),
                "page_number": "3",
                "issue_note": "  Bottom margin is too small.  ",
                "return_to": return_to,
            },
        )

        self.assertRedirects(response, return_to, fetch_redirect_response=False)
        submission.refresh_from_db()
        self.assertEqual(submission.format_status, "needs_edit")
        self.assertEqual(
            submission.format_notes,
            "Previously checked title spacing.\n\nPage 3: Bottom margin is too small.",
        )
        self.assertEqual(submission.source_hash, "")
        self.assertEqual(submission.title_author_review_status, "review_ok")
        self.assertTrue(submission.extracted_title_verified)
        self.assertEqual(
            submission.review_state.format_status,
            "needs_edit",
        )
        self.assertEqual(
            submission.review_state.format_notes,
            submission.format_notes,
        )
        event = self.latest_audit_event(
            "formatting_issue_recorded_from_pdf_preview"
        )
        self.assertEqual(event["paper_id"], "P001")
        self.assertEqual(event["extra"]["page_number"], 3)
        self.assertTrue(
            event["reset_flags"]["format_review_reset_from_review_ok"]
        )

    def test_process_preview_rejects_invalid_formatting_issue_without_changes(self):
        self.make_master_paper("P001", "Formatting Issue Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Formatting Issue Paper",
            page_count=8,
            format_status="pending",
            format_notes="Keep this note.",
        )

        response = self.client.post(
            reverse("submissions:process"),
            {
                "action": "record_format_issue",
                "submission_id": submission.pk,
                "evidence_token": make_evidence_token(
                    "process-formatting-issue",
                    formatting_issue_evidence(submission),
                ),
                "page_number": "9",
                "issue_note": "Out-of-range page.",
            },
            follow=True,
        )

        self.assertContains(response, "Page 9 is outside this 8-page PDF.")
        submission.refresh_from_db()
        self.assertEqual(submission.format_status, "pending")
        self.assertEqual(submission.format_notes, "Keep this note.")

    def test_process_preview_filters_needs_processing_and_processed_candidates(self):
        self.make_master_paper("P001", "Needs Processing", "Ada")
        self.make_master_paper("P002", "Processed Paper", "Grace")
        pending = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Needs Processing",
            processing_status="pending",
        )
        processed = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="Processed Paper",
            processing_status="processed",
            page_count=8,
        )
        pending.pdf_file.save("pending.pdf", ContentFile(b"pending pdf"), save=True)
        processed.pdf_file.save("processed.pdf", ContentFile(b"processed pdf"), save=True)
        thumbnail_folder = Path(django_settings.MEDIA_ROOT) / "pdf_thumbnails" / "102"
        thumbnail_folder.mkdir(parents=True, exist_ok=True)
        (thumbnail_folder / "page-1.png").write_bytes(b"thumbnail")
        processed.thumbnail_folder = str(thumbnail_folder)
        processed.pdf_hash = calculate_pdf_hash(processed.pdf_file.path)
        processed.save(update_fields=["thumbnail_folder", "pdf_hash", "updated_at"])

        needs_processing = self.client.get(
            reverse("submissions:process"), {"filter": "needs_processing"}
        )
        self.assertEqual(
            [row["submission"].paper_id_filled for row in needs_processing.context["processed_rows"]],
            ["P001"],
        )

        processed_page = self.client.get(
            reverse("submissions:process"), {"filter": "processed"}
        )
        self.assertEqual(
            [row["submission"].paper_id_filled for row in processed_page.context["processed_rows"]],
            ["P002"],
        )
        self.assertContains(processed_page, 'loading="lazy"')

    def test_organized_summary_separates_blockers_from_tracked_information(self):
        self.make_master_paper("P001", "Master Title", "Ada")
        self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Different Final Title",
            extracted_title="Different Final Title",
            paper_id_verified=True,
            verification_status="verified",
        )

        response = self.client.get(reverse("submissions:organized_list"))
        _rows, summary, _settings, _filter, _sort = organized_list_rows()
        self.assertContains(response, "Publication blockers")
        self.assertContains(response, "Tracked information")
        self.assertContains(response, "Verified title differences")
        self.assertEqual(summary["unverified"], 0)
        self.assertEqual(summary["verified_title_differences"], 1)

    def test_final_submission_import_panel_is_explicit_and_collapsed(self):
        response = self.client.get(reverse("submissions:final_submission_list"))

        self.assertContains(response, "Import / Re-upload")
        self.assertContains(response, 'id="import-reupload-panel"')
        self.assertContains(response, "preview every change")

    def test_author_count_and_exception_center_support_focused_filters(self):
        settings_obj = AppSetting.load()
        settings_obj.author_paper_limit = 1
        settings_obj.page_minimum = 6
        settings_obj.save()
        for paper_id, final_id in (("P001", "101"), ("P002", "102")):
            self.make_master_paper(paper_id, f"Paper {paper_id}", "Ada")
            self.make_final_submission(
                final_submission_id=final_id,
                paper_id_filled=paper_id,
                final_submission_title=f"Paper {paper_id}",
                extracted_authors="Ada Lovelace",
                page_count=5 if paper_id == "P001" else 8,
            )
        rebuild_paper_authors()

        authors = self.client.get(
            reverse("submissions:author_count"),
            {"filter": "over_limit", "q": "Ada"},
        )
        self.assertContains(authors, "Ada Lovelace")
        self.assertContains(authors, "Needs attention")

        exceptions = self.client.get(
            reverse("submissions:exceptions_center"),
            {"filter": "not_allowed", "type": "page", "q": "P001"},
        )
        self.assertContains(exceptions, "Page count")
        self.assertContains(exceptions, "P001")
        self.assertNotContains(exceptions, "P002")

    def test_contextual_edit_links_return_to_review_worklists(self):
        self.make_master_paper("P001", "Return Workflow", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Return Workflow",
            extracted_title="Return Workflow",
        )
        for route_name, params in (
            ("formatting", {"filter": "all", "q": "P001"}),
            ("title_author_extraction", {"filter": "all", "q": "P001"}),
            ("verify_paper_ids", {"filter": "all", "q": "P001"}),
            ("organized_list", {"view": "compact", "q": "P001"}),
        ):
            response = self.client.get(reverse(f"submissions:{route_name}"), params)
            expected_return = response.wsgi_request.get_full_path()
            edit_url = reverse("submissions:final_submission_edit", args=[submission.pk])
            self.assertContains(response, f"{edit_url}?next=")
            self.assertContains(response, quote(expected_return, safe="/"))

    def test_exception_and_not_publishing_edit_links_preserve_context(self):
        settings_obj = AppSetting.load()
        settings_obj.page_minimum = 6
        settings_obj.save()
        self.make_master_paper("P001", "Exception Return", "Ada")
        issue = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Exception Return",
            page_count=5,
        )
        excluded = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="OUTSIDE",
            final_submission_title="Not Publishing Return",
            excluded_from_publication=True,
        )

        cases = (
            (
                "exceptions_center",
                {"filter": "not_allowed", "type": "page", "q": "P001"},
                issue,
            ),
            ("not_publishing_list", {"q": "102"}, excluded),
        )
        for route_name, params, submission in cases:
            response = self.client.get(reverse(f"submissions:{route_name}"), params)
            expected_return = response.wsgi_request.get_full_path()
            edit_url = reverse("submissions:final_submission_edit", args=[submission.pk])
            self.assertContains(response, f"{edit_url}?next=")
            self.assertContains(response, quote(expected_return, safe="/"))

    def test_edit_page_separates_discard_from_normal_fields(self):
        self.make_master_paper("P001", "Safe Edit", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Safe Edit",
        )

        response = self.client.get(
            reverse("submissions:final_submission_edit", args=[submission.pk])
        )
        content = response.content.decode()
        self.assertContains(response, "Version actions")
        self.assertContains(response, 'id="version-discard-panel" class="collapse"')
        ordered_sections = [
            "Submission identity",
            "Metadata",
            "Current row files",
            "Plagiarism data and report",
            "Workflow status summary",
            "Save Final Submission",
            "Version actions",
        ]
        positions = [content.index(label) for label in ordered_sections]
        self.assertEqual(positions, sorted(positions))
        self.assertContains(response, "Dangerous version-level actions are separate")

    def test_phase_one_navigation_preserves_publication_files_and_review_state(self):
        self.make_master_paper("P001", "Publication Safety", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Publication Safety",
            extracted_title="Publication Safety",
            extracted_authors="Ada Lovelace",
            paper_id_verified=True,
            processing_status="processed",
            page_count=8,
            pdf_hash="processed-hash",
            title_author_review_status="review_ok",
            title_author_verified=True,
            format_status="review_ok",
        )
        submission.pdf_file.save("original.pdf", ContentFile(b"original pdf"), save=True)
        submission.source_file.save("original.docx", ContentFile(b"original source"), save=True)
        submission.formatted_pdf_file.save("corrected.pdf", ContentFile(b"corrected pdf"), save=True)
        submission.formatted_source_file.save("corrected.docx", ContentFile(b"corrected source"), save=True)
        before_pdf = publication_pdf_info(submission)
        before_source = publication_source_info(submission)
        before_pdf_bytes = Path(before_pdf["path"]).read_bytes()
        before_source_bytes = Path(before_source["path"]).read_bytes()

        organized_url = reverse("submissions:organized_list") + "?filter=all&q=P001"
        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            self.final_submission_form_data(submission, next=organized_url),
        )
        self.assertEqual(response["Location"], organized_url)

        submission.refresh_from_db()
        after_pdf = publication_pdf_info(submission)
        after_source = publication_source_info(submission)
        self.assertEqual(after_pdf["source"], "corrected")
        self.assertEqual(after_source["source"], "corrected")
        self.assertEqual(Path(after_pdf["path"]).read_bytes(), before_pdf_bytes)
        self.assertEqual(Path(after_source["path"]).read_bytes(), before_source_bytes)
        self.assertTrue(submission.active_version)
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(submission.processing_status, "processed")
        self.assertEqual(submission.title_author_review_status, "review_ok")
        self.assertEqual(submission.format_status, "review_ok")

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
            {
                "action": "reset_folders",
                "evidence_token": self.settings_evidence_token(),
            },
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
            {
                "exception_key": f"page:{submission.pk}",
                "action": "approve_exception",
                "evidence_token": self.exception_evidence_token(
                    f"page:{submission.pk}"
                ),
            },
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
                "evidence_token": self.exception_evidence_token(
                    f"page:{submission.pk}"
                ),
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
                "evidence_token": self.exception_evidence_token(
                    f"author_number:{submission.pk}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.author_number_exception_author_count, 6)

        submission.similarity_score = 42
        submission.single_similarity_score = 12
        submission.save(update_fields=["similarity_score", "single_similarity_score", "updated_at"])
        response = self.client.get(reverse("submissions:exceptions_center"), {"filter": "not_allowed"})
        self.assertContains(response, "Plagiarism %")
        self.assertContains(response, "Single %")

        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {
                "exception_key": f"plagiarism_percent:{submission.pk}",
                "action": "approve_exception",
                "evidence_token": self.exception_evidence_token(
                    f"plagiarism_percent:{submission.pk}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.plagiarism_percent_exception_approved)

        response = self.client.post(
            reverse("submissions:exceptions_center"),
            {
                "exception_key": f"plagiarism_percent:{submission.pk}",
                "action": "approve_exception",
                "reason": "chair approved high overlap",
                "evidence_token": self.exception_evidence_token(
                    f"plagiarism_percent:{submission.pk}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.plagiarism_percent_exception_approved)

        response = self.client.get(reverse("submissions:organized_list"))
        self.assertContains(response, "P allowed")
        self.assertContains(response, "Manage exceptions")

        submission.similarity_score = 43
        submission.save(update_fields=["similarity_score", "updated_at"])
        response = self.client.get(reverse("submissions:organized_list"))
        self.assertContains(response, "P stale")

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
                "evidence_token": self.exception_evidence_token(
                    "author_limit:a"
                ),
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
            {
                "submission_id": submission.pk,
                "corrected_paper_id": "P001",
                "evidence_token": self.paper_id_review_token(
                    submission,
                    "paper_id_evidence_token",
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)

        response = self.client.post(
            reverse("submissions:verify_paper_ids"),
            {
                "submission_id": submission.pk,
                "action": "unverify",
                "evidence_token": self.paper_id_review_token(
                    submission,
                    "paper_id_unverify_evidence_token",
                ),
            },
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
                "evidence_token": self.publication_decision_token(
                    submission
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.excluded_from_publication)

        response = self.client.post(
            reverse("submissions:not_publishing_list"),
            {
                "submission_id": submission.pk,
                "action": "undo_not_publishing",
                "evidence_token": self.publication_decision_token(
                    submission
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.excluded_from_publication)
        verify_submission(submission, "P001")

        formatting_page = self.open_formatting_review(submission)
        response = self.client.post(
            reverse("submissions:formatting"),
            self.formatting_post_data(
                formatting_page,
                submission,
                format_status="review_ok",
                format_notes="corrected",
                corrected_source=self.uploaded_file("fixed.docx", b"fixed source"),
            ),
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
                "evidence_token": make_evidence_token(
                    "final-submission-edit",
                    final_submission_edit_evidence(submission),
                ),
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
                "evidence_token": make_evidence_token(
                    "final-submission-edit",
                    final_submission_edit_evidence(submission),
                ),
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
        update_formatting_submission(
            submission,
            {
                "corrected_pdf": None,
                "corrected_source": None,
                "format_status": "review_ok",
                "format_notes": "Current files reviewed.",
            },
        )
        submission.plagiarism_report_stale = False
        submission.save(update_fields=["plagiarism_report_stale"])

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

    def test_image_magnifier_is_shared_by_all_formatting_review_modes(self):
        self.make_master_paper("P001", "Magnifier Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Magnifier Paper",
            extracted_title="Magnifier Paper",
        )

        preview = {
            "exists": True,
            "url": "/media/format_previews/magnifier.png",
            "path": "",
            "status": "ready",
            "message": "First page upper-half preview.",
        }
        with patch(
            "submissions.controllers.reviews.formatting_preview_info",
            return_value=preview,
        ):
            list_page = self.open_formatting_review(submission)
            single_page = self.open_formatting_review(submission, mode="single")
            focus_page = self.open_formatting_review(submission, mode="focus")

        for response in (list_page, single_page, focus_page):
            self.assertContains(response, "data-cfm-image-magnifier")
            self.assertContains(
                response,
                'data-cfm-image-magnifier-hint="Hold Ctrl to magnify"',
            )
            self.assertNotContains(response, 'title="Hold Ctrl to magnify"')
            self.assertContains(
                response,
                "/static/submissions/image_magnifier.js",
            )
            self.assertContains(response, "/static/submissions/image_magnifier.css")

        asset_path = finders.find("submissions/image_magnifier.js")
        self.assertIsNotNone(asset_path)
        asset = Path(asset_path).read_text(encoding="utf-8")
        self.assertIn("requestAnimationFrame", asset)
        self.assertIn("shown.bs.collapse", asset)
        self.assertIn("htmx:afterSwap", asset)
        self.assertIn('(hover: hover) and (pointer: fine)', asset)
        self.assertIn('event.key === "Control"', asset)
        self.assertIn('window.addEventListener("blur"', asset)
        self.assertIn('document.addEventListener("visibilitychange"', asset)
        self.assertIn("cfm-image-magnifier-hint", asset)
        self.assertIn("container.dataset.cfmImageMagnifierHint", asset)
        self.assertIn("pointerInside && !active && !controlPressed", asset)
        stylesheet_path = finders.find("submissions/image_magnifier.css")
        self.assertIsNotNone(stylesheet_path)
        stylesheet = Path(stylesheet_path).read_text(encoding="utf-8")
        self.assertIn("aspect-ratio: 3 / 2", stylesheet)
        self.assertIn("width: min(72%, 30rem)", stylesheet)
        self.assertIn(".cfm-image-magnifier-hint.is-visible", stylesheet)

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

        self.assertEqual(
            formatting_filter_counts(),
            {
                "needs_attention": 1,
                "pending": 1,
                "needs_edit": 0,
                "review_ok": 3,
                "review_ok_no_edit": 1,
                "edited": 2,
                "all": 4,
            },
        )

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

        response = self.open_formatting_review(first, mode="single")
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
        self.assertContains(response, f"current={second.pk}")

        with patch("submissions.services.formatting.get_title_author") as extractor:
            save_response = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    response,
                    first,
                    format_status="pending",
                    format_notes="source only",
                    corrected_source=self.uploaded_file(
                        "fixed.docx", b"fixed source"
                    ),
                ),
            )
        extractor.assert_not_called()
        self.assertEqual(save_response.status_code, 302)
        self.assertIn(f"current={first.pk}", save_response["Location"])
        self.assertNotIn(f"current={second.pk}", save_response["Location"])

        saved_page = self.client.get(save_response["Location"])
        self.assertContains(saved_page, "First Format Paper")
        self.assertContains(saved_page, f"current={second.pk}")

    def test_formatting_single_queue_stays_stable_after_review_ok(self):
        self.make_master_paper("P002", "First Pending Paper", "Ada")
        self.make_master_paper("P010", "Second Pending Paper", "Grace")
        first = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="First Pending Paper",
            extracted_title="First Pending Paper",
            format_status="pending",
        )
        second = self.make_final_submission(
            final_submission_id="110",
            paper_id_filled="P010",
            final_submission_title="Second Pending Paper",
            extracted_title="Second Pending Paper",
            format_status="pending",
        )

        page = self.open_formatting_review(
            first,
            mode="single",
            status_filter="needs_attention",
        )
        self.assertEqual(page.context["single_navigation"]["position"], 1)
        self.assertEqual(page.context["single_navigation"]["next"].pk, second.pk)

        response = self.client.post(
            reverse("submissions:formatting"),
            self.formatting_post_data(
                page,
                first,
                format_status="review_ok",
                format_notes="Reviewed in the stable queue.",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"current={first.pk}", response["Location"])

        saved_page = self.client.get(response["Location"])
        first.refresh_from_db()
        self.assertEqual(first.format_status, "review_ok")
        self.assertEqual(saved_page.context["current_filter"], "needs_attention")
        self.assertEqual(saved_page.context["single_navigation"]["position"], 1)
        self.assertEqual(saved_page.context["single_navigation"]["next"].pk, second.pk)
        self.assertContains(saved_page, "Go next")

    def test_formatting_single_queue_uses_natural_order_and_preserves_search(self):
        self.make_master_paper("P10", "Queue Paper Ten", "Ada")
        self.make_master_paper("P2", "Queue Paper Two", "Grace")
        ten = self.make_final_submission(
            final_submission_id="110",
            paper_id_filled="P10",
            final_submission_title="Queue Paper Ten",
            extracted_title="Queue Paper Ten",
            format_status="pending",
        )
        two = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P2",
            final_submission_title="Queue Paper Two",
            extracted_title="Queue Paper Two",
            format_status="pending",
        )

        page = self.open_formatting_review(
            two,
            mode="single",
            status_filter="needs_attention",
            query="Queue Paper",
        )

        navigation = page.context["single_navigation"]
        self.assertEqual(navigation["current"].pk, two.pk)
        self.assertEqual(navigation["next"].pk, ten.pk)
        self.assertEqual(page.context["q"], "Queue Paper")
        self.assertIn("filter=needs_attention", navigation["back_url"])
        self.assertIn("q=Queue+Paper", navigation["back_url"])

        next_page = self.client.get(navigation["next_url"])
        self.assertEqual(next_page.context["q"], "Queue Paper")
        self.assertEqual(next_page.context["single_navigation"]["current"].pk, ten.pk)

    def test_formatting_single_queue_handles_current_paper_leaving_scope(self):
        self.make_master_paper("P001", "Leaving Scope", "Ada")
        self.make_master_paper("P002", "Still In Scope", "Grace")
        first = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Leaving Scope",
            extracted_title="Leaving Scope",
            format_status="pending",
        )
        second = self.make_final_submission(
            final_submission_id="102",
            paper_id_filled="P002",
            final_submission_title="Still In Scope",
            extracted_title="Still In Scope",
            format_status="pending",
        )
        page = self.open_formatting_review(first, mode="single")
        queue_url = page.request["PATH_INFO"] + "?" + page.request["QUERY_STRING"]

        first.discarded = True
        first.save()
        response = self.client.get(queue_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["single_navigation"]["position"], 1)
        self.assertEqual(response.context["single_navigation"]["next"].pk, second.pk)
        self.assertContains(response, "no longer an active publication formatting candidate")
        self.assertContains(response, "Continue to next paper")
        self.assertNotContains(response, "Paper 0 of")

    def test_formatting_focus_and_single_modes_do_not_render_worklist_pagination(self):
        self.make_master_paper("P001", "Focused Format Paper", "Ada")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Focused Format Paper",
            extracted_title="Focused Format Paper",
        )

        single = self.open_formatting_review(submission, mode="single")
        focus = self.open_formatting_review(submission, mode="focus")

        self.assertNotContains(single, 'data-pagination-position="top"')
        self.assertNotContains(single, 'data-pagination-position="bottom"')
        self.assertNotContains(single, "Focused Formatting review")
        self.assertContains(focus, "Focused Formatting review")
        self.assertContains(focus, "Start Single Paper Mode here")
        self.assertContains(focus, "data-formatting-single-form")
        self.assertNotContains(focus, 'data-pagination-position="top"')
        self.assertNotContains(focus, 'data-pagination-position="bottom"')

    def test_formatting_save_rejects_changed_publication_file_snapshot(self):
        self.make_master_paper("P001", "Snapshot Paper", "Ada")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Snapshot Paper",
            extracted_title="Snapshot Paper",
            format_status="pending",
        )
        page = self.open_formatting_review(submission, mode="single")
        pdf_path = Path(publication_pdf_info(submission)["path"])
        pdf_path.write_bytes(b"changed after review page opened")

        response = self.client.post(
            reverse("submissions:formatting"),
            self.formatting_post_data(
                page,
                submission,
                format_status="review_ok",
                format_notes="This must not save.",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "publication PDF changed after the page was opened")
        submission.refresh_from_db()
        self.assertEqual(submission.format_status, "pending")
        self.assertNotEqual(submission.format_notes, "This must not save.")

    def test_formatting_invalid_upload_retains_bound_status_and_notes(self):
        self.make_master_paper("P001", "Bound Form Paper", "Ada")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Bound Form Paper",
            extracted_title="Bound Form Paper",
            format_status="pending",
        )
        page = self.open_formatting_review(submission, mode="single")

        response = self.client.post(
            reverse("submissions:formatting"),
            self.formatting_post_data(
                page,
                submission,
                format_status="needs_edit",
                format_notes="Keep this explanation visible.",
                corrected_pdf=self.uploaded_file("not-a-pdf.pages", b"invalid"),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corrected PDF must use the .pdf extension")
        self.assertContains(response, "Keep this explanation visible.")
        self.assertContains(
            response,
            f'id="format_status_needs_edit_{submission.pk}"',
            html=False,
        )
        submission.refresh_from_db()
        self.assertEqual(submission.format_status, "pending")

    def test_formatting_rejects_two_uploads_of_the_same_file_kind(self):
        self.make_master_paper("P001", "Duplicate Upload Kind", "Ada")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Duplicate Upload Kind",
            extracted_title="Duplicate Upload Kind",
        )
        page = self.open_formatting_review(submission, mode="single")

        response = self.client.post(
            reverse("submissions:formatting"),
            self.formatting_post_data(
                page,
                submission,
                corrected_pdf=self.uploaded_file("first.pdf", b"%PDF first"),
                corrected_source=self.uploaded_file("second.pdf", b"%PDF second"),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Both uploaded files are classified as PDF files")
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)

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

        page = self.open_formatting_review(submission, mode="single")
        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Wrong Paper Title", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    page,
                    submission,
                    format_status="pending",
                    format_notes="corrected pdf",
                    corrected_pdf=self.uploaded_file("wrong.pdf", b"%PDF wrong"),
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Corrected PDF Title Safety Check")
        self.assertContains(response, "Uploaded PDF Title")
        self.assertContains(response, "Compared with Final Submission Title")
        self.assertContains(response, "Show detailed character diff")
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
                "queue": response.context["single_navigation"]["token"],
                "format_status": "pending",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"current={submission.pk}", response["Location"])
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

        page = self.open_formatting_review(submission)
        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Matching Paper Title", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    page,
                    submission,
                    format_status="pending",
                    format_notes="matching corrected pdf",
                    corrected_source=self.uploaded_file(
                        "actually_pdf.pdf", b"%PDF corrected"
                    ),
                ),
            )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.formatted_pdf_file)
        self.assertEqual(submission.extracted_title, "Existing Extracted Title")
        self.assertEqual(submission.title_author_review_status, "pending")

    def test_cancel_formatting_title_guard_removes_preview_without_saving(self):
        self.make_master_paper("P016", "Formatting Cancel Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="116",
            paper_id_filled="P016",
            final_submission_title="Formatting Cancel Paper",
            extracted_title="Existing Title",
            title_author_review_status="review_ok",
            title_author_verified=True,
        )

        page = self.open_formatting_review(submission, mode="single")
        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Wrong Uploaded Paper", "Ada", 1),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    page,
                    submission,
                    format_status="pending",
                    format_notes="cancel this upload",
                    corrected_pdf=self.uploaded_file("cancel.pdf", b"%PDF cancel"),
                ),
            )

        token = response.context["formatting_confirmation"]["token"]
        token_root = Path(django_settings.MEDIA_ROOT) / "formatting_upload_previews" / token
        self.assertTrue(token_root.exists())
        cancel_response = self.client.post(
            reverse("submissions:formatting"),
            {
                "action": "cancel_formatting_upload",
                "preview_token": token,
                "submission_id": submission.pk,
                "mode": "single",
                "filter": "all",
                "queue": response.context["single_navigation"]["token"],
            },
        )

        self.assertEqual(cancel_response.status_code, 302)
        self.assertFalse(token_root.exists())
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)
        self.assertEqual(submission.extracted_title, "Existing Title")
        self.assertEqual(submission.title_author_review_status, "review_ok")

    def test_formatting_corrected_pdf_extraction_error_requires_confirmation(self):
        self.make_master_paper("P001", "Extraction Error Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Extraction Error Paper",
            extracted_title="Existing Extracted Title",
        )

        page = self.open_formatting_review(submission)
        with patch(
            "submissions.services.formatting.get_title_author",
            side_effect=ValueError("cannot read title"),
        ):
            response = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    page,
                    submission,
                    format_status="pending",
                    format_notes="bad corrected pdf",
                    corrected_pdf=self.uploaded_file("bad.pdf", b"%PDF bad"),
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Title extraction failed")
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)

    def test_formatting_title_guard_confirmation_rejects_stale_submission(self):
        self.make_master_paper("P001", "Guard Snapshot Paper", "Ada")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Guard Snapshot Paper",
            extracted_title="Guard Snapshot Paper",
            format_status="pending",
        )
        page = self.open_formatting_review(submission, mode="single")
        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Wrong Uploaded Paper", "Ada", 1),
        ):
            preview = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    page,
                    submission,
                    corrected_pdf=self.uploaded_file("wrong.pdf", b"%PDF wrong"),
                ),
            )
        token = preview.context["formatting_confirmation"]["token"]
        token_root = Path(django_settings.MEDIA_ROOT) / "formatting_upload_previews" / token

        submission.format_notes = "Changed somewhere else after preview."
        submission.save()
        response = self.client.post(
            reverse("submissions:formatting"),
            {
                "action": "confirm_formatting_upload",
                "preview_token": token,
                "submission_id": submission.pk,
                "mode": "single",
                "filter": "all",
                "queue": preview.context["single_navigation"]["token"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed after the page was opened")
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)
        self.assertTrue(token_root.exists())

    def test_formatting_title_guard_rejects_changed_preview_bytes_and_cleans_token(self):
        self.make_master_paper("P001", "Guarded Formatting Paper", "Ada")
        submission = self.make_final_submission(
            paper_id_filled="P001",
            final_submission_title="Guarded Formatting Paper",
            extracted_title="Guarded Formatting Paper",
            format_status="pending",
        )
        page = self.open_formatting_review(submission, mode="single")
        with patch(
            "submissions.services.formatting.get_title_author",
            return_value=("Wrong Uploaded Paper", "Ada", 1),
        ):
            preview = self.client.post(
                reverse("submissions:formatting"),
                self.formatting_post_data(
                    page,
                    submission,
                    corrected_pdf=self.uploaded_file(
                        "wrong.pdf",
                        b"%PDF reviewed bytes",
                    ),
                ),
            )
        token = preview.context["formatting_confirmation"]["token"]
        token_root = (
            Path(django_settings.MEDIA_ROOT)
            / "formatting_upload_previews"
            / token
        )
        preview_pdf = next(token_root.glob("corrected_pdf-*"))
        preview_pdf.write_bytes(b"%PDF changed after review")

        response = self.client.post(
            reverse("submissions:formatting"),
            {
                "action": "confirm_formatting_upload",
                "preview_token": token,
                "submission_id": submission.pk,
                "mode": "single",
                "filter": "all",
                "queue": preview.context["single_navigation"]["token"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Formatting upload preview file changed")
        submission.refresh_from_db()
        self.assertFalse(submission.formatted_pdf_file)
        self.assertFalse(token_root.exists())

    def test_formatting_single_empty_queue_has_integrated_completion_state(self):
        response = self.client.get(
            reverse("submissions:formatting"),
            {"mode": "single", "filter": "needs_attention"},
        )
        self.assertEqual(response.status_code, 302)

        page = self.client.get(response["Location"])
        self.assertContains(
            page,
            "Queue complete · no papers match the selected filter and search.",
        )
        self.assertContains(page, "This Single Paper queue has no papers to review.")
        self.assertNotContains(page, "Paper 0 of 0")

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
        self.assertEqual(
            final_submission_edit_evidence(submission),
            final_submission_edit_evidence(
                FinalSubmission.objects.get(pk=submission.pk)
            ),
        )

        response = self.client.post(
            reverse("submissions:final_submission_edit", args=[submission.pk]),
            {
                "evidence_token": make_evidence_token(
                    "final-submission-edit",
                    final_submission_edit_evidence(submission),
                ),
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
            ),
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "title_mismatch")
        self.assertEqual(submission.extracted_title, "Original Accepted Title")
        self.assertFalse(submission.extracted_title_verified)
        self.assertEqual(submission.extracted_title_match_status, "title_mismatch")
        event = self.latest_audit_event("final_submission_manual_edit")
        self.assertEqual(event["status"], "success")
        self.assertIn("final_submission_title", event["changed_fields"])
        self.assertEqual(event["before"]["final_submission_title"], "Original Accepted Title")
        self.assertEqual(event["after"]["final_submission_title"], "Final Revised Title For Publication")
        self.assertTrue(event["reset_flags"]["identity_recalculated"])
        self.assert_publication_blocked("Unverified Paper ID")

        verify_submission(submission, "P001")
        apply_title_author_manual_override(
            submission,
            "Final Revised Title For Publication",
            submission.extracted_authors,
            "PDF extraction title needed editorial correction.",
        )
        submission.refresh_from_db()
        self.assertEqual(submission.title_author_source, "manual_override")
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertTrue(submission.extracted_title_verified)
        verify_title_author(submission)

        submission.refresh_from_db()
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "verified")
        self.assertIn("manually verified", submission.verification_message)
        self.assertEqual(publication_readiness_rows(), [])
        self.assertTrue(Path(export_publication_package()).exists())

    def test_manual_create_final_submission_evaluates_paper_id_and_writes_audit(self):
        self.make_master_paper("P001", title="Manual Final Paper")

        response = self.client.post(
            reverse("submissions:final_submission_add"),
            {
                "final_submission_id": "101",
                "start2_paper_id_raw": "P001",
                "paper_id_filled": "P001",
                "final_submission_title": "Manual Final Paper",
                "final_submission_authors": "Ada Lovelace",
                "upload_date": "2026-07-12T12:00:00",
                "similarity_score": "",
                "single_similarity_score": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        submission = FinalSubmission.objects.get(final_submission_id="101")
        self.assertEqual(submission.mapping_source, "manual_add")
        self.assertTrue(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "verified")
        self.assertTrue(submission.active_version)
        self.assertEqual(submission.processing_status, "pending")
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertEqual(submission.format_status, "pending")
        event = self.latest_audit_event("final_submission_manual_create")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["paper_id"], "P001")
        self.assertEqual(event["final_submission_id"], "101")
        self.assertEqual(event["before"], {})
        self.assertTrue(event["reset_flags"]["active_versions_recalculated"])

    def test_manual_create_final_submission_saves_pdf_source_and_recalculates_versions(self):
        self.make_master_paper("P001", title="Replacement Paper")
        older = self.make_final_submission(
            final_submission_id="100",
            paper_id_filled="P001",
            start2_paper_id_raw="P001",
            final_submission_title="Replacement Paper",
            extracted_title="Replacement Paper",
            active_version=True,
            duplicate_submission=False,
        )

        response = self.client.post(
            reverse("submissions:final_submission_add"),
            {
                "final_submission_id": "101",
                "start2_paper_id_raw": "P001",
                "paper_id_filled": "P001",
                "final_submission_title": "Replacement Paper",
                "final_submission_authors": "Ada Lovelace",
                "upload_date": "2026-07-12T13:00:00",
                "pdf_file": SimpleUploadedFile(
                    "manual.pdf",
                    b"%PDF manual final",
                    content_type="application/pdf",
                ),
                "source_file": SimpleUploadedFile(
                    "manual.docx",
                    b"manual source",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
                "similarity_score": "",
                "single_similarity_score": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        created = FinalSubmission.objects.get(final_submission_id="101")
        older.refresh_from_db()
        self.assertTrue(Path(created.pdf_file.path).exists())
        self.assertTrue(Path(created.source_file.path).exists())
        self.assertEqual(Path(created.current_file_path), Path(created.pdf_file.path))
        self.assertEqual(Path(created.source_current_file_path), Path(created.source_file.path))
        self.assertEqual(created.original_file_name, "manual.pdf")
        self.assertEqual(created.source_original_file_name, "manual.docx")
        self.assertEqual(created.processing_status, "pending")
        self.assertIn("Process PDFs", created.processing_message)
        self.assertTrue(created.active_version)
        self.assertFalse(created.duplicate_submission)
        self.assertFalse(older.active_version)
        self.assertTrue(older.duplicate_submission)

    def test_manual_create_invalid_form_does_not_create_record(self):
        self.make_master_paper("P001", title="Invalid Manual Paper")

        response = self.client.post(
            reverse("submissions:final_submission_add"),
            {
                "final_submission_id": "",
                "start2_paper_id_raw": "P001",
                "paper_id_filled": "P001",
                "final_submission_title": "Invalid Manual Paper",
                "final_submission_authors": "Ada Lovelace",
                "upload_date": "2026-07-12T12:00:00",
                "similarity_score": "",
                "single_similarity_score": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        self.assertFalse(FinalSubmission.objects.exists())
        self.assertEqual(read_audit_log(query="final_submission_manual_create", limit=10), [])

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
        self.assertTrue(submission.excluded_from_publication)
        self.assertFalse(submission.paper_id_verified)
        self.assertFalse(submission.auto_verify_blocked)
        self.assertNotIn(
            "Unverified Paper ID",
            {row["category"] for row in publication_readiness_rows()},
        )

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
        source_choices = dict(FinalSubmission._meta.get_field("title_author_source").choices)

        self.assertEqual(source_choices["built_in_extractor"], "Built-in extractor")
        self.assertNotIn("title_author_source", form.fields)

    def test_grobid_title_author_source_is_valid_in_edit_form(self):
        submission = self.make_final_submission(
            title_author_source="grobid",
            title_author_extraction_status="extracted",
        )

        form = FinalSubmissionForm(instance=submission)
        source_choices = dict(FinalSubmission._meta.get_field("title_author_source").choices)

        self.assertEqual(source_choices["grobid"], "GROBID")
        self.assertNotIn("extracted_title", form.fields)
        self.assertNotIn("extracted_authors", form.fields)

    def test_final_submission_edit_does_not_expose_workflow_state_fields(self):
        form = FinalSubmissionForm(instance=self.make_final_submission())

        for field_name in {
            "title_author_review_status",
            "extracted_title_verified",
            "duplicate_author_review_status",
            "processing_message",
            "excluded_from_publication",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
        }:
            self.assertNotIn(field_name, form.fields)

    def test_publication_candidates_exclude_invalid_and_not_publishing_records(self):
        self.make_master_paper("P001", "Included Candidate", "Ada")
        self.make_master_paper("P002", "Excluded Candidate", "Ada")
        self.make_final_submission(
            final_submission_id="PC001",
            paper_id_filled="P001",
            final_submission_title="Included Candidate",
        )
        self.make_final_submission(
            final_submission_id="PC002",
            paper_id_filled="P002",
            final_submission_title="Excluded Candidate",
            excluded_from_publication=True,
        )
        self.make_final_submission(
            final_submission_id="PC003",
            paper_id_filled="NOTMASTER",
            final_submission_title="Invalid Candidate",
        )

        legacy_response = self.client.get(reverse("submissions:active_versions"))
        self.assertEqual(legacy_response.status_code, 302)
        self.assertIn("view=compact", legacy_response["Location"])
        response = self.client.get(legacy_response["Location"])

        self.assertContains(response, "Included Candidate")
        self.assertNotContains(response, "Excluded Candidate")
        self.assertNotContains(response, "Invalid Candidate")

    def test_process_pdfs_only_processes_publication_candidates(self):
        self.make_master_paper("P001", "Included", "Ada")
        self.make_master_paper("P002", "Not Publishing", "Ada")
        included = self.make_final_submission(
            final_submission_id="PROC001",
            paper_id_filled="P001",
        )
        self.make_final_submission(
            final_submission_id="PROC002",
            paper_id_filled="P002",
            excluded_from_publication=True,
        )
        self.make_final_submission(
            final_submission_id="PROC003",
            paper_id_filled="NOTMASTER",
        )
        self.make_final_submission(
            final_submission_id="PROC004",
            paper_id_filled="P001",
            discarded=True,
        )

        with patch(
            "submissions.services.pdf_processor.process_submission_pdf",
            return_value=None,
        ) as process_one, patch(
            "submissions.services.pdf_processor.sync_debug_publication_files",
            return_value={"synced_count": 0, "skipped_count": 0, "manifest_path": ""},
        ):
            process_all_pdfs()

        self.assertEqual([call.args[0].pk for call in process_one.call_args_list], [included.pk])

    def test_manual_override_source_choice_exists(self):
        source_choices = dict(FinalSubmission._meta.get_field("title_author_source").choices)

        self.assertEqual(source_choices["manual_override"], "Manual override")

    def test_title_author_manual_override_requires_reason(self):
        submission = self.make_final_submission(final_submission_id="MO001")

        with self.assertRaises(ManualOverrideError):
            apply_title_author_manual_override(
                submission,
                "Manual Title",
                "Ada Lovelace",
                "",
            )

    def test_title_author_manual_override_resets_review_and_auto_matches_title(self):
        submission = self.make_final_submission(
            final_submission_id="MO002",
            final_submission_title="Manual Corrected Title",
            extracted_title="Wrong Title",
            extracted_authors="Wrong Author",
            title_author_source="built_in_extractor",
            title_author_review_status="review_ok",
            title_author_verified=True,
            extracted_title_verified=False,
            duplicate_author_review_status="review_ok",
            author_number_exception_approved=True,
            author_number_exception_reason="Allowed old count",
            author_number_exception_author_count=8,
            author_number_exception_approved_at=timezone.now(),
        )

        def fake_image(_pdf_path, _title, _authors, _source_label, target_dir):
            image_path = Path(target_dir) / "manual_override.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"image")
            return image_path, []

        with patch(
            "submissions.services.title_author_extraction.generate_text_verification_image",
            side_effect=fake_image,
        ):
            apply_title_author_manual_override(
                submission,
                "Manual Corrected Title",
                "Ada Lovelace and Alan Turing",
                "Extractor failed on author formatting.",
            )

        submission.refresh_from_db()
        self.assertEqual(submission.extracted_title, "Manual Corrected Title")
        self.assertEqual(submission.extracted_authors, "Ada Lovelace and Alan Turing")
        self.assertEqual(submission.title_author_source, "manual_override")
        self.assertEqual(submission.title_author_manual_override_reason, "Extractor failed on author formatting.")
        self.assertIsNotNone(submission.title_author_manual_override_at)
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertFalse(submission.title_author_verified)
        self.assertTrue(submission.extracted_title_verified)
        self.assertEqual(submission.extracted_title_match_status, "verified")
        self.assertEqual(submission.duplicate_author_review_status, "pending")
        self.assertFalse(submission.author_number_exception_approved)
        self.assertTrue(Path(submission.title_author_verification_image).exists())
        self.assertIn("manual_override", Path(submission.title_author_verification_image).name)
        event = self.latest_audit_event("title_author_manual_override")
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["after"]["reason"], "Extractor failed on author formatting.")

    def test_manual_override_is_info_report_and_not_publication_blocker_after_review(self):
        self.make_master_paper("MO003", title="Manual Info Title")
        submission = self.make_final_submission(
            final_submission_id="MO003-F",
            paper_id_filled="MO003",
            start2_paper_id_raw="MO003",
            final_submission_title="Manual Info Title",
            paper_id_verified=True,
            verification_status="verified",
        )
        apply_title_author_manual_override(
            submission,
            "Manual Info Title",
            "Ada Lovelace and Alan Turing",
            "Allowed manual correction.",
        )
        verify_title_author(submission)

        categories = {row["category"] for row in error_report_rows()}
        self.assertIn("Manual Title/Author Override", categories)
        self.assertEqual(publication_readiness_rows(), [])
        self.assertTrue(Path(export_publication_package()).exists())

    def test_title_author_page_shows_manual_override_form_and_badge(self):
        self.make_master_paper("P001", "Manual", "Ada")
        submission = self.make_final_submission(
            final_submission_id="MO004",
            paper_id_filled="P001",
            title_author_source="manual_override",
            title_author_manual_override_reason="Existing override.",
        )

        response = self.client.get(
            reverse("submissions:title_author_extraction") + f"?filter=all&q={submission.final_submission_id}"
        )

        self.assertContains(response, "Manual override")
        self.assertContains(response, "Loading manual override form...")
        self.assertNotContains(response, "Exception workflow")
        self.assertNotContains(response, "Reason required")

        partial = self.client.get(
            reverse(
                "submissions:title_author_manual_override_form",
                args=[submission.pk],
            )
        )
        self.assertContains(partial, "Exception workflow")
        self.assertContains(partial, "Reason required")
        self.assertContains(partial, "Existing override.")

    def test_title_author_page_filters_manual_override_rows(self):
        self.make_master_paper("P001", "Manual", "Ada")
        self.make_master_paper("P002", "Built In", "Ada")
        self.make_final_submission(
            final_submission_id="MOFILTER1",
            paper_id_filled="P001",
            title_author_source="manual_override",
            title_author_manual_override_reason="Existing override.",
        )
        self.make_final_submission(
            final_submission_id="MOFILTER2",
            paper_id_filled="P002",
            title_author_source="built_in_extractor",
        )

        response = self.client.get(
            reverse("submissions:title_author_extraction") + "?filter=manual_override"
        )

        self.assertContains(response, "Manual Override")
        self.assertContains(response, "MOFILTER1")
        self.assertNotContains(response, "MOFILTER2")

    def test_title_author_verification_image_url_includes_cache_buster(self):
        submission = self.make_final_submission(final_submission_id="G000")
        image_path = self.media_root / "title_author_verification" / "G000" / "G000-grobid.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"image")
        submission.title_author_verification_image = str(image_path)

        url = verification_image_url(submission)

        self.assertIn("/media/title_author_verification/G000/G000-grobid.png", url)
        self.assertIn("?v=", url)

    def test_grobid_tei_parser_extracts_title_and_multiline_authors(self):
        tei = """
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader>
            <fileDesc>
              <titleStmt><title>Bridging Disciplines</title></titleStmt>
              <sourceDesc>
                <biblStruct>
                  <analytic>
                    <author><persName><forename>Jaejoon</forename><surname>Lee</surname></persName></author>
                    <author><persName><forename>Stuart</forename><surname>Nicholson</surname></persName></author>
                    <author><persName><forename>Srilatha</forename><surname>Narayanagari</surname></persName></author>
                    <author><persName><forename>Kay</forename><surname>Bond</surname></persName></author>
                  </analytic>
                </biblStruct>
              </sourceDesc>
            </fileDesc>
          </teiHeader>
        </TEI>
        """

        result = parse_grobid_tei(tei)

        self.assertEqual(result.title, "Bridging Disciplines")
        self.assertEqual(
            result.authors,
            "Jaejoon Lee, Stuart Nicholson, Srilatha Narayanagari, and Kay Bond",
        )
        self.assertEqual(result.author_count, 4)

    def test_grobid_tei_parser_rejects_missing_authors(self):
        tei = """
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader><fileDesc><titleStmt><title>Only Title</title></titleStmt></fileDesc></teiHeader>
        </TEI>
        """

        with self.assertRaises(GrobidExtractionError):
            parse_grobid_tei(tei)

    def test_grobid_verification_image_uses_source_labeled_filename(self):
        import fitz

        pdf_path = self.root / "grobid_visual.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((120, 100), "GROBID Visual Paper", fontsize=18)
        page.insert_text((120, 140), "Ada Lovelace and Alan Turing", fontsize=12)
        source_page_height = page.rect.height
        document.save(pdf_path)
        document.close()

        output_path, missing_authors = generate_text_verification_image(
            pdf_path,
            "GROBID Visual Paper",
            "Ada Lovelace and Alan Turing",
            "GROBID",
            self.media_root / "title_author_verification" / "GROBIDVIS",
        )

        self.assertEqual(output_path.name, "grobid_visual-grobid.png")
        self.assertTrue(output_path.exists())
        self.assertEqual(missing_authors, [])
        output_pixmap = fitz.Pixmap(output_path)
        source_crop_height = int((source_page_height / 3) * (300 / 72))
        self.assertGreater(output_pixmap.height, source_crop_height)
        submission = self.make_final_submission(final_submission_id="GROBIDVIS")
        submission.title_author_verification_image = str(output_path)
        self.assertEqual(
            verification_image_dimensions(submission),
            {"width": output_pixmap.width, "height": output_pixmap.height},
        )

    def test_verification_image_layout_keeps_long_header_content_separate(self):
        from submissions.services.title_author_verification import (
            HEADER_TO_CONTENT_MARGIN,
            _build_header_layout,
            _source_offset,
        )

        layout = _build_header_layout(
            360,
            "EDITOR-PAPER-WITH-A-VERY-LONG-FILENAME_file_Submit_PDF.pdf",
            (
                "A Very Long Extracted Title That Must Wrap Without Covering "
                "The Author Evidence Or The PDF Content Below"
            ),
            [
                "First Long Author Name",
                "Second Long Author Name",
                "Third Long Author Name",
                "Fourth Long Author Name",
            ],
            "MANUAL OVERRIDE",
        )

        self.assertGreater(layout["title_label_top"], layout["filename_top"])
        self.assertGreater(layout["authors_label_top"], layout["title_top"])
        self.assertGreater(layout["height"], layout["authors_top"])
        self.assertGreater(len(layout["title_lines"]), 1)
        source_offset = _source_offset(layout["height"], 80)
        self.assertGreaterEqual(
            source_offset + 80,
            layout["height"] + HEADER_TO_CONTENT_MARGIN,
        )

    def test_verification_image_reuses_safe_pdf_top_whitespace(self):
        import fitz

        def create_pdf(path, title_y, author_y):
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, title_y), "Whitespace Review Paper", fontsize=18)
            page.insert_text((72, author_y), "Ada Lovelace and Alan Turing", fontsize=12)
            document.save(path)
            document.close()

        crowded_pdf = self.root / "crowded_verification.pdf"
        spacious_pdf = self.root / "spacious_verification.pdf"
        create_pdf(crowded_pdf, 28, 56)
        create_pdf(spacious_pdf, 220, 252)

        crowded_output, crowded_missing = generate_text_verification_image(
            crowded_pdf,
            "Whitespace Review Paper",
            "Ada Lovelace and Alan Turing",
            "GROBID",
            self.media_root / "title_author_verification" / "CROWDED",
        )
        spacious_output, spacious_missing = generate_text_verification_image(
            spacious_pdf,
            "Whitespace Review Paper",
            "Ada Lovelace and Alan Turing",
            "GROBID",
            self.media_root / "title_author_verification" / "SPACIOUS",
        )

        self.assertEqual(crowded_missing, [])
        self.assertEqual(spacious_missing, [])
        crowded_pixmap = fitz.Pixmap(crowded_output)
        spacious_pixmap = fitz.Pixmap(spacious_output)
        self.assertLess(spacious_pixmap.height, crowded_pixmap.height)

    def test_grobid_success_resets_review_flags_and_creates_verification_image(self):
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.grobid_api_url = "http://192.168.111.10:8070"
        settings_obj.save()
        submission = self.make_final_submission(
            final_submission_id="G001",
            final_submission_title="GROBID Paper",
            extracted_title="Old Title",
            extracted_authors="Old Author",
            title_author_source="manual_override",
            title_author_manual_override_reason="Old override",
            title_author_manual_override_at=timezone.now(),
            title_author_review_status="review_ok",
            title_author_verified=True,
            extracted_title_verified=True,
            duplicate_author_review_status="review_ok",
            author_number_exception_approved=True,
            author_number_exception_reason="Allowed old count",
            author_number_exception_author_count=9,
            author_number_exception_approved_at=timezone.now(),
        )

        def fake_image(_pdf_path, _title, _authors, _source_label, target_dir):
            image_path = Path(target_dir) / "grobid.png"
            image_path.write_bytes(b"image")
            return image_path, []

        with patch(
            "submissions.services.title_author_extraction.check_grobid_api",
            return_value={
                "available": True,
                "level": "success",
                "label": "Available",
                "message": "GROBID API is reachable.",
            },
        ), patch(
            "submissions.services.title_author_extraction.extract_header_with_grobid",
            return_value=GrobidExtractionResult(
                title="GROBID Paper",
                authors="Ada Lovelace and Alan Turing",
                author_count=2,
                raw_tei="<TEI/>",
            ),
        ), patch(
            "submissions.services.title_author_extraction.generate_text_verification_image",
            side_effect=fake_image,
        ):
            self.assertTrue(extract_title_author_with_grobid(submission))

        submission.refresh_from_db()
        self.assertEqual(submission.extracted_title, "GROBID Paper")
        self.assertEqual(submission.extracted_authors, "Ada Lovelace and Alan Turing")
        self.assertEqual(submission.title_author_source, "grobid")
        self.assertEqual(submission.title_author_manual_override_reason, "")
        self.assertIsNone(submission.title_author_manual_override_at)
        self.assertEqual(submission.title_author_review_status, "pending")
        self.assertFalse(submission.title_author_verified)
        self.assertTrue(submission.extracted_title_verified)
        self.assertEqual(submission.extracted_title_match_status, "verified")
        self.assertEqual(submission.duplicate_author_review_status, "pending")
        self.assertFalse(submission.author_number_exception_approved)
        self.assertTrue(Path(submission.title_author_verification_image).exists())

    def test_built_in_reextract_clears_manual_override_metadata(self):
        submission = self.make_final_submission(
            final_submission_id="MO005",
            final_submission_title="Built In Title",
            title_author_source="manual_override",
            title_author_manual_override_reason="Old override",
            title_author_manual_override_at=timezone.now(),
        )

        def fake_get_title_author(pdf_path, verify=False, verify_folder=""):
            self.assertFalse(verify)
            return "Built In Title", "Ada Lovelace and Alan Turing", 2

        def fake_image(_pdf_path, _title, _authors, source_label, target_dir):
            self.assertEqual(source_label, "BUILT-IN")
            image_path = Path(target_dir) / "built-in.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"image")
            return image_path, []

        with patch(
            "submissions.services.title_author_extraction.get_title_author",
            side_effect=fake_get_title_author,
        ), patch(
            "submissions.services.title_author_extraction.generate_text_verification_image",
            side_effect=fake_image,
        ):
            self.assertTrue(extract_title_author_for_submission(submission))

        submission.refresh_from_db()
        self.assertEqual(submission.title_author_source, "built_in_extractor")
        self.assertEqual(submission.title_author_manual_override_reason, "")
        self.assertIsNone(submission.title_author_manual_override_at)

    def test_grobid_failure_does_not_modify_existing_extraction(self):
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()
        submission = self.make_final_submission(
            final_submission_id="G002",
            extracted_title="Existing Title",
            extracted_authors="Existing Author",
            title_author_source="manual",
            title_author_review_status="review_ok",
            title_author_verified=True,
        )

        with patch(
            "submissions.services.title_author_extraction.check_grobid_api",
            return_value={
                "available": True,
                "level": "success",
                "label": "Available",
                "message": "GROBID API is reachable.",
            },
        ), patch(
            "submissions.services.title_author_extraction.extract_header_with_grobid",
            side_effect=GrobidExtractionError("connection failed"),
        ):
            self.assertFalse(extract_title_author_with_grobid(submission))

        submission.refresh_from_db()
        self.assertEqual(submission.extracted_title, "Existing Title")
        self.assertEqual(submission.extracted_authors, "Existing Author")
        self.assertEqual(submission.title_author_source, "manual")
        self.assertEqual(submission.title_author_review_status, "review_ok")
        self.assertTrue(submission.title_author_verified)

    def test_grobid_unavailable_does_not_modify_submission_or_call_extractor(self):
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()
        submission = self.make_final_submission(
            final_submission_id="G002A",
            extracted_title="Existing Title",
            extracted_authors="Existing Author",
            title_author_source="built_in_extractor",
            title_author_review_status="review_ok",
            title_author_verified=True,
        )

        with patch(
            "submissions.services.title_author_extraction.check_grobid_api",
            return_value={
                "available": False,
                "level": "danger",
                "label": "Unavailable",
                "message": "GROBID API is not reachable.",
            },
        ), patch(
            "submissions.services.title_author_extraction.extract_header_with_grobid"
        ) as mocked_extract:
            self.assertFalse(extract_title_author_with_grobid(submission))

        mocked_extract.assert_not_called()
        submission.refresh_from_db()
        self.assertEqual(submission.extracted_title, "Existing Title")
        self.assertEqual(submission.extracted_authors, "Existing Author")
        self.assertEqual(submission.title_author_review_status, "review_ok")
        self.assertTrue(submission.title_author_verified)
        self.assertIn("GROBID API is unavailable", submission._last_grobid_error)

    def test_grobid_batch_only_processes_suspicious_rows(self):
        self.make_master_paper("P001", "Suspicious", "Ada")
        self.make_master_paper("P002", "Clean", "Ada")
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()
        suspicious = self.make_final_submission(
            final_submission_id="G003",
            paper_id_filled="P001",
            title_author_extraction_status="error",
            title_author_review_status="pending",
        )
        clean = self.make_final_submission(
            final_submission_id="G004",
            paper_id_filled="P002",
            extracted_authors="Ada Lovelace and Alan Turing",
            title_author_review_status="review_ok",
            title_author_verified=True,
        )

        self.assertTrue(is_grobid_suspicious(suspicious))
        self.assertFalse(is_grobid_suspicious(clean))
        clean.extracted_authors = "Jaejoon Lee, and Kay"
        clean.title_author_review_status = "pending"
        clean.title_author_verified = False
        clean.save(
            update_fields=[
                "extracted_authors",
                "title_author_review_status",
                "title_author_verified",
                "updated_at",
            ]
        )
        self.assertFalse(is_grobid_suspicious(clean))
        with patch(
            "submissions.services.title_author_extraction.check_grobid_api",
            return_value={
                "available": True,
                "level": "success",
                "label": "Available",
                "message": "GROBID API is reachable.",
            },
        ), patch(
            "submissions.services.title_author_extraction.extract_title_author_with_grobid",
            return_value=True,
        ) as mocked_extract:
            result = extract_grobid_for_suspicious_rows()

        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(mocked_extract.call_args.args[0].pk, suspicious.pk)

    def test_grobid_batch_unavailable_aborts_without_processing_rows(self):
        self.make_master_paper("P001", "Suspicious", "Ada")
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()
        self.make_final_submission(
            final_submission_id="G003A",
            paper_id_filled="P001",
            title_author_extraction_status="error",
            title_author_review_status="pending",
        )

        with patch(
            "submissions.services.title_author_extraction.check_grobid_api",
            return_value={
                "available": False,
                "level": "danger",
                "label": "Unavailable",
                "message": "GROBID API is not reachable.",
            },
        ), patch(
            "submissions.services.title_author_extraction.extract_title_author_with_grobid"
        ) as mocked_extract:
            result = extract_grobid_for_suspicious_rows()

        mocked_extract.assert_not_called()
        self.assertTrue(result["aborted"])
        self.assertEqual(result["extracted"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("GROBID API is unavailable", result["message"])

    def test_grobid_batch_stops_when_service_drops_mid_run(self):
        for index in range(1, 4):
            self.make_master_paper(f"P00{index}", f"Suspicious {index}", "Ada")
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()
        first = self.make_final_submission(
            final_submission_id="GSTOP1",
            paper_id_filled="P001",
            title_author_extraction_status="error",
            title_author_review_status="pending",
        )
        second = self.make_final_submission(
            final_submission_id="GSTOP2",
            paper_id_filled="P002",
            title_author_extraction_status="error",
            title_author_review_status="pending",
        )
        third = self.make_final_submission(
            final_submission_id="GSTOP3",
            paper_id_filled="P003",
            title_author_extraction_status="error",
            title_author_review_status="pending",
        )

        def fake_extract(submission, refresh_author_cache=True, skip_health_check=False):
            if submission.pk == first.pk:
                return True
            if submission.pk == second.pk:
                submission._last_grobid_error = "GROBID request timed out."
                submission._last_grobid_service_unavailable = True
                return False
            raise AssertionError("Batch should stop before processing the third row.")

        with patch(
            "submissions.services.title_author_extraction.check_grobid_api",
            return_value={
                "available": True,
                "level": "success",
                "label": "Available",
                "message": "GROBID API is reachable.",
            },
        ), patch(
            "submissions.services.title_author_extraction.extract_title_author_with_grobid",
            side_effect=fake_extract,
        ) as mocked_extract:
            result = extract_grobid_for_suspicious_rows()

        self.assertEqual(mocked_extract.call_count, 2)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertIn("timed out", result["message"])

    def test_title_author_page_shows_grobid_controls_only_when_enabled(self):
        self.make_master_paper("P001", "GROBID Paper", "Ada")
        self.make_final_submission(final_submission_id="G005", paper_id_filled="P001", title_author_review_status="pending")

        response = self.client.get(reverse("submissions:title_author_extraction"))
        self.assertNotContains(response, "Try GROBID")

        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()

        response = self.client.get(reverse("submissions:title_author_extraction"))
        self.assertContains(response, "GROBID fallback")
        self.assertContains(response, "Try suspicious rows")
        self.assertContains(response, "Try GROBID")

    def test_title_author_page_warns_review_ok_grobid_resets_review(self):
        self.make_master_paper("P001", "GROBID Paper", "Ada")
        settings_obj = AppSetting.load()
        settings_obj.grobid_enabled = True
        settings_obj.save()
        self.make_final_submission(
            final_submission_id="G006",
            paper_id_filled="P001",
            title_author_review_status="review_ok",
            title_author_verified=True,
        )

        response = self.client.get(reverse("submissions:title_author_extraction") + "?filter=review_ok")

        self.assertContains(response, "GROBID re-extract")
        self.assertContains(response, "GROBID re-extract resets review status.")

    def test_title_author_page_lazy_loads_images_and_manual_override_form(self):
        self.make_master_paper("P001", "Deferred Review Paper", "Ada")
        submission = self.make_final_submission(
            final_submission_id="DEFER1",
            paper_id_filled="P001",
            extracted_title="Deferred Review Paper",
            extracted_authors="Ada Lovelace",
            title_author_manual_override_reason="Existing editorial reason",
        )
        image_path = (
            self.media_root
            / "title_author_verification"
            / "DEFER1"
            / "DEFER1.png"
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"verification image")
        submission.title_author_verification_image = str(image_path)
        submission.save(update_fields=["title_author_verification_image", "updated_at"])

        response = self.client.get(
            reverse("submissions:title_author_extraction"),
            {"filter": "all", "q": "DEFER1"},
        )

        self.assertContains(response, 'loading="lazy"')
        self.assertContains(response, 'decoding="async"')
        self.assertContains(response, "data-cfm-image-magnifier")
        self.assertContains(
            response,
            'data-cfm-image-magnifier-hint="Hold Ctrl to magnify"',
        )
        self.assertNotContains(response, 'title="Hold Ctrl to magnify"')
        self.assertContains(response, "/static/submissions/image_magnifier.js")
        self.assertContains(response, "/static/submissions/image_magnifier.css")
        self.assertContains(response, 'width="2550"')
        self.assertContains(response, 'height="1100"')
        self.assertContains(
            response,
            reverse(
                "submissions:title_author_manual_override_form",
                args=[submission.pk],
            ),
        )
        self.assertNotContains(response, 'name="manual_extracted_title"')
        self.assertNotContains(response, 'name="manual_extracted_authors"')
        self.assertNotContains(response, 'name="manual_override_reason"')

        partial = self.client.get(
            reverse(
                "submissions:title_author_manual_override_form",
                args=[submission.pk],
            )
        )

        self.assertEqual(partial.status_code, 200)
        self.assertContains(partial, 'name="manual_extracted_title"')
        self.assertContains(partial, "Deferred Review Paper")
        self.assertContains(partial, 'name="manual_extracted_authors"')
        self.assertContains(partial, "Ada Lovelace")
        self.assertContains(partial, "Existing editorial reason")

    def test_title_author_page_emphasizes_title_match_check(self):
        self.make_master_paper("P001", "Bridging Disciplines", "Ada")
        submission = self.make_final_submission(
            final_submission_id="TMVIS1",
            paper_id_filled="P001",
            final_submission_title="Bridging Disciplines",
            extracted_title="Bridging Disciplines",
            extracted_title_verified=False,
            extracted_title_auto_verify_blocked=True,
        )
        submission.title_author_review_status = "pending"
        submission.save(
            update_fields=[
                "title_author_review_status",
                "extracted_title_verified",
                "extracted_title_auto_verify_blocked",
                "updated_at",
            ]
        )

        response = self.client.get(
            reverse("submissions:title_author_extraction") + "?filter=all&q=TMVIS1"
        )

        self.assertContains(response, "Title Comparison")
        self.assertContains(response, "Review with title/authors")
        self.assertContains(response, "Score 100%")
        self.assertNotContains(response, "Confirm match")
        self.assertContains(response, "Review OK")
        self.assertNotContains(response, "Extracted Title Diff Against Final Title")

    def test_title_author_page_filters_review_ok_and_needs_review(self):
        self.make_master_paper("TM001", "Matched Title", "Ada")
        self.make_master_paper("TM002", "Needs Match Title", "Ada")
        self.make_final_submission(
            final_submission_id="TM001",
            paper_id_filled="TM001",
            final_submission_title="Matched Title",
            extracted_title="Matched Title",
            title_author_review_status="review_ok",
            title_author_verified=True,
            extracted_title_verified=True,
        )
        self.make_final_submission(
            final_submission_id="TM002",
            paper_id_filled="TM002",
            final_submission_title="Needs Match Title",
            extracted_title="Needs Match Title",
            title_author_review_status="pending",
            title_author_verified=False,
            extracted_title_verified=False,
        )

        matched = self.client.get(reverse("submissions:title_author_extraction") + "?filter=review_ok")
        self.assertContains(matched, "Review OK")
        self.assertContains(matched, "TM001")
        self.assertNotContains(matched, "TM002")

        needs_match = self.client.get(
            reverse("submissions:title_author_extraction") + "?filter=needs_verification"
        )
        self.assertContains(needs_match, "Needs Review")
        self.assertContains(needs_match, "TM002")
        self.assertNotContains(needs_match, "TM001")

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
            title_author_review_status="pending",
            title_author_verified=False,
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
        self.assertEqual(summary["unverified"], 1)
        self.assertEqual(summary["verified_title_differences"], 1)
        self.assertEqual(summary["page_errors"], 1)
        self.assertEqual(summary["missing_plagiarism"], 0)
        self.assertEqual(summary["plagiarism_issues"], 1)

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
        self.assertContains(response, "P/S Issues")

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
        self.assertContains(response, "Publication files")
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

    def test_organized_list_details_integrates_publication_authors_files_and_notes(self):
        self.make_master_paper(
            "P001",
            "Publication Record",
            "Master Metadata Author",
            notes="Check the publisher category before release.",
        )
        submission = self.make_final_submission(
            final_submission_id="EDITOR-P001-001",
            paper_id_filled="P001",
            final_submission_title="Publication Record",
            final_submission_authors="Final Metadata Author",
            extracted_title="Publication Record",
            extracted_authors=(
                "Vasile Rus and Panayiota Kendeou and Matthew L. Bernacki "
                "and Amy Cook and Andrew Tawfik"
            ),
            title_author_source="grobid",
            title_author_review_status="review_ok",
            title_author_verified=True,
            submission_origin="editor_upload",
            editor_upload_notes="Author supplied this version by email.",
        )

        response = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Publication record")
        self.assertContains(response, "Publication metadata")
        self.assertContains(response, "Publication authors")
        self.assertNotContains(response, "Publication content")
        self.assertContains(response, "Publication files")
        self.assertContains(response, "Vasile Rus")
        self.assertContains(response, "Panayiota Kendeou")
        self.assertContains(response, "Matthew L. Bernacki")
        self.assertContains(response, "Amy Cook")
        self.assertContains(response, "Andrew Tawfik")
        self.assertContains(response, "5 authors")
        self.assertContains(response, 'class="cfm-publication-author"', count=5)
        self.assertContains(response, "GROBID")
        self.assertContains(response, "Open Title/Author Review")
        self.assertContains(
            response,
            f'{reverse("submissions:title_author_extraction")}?submission={submission.pk}',
            html=False,
        )
        self.assertContains(response, "Check the publisher category before release.")
        self.assertContains(response, "Author supplied this version by email.")
        self.assertContains(response, "Technical details")
        self.assertContains(response, reverse("submissions:publication_pdf", args=[submission.pk]))
        self.assertContains(response, reverse("submissions:publication_source", args=[submission.pk]))

        rows, _summary, _settings_obj, _current_filter, _current_sort = organized_list_rows(
            query="P001",
            current_filter="all",
        )
        self.assertEqual(
            rows[0]["author_display_items"],
            [
                {"order": 1, "name": "Vasile Rus"},
                {"order": 2, "name": "Panayiota Kendeou"},
                {"order": 3, "name": "Matthew L. Bernacki"},
                {"order": 4, "name": "Amy Cook"},
                {"order": 5, "name": "Andrew Tawfik"},
            ],
        )

    def test_organized_list_details_marks_missing_publication_authors(self):
        self.make_master_paper("P001", "Missing Authors", "Master Metadata Author")
        self.make_final_submission(
            final_submission_id="100",
            paper_id_filled="P001",
            final_submission_title="Missing Authors",
            extracted_title="Missing Authors",
            extracted_authors="",
            title_author_review_status="pending",
            title_author_verified=False,
        )

        response = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No extracted authors")

    def test_navbar_prioritizes_organized_list_and_groups_editorial_work(self):
        response = self.client.get(reverse("submissions:organized_list"))
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'href="{reverse("submissions:organized_list")}" aria-current="page">Organized List</a>',
            html,
        )
        self.assertIn("Submissions\n                    </a>", html)
        self.assertIn("Reviews\n                    </a>", html)
        self.assertIn("Reports &amp; Output\n                    </a>", html)
        self.assertIn("Administration\n                    </a>", html)
        self.assertNotIn("Workflow\n                    </a>", html)
        self.assertNotIn("Integrations\n                    </a>", html)
        self.assertIn("Official publication scope and editorial notes", html)
        self.assertIn("Trace state-changing editorial actions", html)
        self.assertIn('class="navbar navbar-expand-xl cfm-primary-nav"', html)
        self.assertIn('class="navbar-toggler d-xl-none"', html)
        self.assertIn('aria-label="Toggle navigation"', html)
        self.assertIn(reverse("submissions:editor_upload"), html)
        self.assertIn(reverse("submissions:integration"), html)
        self.assertIn(reverse("submissions:system_state"), html)

        system_response = self.client.get(reverse("submissions:system_state"))
        self.assertContains(system_response, "System Backup / Restore")
        self.assertNotContains(system_response, "Prepare CrossCheck PDFs")
        crosscheck_response = self.client.get(reverse("submissions:integration"))
        self.assertContains(crosscheck_response, "Prepare CrossCheck PDFs")
        self.assertNotContains(crosscheck_response, "Restore System State")

    def test_base_layout_keeps_footer_at_viewport_bottom_on_short_pages(self):
        response = self.client.get(reverse("submissions:organized_list"))
        html = response.content.decode()

        self.assertIn("min-height: 100vh", html)
        self.assertIn("flex: 1 0 auto", html)
        self.assertIn("flex-shrink: 0", html)

    def test_tables_use_uniform_rows_with_hover_instead_of_zebra_striping(self):
        for index in (1, 2):
            paper_id = f"P{index:03d}"
            self.make_master_paper(paper_id, f"Paper {index}", "Ada Lovelace")
            self.make_final_submission(
                final_submission_id=str(index * 10),
                paper_id_filled=paper_id,
                final_submission_title=f"Paper {index}",
                extracted_title=f"Paper {index}",
                active_version=True,
            )
            self.make_final_submission(
                final_submission_id=str(index * 10 - 1),
                paper_id_filled=paper_id,
                final_submission_title=f"Old Paper {index}",
                extracted_title=f"Old Paper {index}",
                active_version=False,
                discarded=True,
                discard_notes=f"Old version {index}",
            )

        organized = self.client.get(reverse("submissions:organized_list"))
        final_submissions = self.client.get(reverse("submissions:final_submission_list"))
        old_versions = self.client.get(reverse("submissions:old_versions"))

        rows, _summary, _settings, _current_filter, _current_sort = organized_list_rows()
        clean_row = next(row for row in rows if row["paper"] and row["paper"].paper_id == "P001")
        self.assertEqual(clean_row["author_count_level"], "secondary")
        self.assertEqual(clean_row["page_level"], "secondary")
        self.assertEqual(clean_row["source_level"], "secondary")

        for response in (organized, final_submissions, old_versions):
            self.assertNotContains(response, "table-striped")
            self.assertNotContains(response, "cfm-stripe-")
            self.assertContains(response, "table-hover")
        self.assertContains(organized, 'class="collapse"')
        self.assertContains(final_submissions, 'class="collapse"')
        self.assertContains(old_versions, 'class="collapse"')

    def test_dashboard_plagiarism_threshold_count_is_paper_count_not_score_count(self):
        self.make_master_paper("P001", "Both Scores High", "Ada")
        self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Both Scores High",
            extracted_title="Both Scores High",
            similarity_score=42,
            single_similarity_score=12,
        )
        self.make_master_paper("P002", "Single Score High", "Ada")
        self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Single Score High",
            extracted_title="Single Score High",
            similarity_score=4,
            single_similarity_score=12,
        )

        counts = dashboard_counts()
        self.assertEqual(counts["plagiarism_over_threshold"], 1)
        self.assertEqual(counts["single_over_threshold"], 2)
        self.assertEqual(counts["plagiarism_threshold_issue_papers"], 2)

        response = self.client.get(reverse("submissions:dashboard_summary"))
        self.assertContains(response, "2 papers over threshold")
        self.assertNotContains(response, "3 papers over threshold")

    def test_dashboard_counts_only_active_invalid_ids_and_tracks_verified_title_differences(self):
        self.make_master_paper("P001", "Paper Master Title", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Different Final Title",
            extracted_title="Different Final Title",
            paper_id_verified=True,
            verification_status="verified",
        )
        self.make_final_submission(
            final_submission_id="9",
            paper_id_filled="INVALID-OLD",
            final_submission_title="Old Invalid Version",
            extracted_title="Old Invalid Version",
            active_version=False,
        )

        counts = dashboard_counts()

        self.assertEqual(counts["invalid_paper_ids"], 0)
        self.assertEqual(counts["title_mismatches"], 0)
        self.assertEqual(counts["verified_title_differences"], 1)

    def test_dashboard_title_author_attention_counts_each_paper_once(self):
        self.make_master_paper("P001", "Review Metadata", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Review Metadata",
            extracted_title="Review Metadata",
            extracted_authors="",
            title_author_review_status="pending",
            title_author_verified=False,
            extracted_title_verified=False,
        )

        counts = dashboard_counts()

        self.assertEqual(counts["missing_title_author_extraction"], 1)
        self.assertEqual(counts["title_author_attention_papers"], 1)

    def test_dashboard_uses_the_same_blockers_as_publication_export(self):
        self.make_master_paper("P001", "Needs Metadata Review", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Needs Metadata Review",
            extracted_title="Needs Metadata Review",
            extracted_authors="Ada",
            title_author_review_status="pending",
            title_author_verified=False,
        )
        readiness_rows = publication_readiness_rows()

        response = self.client.get(reverse("submissions:dashboard_summary"))

        self.assertEqual(
            response.context["readiness"]["blocking_issue_count"],
            len(readiness_rows),
        )
        self.assertContains(response, "Final package is blocked")
        self.assertContains(response, "Title and author review")
        self.assertNotContains(response, "System Overview")
        self.assertNotContains(response, "refresh active/old publication files")

    def test_dashboard_tracks_reviewed_extracted_title_differences(self):
        self.make_master_paper("P001", "Title Match Review", "Ada")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Title Match Review Extended",
            extracted_title="Title Match Review",
            title_author_review_status="review_ok",
            title_author_verified=True,
            extracted_title_verified=False,
            format_status="review_ok",
        )

        response = self.client.get(reverse("submissions:dashboard_summary"))

        self.assertEqual(dashboard_counts()["unverified_extracted_title_match"], 0)
        self.assertEqual(dashboard_counts()["reviewed_extracted_title_differences"], 1)
        self.assertContains(response, "Reviewed extracted-title differences")
        self.assertNotContains(response, "Extracted title match review")

    def test_organized_list_exception_panel_sections_are_relevant_and_actionable(self):
        self.make_master_paper("P001", "Clean", "Ada")
        clean = self.make_final_submission(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Clean",
            extracted_title="Clean",
        )
        self.make_master_paper("P002", "Page Issue", "Ada")
        page_issue = self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Page Issue",
            extracted_title="Page Issue",
            page_count=13,
        )
        self.make_master_paper("P003", "Author Issue", "Ada")
        author_issue = self.make_final_submission(
            final_submission_id="3",
            paper_id_filled="P003",
            final_submission_title="Author Issue",
            extracted_title="Author Issue",
            extracted_authors="A; B; C; D; E; F",
            final_submission_authors="A; B; C; D; E; F",
        )
        self.make_master_paper("P004", "Plagiarism Issue", "Ada")
        plagiarism_issue = self.make_final_submission(
            final_submission_id="4",
            paper_id_filled="P004",
            final_submission_title="Plagiarism Issue",
            extracted_title="Plagiarism Issue",
            similarity_score=42,
            single_similarity_score=4,
        )
        self.make_master_paper("P005", "Duplicate Author", "Ada")
        duplicate_author = self.make_final_submission(
            final_submission_id="5",
            paper_id_filled="P005",
            final_submission_title="Duplicate Author",
            extracted_title="Duplicate Author",
            extracted_authors="Ada Lovelace, Alan Turing, Ada Lovelace",
        )

        rows, _summary, _settings_obj, _current_filter, _current_sort = organized_list_rows(
            current_filter="all"
        )
        by_id = {
            row["paper"].paper_id if row["paper"] else row["submission"].paper_id_filled: row
            for row in rows
        }
        self.assertEqual(by_id["P001"]["exception_panel_sections"], [])
        self.assertEqual(
            [section["title"] for section in by_id["P002"]["exception_panel_sections"]],
            ["Page count exception"],
        )
        self.assertEqual(
            [section["title"] for section in by_id["P003"]["exception_panel_sections"]],
            ["Authors in paper exception"],
        )
        self.assertEqual(
            [section["title"] for section in by_id["P004"]["exception_panel_sections"]],
            ["Plagiarism % exception"],
        )
        self.assertEqual(
            [section["title"] for section in by_id["P005"]["exception_panel_sections"]],
            ["Duplicate author review"],
        )

        response = self.client.get(reverse("submissions:organized_list"), {"filter": "all"})
        self.assertContains(response, "Manage exceptions")
        self.assertContains(response, "Page count exception")
        self.assertContains(response, "Authors in paper exception")
        self.assertContains(response, "Plagiarism % exception")
        self.assertContains(response, "Duplicate author review")
        self.assertContains(response, "Open publication PDF")

        url = reverse("submissions:organized_list") + "?filter=page_issues&sort=page_count_desc&q=P002"
        response = self.client.post(
            url,
            {
                "submission_id": page_issue.pk,
                "exception_key": f"page:{page_issue.pk}",
                "action": "approve_exception",
                "reason": "Chair approved page count.",
                "evidence_token": self.exception_evidence_token(
                    f"page:{page_issue.pk}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, url)
        page_issue.refresh_from_db()
        self.assertTrue(page_issue.has_valid_page_limit_exception)

        response = self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": plagiarism_issue.pk,
                "exception_key": f"plagiarism_percent:{plagiarism_issue.pk}",
                "action": "approve_exception",
                "evidence_token": self.exception_evidence_token(
                    f"plagiarism_percent:{plagiarism_issue.pk}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        plagiarism_issue.refresh_from_db()
        self.assertFalse(plagiarism_issue.plagiarism_percent_exception_approved)

        response = self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": author_issue.pk,
                "exception_key": f"author_number:{author_issue.pk}",
                "action": "approve_exception",
                "reason": "Panel paper approved.",
                "evidence_token": self.exception_evidence_token(
                    f"author_number:{author_issue.pk}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        author_issue.refresh_from_db()
        self.assertTrue(author_issue.author_number_exception_approved)
        self.assertEqual(author_issue.author_number_exception_author_count, 6)

        response = self.client.post(
            reverse("submissions:organized_list"),
            {
                "submission_id": duplicate_author.pk,
                "action": "mark_duplicate_author_reviewed",
                "duplicate_author_review_notes": "Confirmed different people.",
                "evidence_token": self.duplicate_author_review_token(
                    duplicate_author
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        duplicate_author.refresh_from_db()
        self.assertEqual(duplicate_author.duplicate_author_review_status, "review_ok")

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
            "evidence_token": self.settings_evidence_token(),
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
            "grobid_api_url": settings_obj.grobid_api_url,
            "grobid_timeout_seconds": settings_obj.grobid_timeout_seconds,
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
        self.assertContains(response, "cfm-alert-stack")
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
        self.assertContains(response, "cfm-alert-stack")
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
            "evidence_token": self.settings_evidence_token(),
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
            "grobid_api_url": settings_obj.grobid_api_url,
            "grobid_timeout_seconds": settings_obj.grobid_timeout_seconds,
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
        submission.refresh_from_db()

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

        cache.clear()
        alerts = self.client.get(reverse("submissions:workflow_alerts"))
        dashboard = self.client.get(reverse("submissions:dashboard_summary"))
        self.assertContains(alerts, "Start2/Editor version decision needed")
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
            {
                "submission_id": submission.pk,
                "action": "discard_submission",
                "discard_notes": "",
                "evidence_token": self.version_decision_token(
                    submission
                ),
            },
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
                "evidence_token": self.version_decision_token(
                    submission
                ),
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
        self.assertEqual(master_page.context["total_paper_count"], 2)
        self.assertEqual(master_page.context["displayed_paper_count"], 2)
        self.assertContains(master_page, "in publication scope")
        self.assertContains(master_page, "Note Summary (1)")
        self.assertContains(master_page, "Check special session placement.")
        self.assertContains(master_page, "cfm-title-cell")

        filtered_master_page = self.client.get(
            reverse("submissions:initial_paper_list"),
            {"q": "Noted"},
        )
        self.assertEqual(filtered_master_page.context["displayed_paper_count"], 1)
        self.assertEqual(filtered_master_page.context["total_paper_count"], 2)
        self.assertContains(filtered_master_page, "shown")

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
        update_formatting_submission(
            corrected_source,
            {
                "corrected_pdf": None,
                "corrected_source": None,
                "format_status": "review_ok",
                "format_notes": "Corrected source reviewed.",
            },
        )
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
        self.assertContains(blocked, "cfm-alert-stack")
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
                "evidence_token": self.publication_decision_token(
                    excluded
                ),
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

        dashboard = self.client.get(reverse("submissions:dashboard_summary"))
        self.assertContains(dashboard, "Final package checks are clear")
        self.assertContains(dashboard, "Publication candidates")
        self.assertContains(dashboard, "Current Not Publishing")
        self.assertContains(dashboard, "Plagiarism results")
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
        self.assertNotIn("Unverified Extracted Title Match", categories)
        self.assertTrue(all("group" in row and "level" in row for row in rows))

        verify_title_author(submission)
        submission.refresh_from_db()
        self.assertTrue(submission.title_author_verified)
        self.assertTrue(submission.extracted_title_verified)
        unverify_title_author(submission)
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

    def test_evidence_signing_is_query_free_and_exception_tokens_are_page_bounded(self):
        submission = FinalSubmission.objects.order_by("pk").first()
        with self.assertNumQueries(0):
            token = make_evidence_token(
                "final-submission-edit",
                final_submission_edit_evidence(submission),
            )
            require_evidence_token(
                token,
                "final-submission-edit",
                final_submission_edit_evidence(submission),
            )

        for index in range(30):
            paper_id = f"PX{index:03d}"
            self.make_master_paper(
                paper_id=paper_id,
                title=f"Exception Paper {index}",
                authors="Ada",
            )
            self.make_final_submission(
                final_submission_id=f"X{index:03d}",
                paper_id_filled=paper_id,
                final_submission_title=f"Exception Paper {index}",
                extracted_title=f"Exception Paper {index}",
                page_count=13,
            )
        with patch(
            "submissions.services.workflow_evidence.make_evidence_token",
            wraps=make_evidence_token,
        ) as signer:
            response = self.client.get(
                reverse("submissions:exceptions_center"),
                {"filter": "all"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(signer.call_count, len(response.context["rows"]))
        self.assertLessEqual(signer.call_count, 25)

class ExactNavigationTests(EditorialAcceptanceTestCase):
    def test_exact_submission_focus_does_not_collide_with_similar_ids(self):
        self.make_master_paper(paper_id="PT007", title="Exact Target")
        self.make_master_paper(paper_id="R058", title="Similar Paper ID")
        target = self.make_final_submission(
            final_submission_id="58",
            paper_id_filled="PT007",
            final_submission_title="Exact Target",
            extracted_title="Exact Target",
        )
        other = self.make_final_submission(
            final_submission_id="33",
            paper_id_filled="R058",
            final_submission_title="Similar Paper ID",
            extracted_title="Similar Paper ID",
        )

        focused_pages = [
            ("verify_paper_ids", {"submission": target.pk}),
            ("title_author_extraction", {"submission": target.pk}),
            ("process", {"submission": target.pk}),
        ]
        for url_name, query in focused_pages:
            with self.subTest(url_name=url_name):
                response = self.client.get(
                    reverse(f"submissions:{url_name}"), query
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Focused")
                self.assertContains(response, "Exact Target")
                self.assertNotContains(response, "Similar Paper ID")

        fuzzy = self.client.get(
            reverse("submissions:title_author_extraction"), {"filter": "all", "q": "58"}
        )
        self.assertContains(fuzzy, "Exact Target")
        self.assertContains(fuzzy, "Similar Paper ID")
        self.assertNotEqual(target.pk, other.pk)

    def test_final_submission_workflow_links_use_exact_targets(self):
        self.make_master_paper(paper_id="P001")
        submission = self.make_final_submission()
        response = self.client.get(
            reverse("submissions:final_submission_edit", args=[submission.pk])
        )

        self.assertContains(
            response,
            f'{reverse("submissions:organized_list")}?paper_id=P001',
            html=False,
        )
        self.assertContains(
            response,
            f'{reverse("submissions:verify_paper_ids")}?submission={submission.pk}',
            html=False,
        )
        self.assertContains(
            response,
            f'{reverse("submissions:process")}?submission={submission.pk}',
            html=False,
        )
        self.assertContains(
            response,
            f'{reverse("submissions:title_author_extraction")}?submission={submission.pk}',
            html=False,
        )
        self.assertContains(
            response,
            (
                f'{reverse("submissions:formatting")}?mode=focus&amp;'
                f'submission={submission.pk}'
            ),
            html=False,
        )
        self.assertContains(
            response,
            f'{reverse("submissions:not_publishing_list")}?submission={submission.pk}',
            html=False,
        )

    def test_focused_publication_decision_can_show_normal_candidate(self):
        self.make_master_paper(paper_id="P001")
        submission = self.make_final_submission()
        response = self.client.get(
            reverse("submissions:not_publishing_list"),
            {"submission": submission.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Focused publication decision")
        self.assertContains(response, "Publication candidate")
        self.assertContains(response, "Mark Not Publishing")

    def test_author_count_links_to_exact_exception_key(self):
        for index in range(4):
            paper_id = f"A{index:03d}"
            self.make_master_paper(
                paper_id=paper_id,
                title=f"Author Paper {index}",
                authors="Ada Lovelace",
            )
            self.make_final_submission(
                final_submission_id=str(700 + index),
                paper_id_filled=paper_id,
                final_submission_title=f"Author Paper {index}",
                extracted_title=f"Author Paper {index}",
                extracted_authors="Ada Lovelace",
            )
        rebuild_paper_authors()

        author_page = self.client.get(reverse("submissions:author_count"))
        expected_key = "author_limit:ada lovelace"
        self.assertContains(
            author_page,
            (
                f'{reverse("submissions:exceptions_center")}?'
                "exception_key=author_limit%3Aada%20lovelace"
            ),
            html=False,
        )
        focused = self.client.get(
            reverse("submissions:exceptions_center"),
            {"exception_key": expected_key},
        )
        self.assertContains(focused, "Focused author exception")
        self.assertContains(focused, "Ada Lovelace")

    def test_dashboard_links_open_scoped_worklists(self):
        self.make_master_paper(paper_id="MISSING")
        response = self.client.get(reverse("submissions:dashboard_summary"))
        self.assertContains(
            response,
            f'{reverse("submissions:error_report")}?area=mapping',
            html=False,
        )

        report = self.client.get(
            reverse("submissions:error_report"), {"area": "mapping"}
        )
        self.assertEqual(report.context["current_area"], "mapping")
        self.assertContains(report, "Focused workflow area")
        self.assertContains(report, "Missing Final Submission")

    def test_focused_worklist_gets_do_not_change_review_state(self):
        self.make_master_paper(paper_id="P001")
        submission = self.make_final_submission(
            paper_id_verified=False,
            verification_status="pending",
            title_author_review_status="pending",
            title_author_verified=False,
        )
        before = {
            "paper_id_verified": submission.paper_id_verified,
            "verification_status": submission.verification_status,
            "title_author_review_status": submission.title_author_review_status,
            "active_version": submission.active_version,
        }

        for url_name, query in [
            ("verify_paper_ids", {"submission": submission.pk}),
            ("title_author_extraction", {"submission": submission.pk}),
            ("process", {"submission": submission.pk}),
            ("formatting", {"mode": "focus", "submission": submission.pk}),
            ("not_publishing_list", {"submission": submission.pk}),
            ("organized_list", {"paper_id": "P001"}),
        ]:
            with self.subTest(url_name=url_name):
                response = self.client.get(
                    reverse(f"submissions:{url_name}"), query
                )
                self.assertEqual(response.status_code, 200)

        submission.refresh_from_db()
        self.assertEqual(
            {
                "paper_id_verified": submission.paper_id_verified,
                "verification_status": submission.verification_status,
                "title_author_review_status": submission.title_author_review_status,
                "active_version": submission.active_version,
            },
            before,
        )


class MasterAndFinalListSortingTests(EditorialAcceptanceTestCase):
    def test_paper_master_sort_uses_natural_paper_id_order(self):
        self.make_master_paper("P10", "Alpha", "Ada")
        self.make_master_paper("P2", "Zulu", "Grace")

        ascending = self.client.get(
            reverse("submissions:initial_paper_list"),
            {"sort": "paper_id_asc", "page_size": "all"},
        )
        descending = self.client.get(
            reverse("submissions:initial_paper_list"),
            {"sort": "paper_id_desc", "page_size": "all"},
        )

        self.assertEqual(
            [paper.paper_id for paper in ascending.context["papers"]],
            ["P2", "P10"],
        )
        self.assertEqual(
            [paper.paper_id for paper in descending.context["papers"]],
            ["P10", "P2"],
        )
        self.assertEqual(ascending.context["current_sort"], "paper_id_asc")
        self.assertContains(ascending, 'id="paper-master-sort"')

    def test_final_submission_sort_uses_natural_final_id_order_and_preserves_tab_sort(self):
        self.make_master_paper("P001", "First", "Ada")
        self.make_master_paper("P002", "Second", "Grace")
        self.make_final_submission(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="First",
        )
        self.make_final_submission(
            final_submission_id="2",
            paper_id_filled="P002",
            final_submission_title="Second",
        )

        response = self.client.get(
            reverse("submissions:final_submission_list"),
            {"sort": "final_id_asc", "page_size": "all"},
        )

        self.assertEqual(
            [item.final_submission_id for item in response.context["submissions"]],
            ["2", "10"],
        )
        self.assertEqual(response.context["current_sort"], "final_id_asc")
        self.assertContains(response, "sort=final_id_asc")
        self.assertContains(response, 'id="final-submission-sort"')

    def test_invalid_sort_values_fall_back_to_stable_defaults(self):
        master = self.client.get(
            reverse("submissions:initial_paper_list"), {"sort": "unknown"}
        )
        final = self.client.get(
            reverse("submissions:final_submission_list"), {"sort": "unknown"}
        )

        self.assertEqual(master.context["current_sort"], "paper_id_asc")
        self.assertEqual(final.context["current_sort"], "paper_id_asc")

    def test_old_versions_use_shared_tab_design(self):
        response = self.client.get(
            reverse("submissions:old_versions"), {"filter": "discarded"}
        )

        self.assertContains(response, 'class="nav nav-tabs cfm-tabs"')
        self.assertContains(response, "filter=discarded")
        self.assertContains(response, "text-bg-primary")
