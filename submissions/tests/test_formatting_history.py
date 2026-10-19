import csv
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from submissions.models import FinalSubmission, FormattingRevision
from submissions.services.formatting import (
    apply_formatting_upload_preview, create_formatting_review_snapshot,
    load_formatting_review_snapshot, preview_formatting_upload,
    save_formatting_review, update_formatting_submission,
)
from submissions.services.formatting_history import (
    capture_existing_formatting,
    current_file_fingerprints,
    open_revision_file,
    revision_files_transaction,
    revision_matches_current,
)
from submissions.services.import_preview import _archive_and_unlink_corrected_files
from submissions.services.storage_inventory import (
    apply_storage_cleanup, build_storage_inventory, preview_storage_cleanup,
)
from submissions.services.system_state import (
    CONFIRMATION_TEXT, SystemStateError, _model_payload_sha256,
    apply_system_state_restore, export_system_state, preview_system_state_restore,
)
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase


class FormattingHistoryTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(BASE_DIR=self.root.resolve()))
        self.make_master_paper()
        self.submission = self.make_final_submission(format_status="pending")

    def upload(self, pdf=None, source=None, **overrides):
        data = {
            "format_status": "pending", "format_notes": "Revision notes",
            "corrected_pdf": SimpleUploadedFile("same.pdf", pdf) if pdf is not None else None,
            "corrected_source": SimpleUploadedFile("same.zip", source) if source is not None else None,
        }
        data.update(overrides)
        result = update_formatting_submission(self.submission, data)
        self.submission.refresh_from_db()
        return result

    def read_revision(self, revision, kind):
        with open_revision_file(revision, kind) as handle:
            return handle.read()

    def test_uploads_capture_complete_independent_pairs_and_status_only_creates_none(self):
        original_source = Path(self.submission.source_file.path).read_bytes()
        self.upload(pdf=b"round one")
        first = FormattingRevision.objects.get(number=1)
        self.upload(source=b"source two")
        second = FormattingRevision.objects.get(number=2)
        self.upload(pdf=b"round three")
        third = FormattingRevision.objects.get(number=3)
        self.assertEqual(self.read_revision(first, "pdf"), b"round one")
        self.assertEqual(self.read_revision(first, "source"), original_source)
        self.assertEqual(self.read_revision(second, "pdf"), b"round one")
        self.assertEqual(self.read_revision(second, "source"), b"source two")
        self.assertEqual(self.read_revision(third, "pdf"), b"round three")
        self.assertEqual(self.read_revision(third, "source"), b"source two")
        self.assertNotEqual(second.pdf_file.name, first.pdf_file.name)
        self.upload(format_notes="Only notes", format_status="needs_edit")
        self.assertEqual(FormattingRevision.objects.count(), 3)
        self.assertEqual(third.format_notes, "Revision notes")
        self.assertTrue(revision_matches_current(third, self.submission))
        self.assertFalse(revision_matches_current(first, self.submission))

    def test_baseline_is_idempotent_preserves_review_and_supports_inactive_discarded(self):
        self.submission.formatted_pdf_file.save("legacy.pdf", ContentFile(b"legacy"))
        self.submission.discarded = True
        self.submission.active_version = False
        self.submission.save()
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        call_command("backfill_formatting_revisions", stdout=io.StringIO())
        call_command("backfill_formatting_revisions", stdout=io.StringIO())
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        revision = FormattingRevision.objects.get()
        self.assertEqual(revision.event_type, "baseline")
        self.assertEqual(self.read_revision(revision, "pdf"), b"legacy")
        response = self.client.get(reverse("submissions:final_submission_edit", args=[self.submission.pk]))
        self.assertContains(response, "Revision history (1)")

    def test_first_replacement_captures_existing_corrected_pair(self):
        self.submission.formatted_pdf_file.save("legacy.pdf", ContentFile(b"legacy"))
        self.upload(pdf=b"new")
        self.assertEqual(list(FormattingRevision.objects.order_by("number").values_list("event_type", flat=True)), ["baseline", "upload"])
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(number=1), "pdf"), b"legacy")

    def test_original_invalidation_captures_before_unlink_and_never_marks_latest_current(self):
        self.submission.formatted_pdf_file.save("legacy.pdf", ContentFile(b"legacy"))
        with revision_files_transaction():
            _archive_and_unlink_corrected_files(self.submission)
            self.submission.save()
        revision = FormattingRevision.objects.get()
        self.assertEqual(self.read_revision(revision, "pdf"), b"legacy")
        self.assertFalse(revision_matches_current(revision, self.submission))

    def test_copy_failure_rolls_back_files_database_and_revision(self):
        self.upload(pdf=b"first")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        before_files = set(self.media_root.rglob("*"))
        with patch("submissions.services.formatting_history._copy_snapshot", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.upload(pdf=b"new", source=b"new source")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(FormattingRevision.objects.count(), 1)
        self.assertEqual({p for p in self.media_root.rglob("*") if p.is_file()}, {p for p in before_files if p.is_file()})

    def test_audit_and_database_failures_leave_no_new_revision_or_live_upload(self):
        self.upload(pdf=b"first")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        before_files = {p for p in self.media_root.rglob("*") if p.is_file()}
        for target in ("submissions.services.formatting.audit_success", "submissions.services.formatting_history.FormattingRevision.objects.create"):
            with self.subTest(target=target), patch(target, side_effect=RuntimeError("failure")):
                with self.assertRaises(RuntimeError):
                    self.upload(pdf=b"failed")
                self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
                self.assertEqual(FormattingRevision.objects.count(), 1)
                self.assertEqual({p for p in self.media_root.rglob("*") if p.is_file()}, before_files)

    def test_stale_save_and_failed_review_validation_create_no_revision(self):
        before_files = {p for p in self.media_root.rglob("*") if p.is_file()}
        with self.assertRaises((ValueError, KeyError)):
            save_formatting_review(self.submission.pk, {}, {"submission_id": -1})
        self.assertFalse(FormattingRevision.objects.exists())
        self.assertEqual({p for p in self.media_root.rglob("*") if p.is_file()}, before_files)

    def test_history_download_is_exact_scoped_and_detects_corruption(self):
        self.upload(pdf=b"history pdf", source=b"history source")
        revision = FormattingRevision.objects.get()
        self.upload(pdf=b"current pdf")
        for kind, expected in (("pdf", b"history pdf"), ("source", b"history source")):
            url = reverse(f"submissions:formatting_revision_{kind}", args=[self.submission.pk, revision.pk])
            response = self.client.get(url)
            self.assertEqual(b"".join(response.streaming_content), expected)
            self.assertIn("inline" if kind == "pdf" else "attachment", response["Content-Disposition"])
            response.close()
            other = self.make_final_submission(final_submission_id=f"OTHER-{kind}")
            wrong = reverse(f"submissions:formatting_revision_{kind}", args=[other.pk, revision.pk])
            self.assertEqual(self.client.get(wrong).status_code, 404)
        Path(revision.pdf_file.path).write_bytes(b"corrupt")
        response = self.client.get(reverse("submissions:formatting_revision_pdf", args=[self.submission.pk, revision.pk]))
        self.assertContains(response, "integrity check", status_code=404)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), b"current pdf")

    def test_missing_revision_does_not_change_publication_file_and_no_fallback(self):
        self.upload(pdf=b"publication")
        revision = FormattingRevision.objects.get()
        Path(revision.pdf_file.path).unlink()
        from submissions.services.file_manager import publication_pdf_info
        self.assertEqual(Path(publication_pdf_info(self.submission)["path"]).read_bytes(), b"publication")
        Path(self.submission.formatted_pdf_file.path).unlink()
        self.assertFalse(publication_pdf_info(self.submission)["exists"])

    def test_small_history_omits_redundant_controls_for_full_and_lazy_pages(self):
        self.upload(pdf=b"history")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        url = reverse("submissions:formatting_history", args=[self.submission.pk])
        for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
            with self.subTest(headers=headers):
                response = self.client.get(url, **headers)
                self.assertContains(response, "Revision 1")
                self.assertNotContains(response, "cfm-worklist-pagination")
                self.assertNotContains(response, "Rows per page")
                self.assertNotContains(response, "Revisions per page")
                self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

    def test_history_read_and_pagination_are_local_and_read_only(self):
        self.upload(pdf=b"history")
        for number in range(2, 28):
            self.upload(pdf=f"history {number}".encode())
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        response = self.client.get(reverse("submissions:formatting_history", args=[self.submission.pk]), HTTP_HX_REQUEST="true")
        self.assertContains(response, "Showing 1-25 of 27", count=2)
        self.assertContains(response, 'hx-push-url="false"')
        self.assertContains(response, 'aria-label="Top revision history pagination"', count=1)
        self.assertContains(response, 'aria-label="Bottom revision history pagination"', count=1)
        self.assertContains(response, 'aria-label="Revisions per page"', count=2)
        self.assertNotContains(response, 'aria-label="Rows per page"')
        self.assertEqual(len(response.context["rows"]), 25)
        second = self.client.get(reverse("submissions:formatting_history", args=[self.submission.pk]), {"page": 2}, HTTP_HX_REQUEST="true")
        self.assertContains(second, "Showing 26-27 of 27", count=2)
        self.assertEqual([row["revision"].number for row in second.context["rows"]], [2, 1])
        for size in ("50", "all"):
            complete = self.client.get(reverse("submissions:formatting_history", args=[self.submission.pk]), {"page_size": size}, HTTP_HX_REQUEST="true")
            self.assertContains(complete, 'aria-label="Revisions per page"', count=2)
            self.assertContains(complete, "page_size=25")
            self.assertEqual(len(complete.context["rows"]), 27)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        for params in ({"filter": "all"}, {"mode": "focus", "submission": self.submission.pk}):
            page = self.client.get(reverse("submissions:formatting"), params)
            self.assertContains(page, "Revision history (27)")

    def test_cleanup_preserves_history_even_with_overlapping_reports_folder(self):
        self.upload(pdf=b"history", source=b"zip")
        setting = self.submission._meta.apps.get_model("submissions", "AppSetting").load()
        setting.reports_folder = str(self.media_root)
        setting.save()
        inventory = build_storage_inventory()
        candidates = inventory["cleanup_candidates"] + inventory["report_export_cleanup_candidates"]
        self.assertFalse(any("formatting_revisions" in str(row["path"]) for row in candidates))
        for revision in FormattingRevision.objects.all():
            self.assertTrue(Path(revision.source_file.path).exists())
        setting.reports_folder = str(self.root / "reports")
        setting.save()
        orphan = self.media_root / "pdf_thumbnails" / "unused.png"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan cache")
        preview = preview_storage_cleanup()
        apply_storage_cleanup(preview["token"], "CLEAN STORAGE")
        self.assertFalse(orphan.exists())
        report = self.root / "reports" / "generated.xlsx"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"regenerable report")
        preview = preview_storage_cleanup("generated_reports_exports")
        apply_storage_cleanup(preview["token"], "CLEAN STORAGE")
        self.assertFalse(report.exists())
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "source"), b"zip")

    def test_current_backup_restore_preserves_revision_files_and_flags(self):
        self.upload(pdf=b"preserved", source=b"paired")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        snapshot = export_system_state()
        self.assertEqual(snapshot["manifest"]["state_archive_version"], 7)
        upload = SimpleUploadedFile("state.zip", snapshot["path"].read_bytes())
        preview = preview_system_state_restore(upload)
        self.assertEqual(preview["artifact_counts"]["formatting_revision_files"], 2)
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        restored = FormattingRevision.objects.get()
        self.assertEqual(self.read_revision(restored, "pdf"), b"preserved")
        self.assertEqual(self.read_revision(restored, "source"), b"paired")
        current = FinalSubmission.objects.values().get(pk=self.submission.pk)
        for field in ("paper_id_verified", "format_status", "title_author_verified", "extracted_title_verified", "similarity_score"):
            self.assertEqual(current[field], before[field])

    def rewritten_snapshot(self, *, version=6, omit_revisions=False, mutate=None):
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        manifest, state = (json.loads(entries[name]) for name in ("manifest.json", "state.json"))
        if version < 7:
            for key in ("crosscheck_service_jobs", "crosscheck_service_items"):
                state["models"].pop(key)
                manifest["record_counts"].pop(key)
        if omit_revisions:
            state["models"].pop("formatting_revisions")
            manifest["record_counts"].pop("formatting_revisions")
        if mutate:
            mutate(manifest, state)
        for payload in (manifest, state):
            payload["state_archive_version"] = version
            payload["snapshot_version"] = version
            payload["model_payload_sha256"] = _model_payload_sha256(state["models"])
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w") as archive:
            for name, content in entries.items():
                if name == "manifest.json":
                    content = json.dumps(manifest)
                if name == "state.json":
                    content = json.dumps(state)
                archive.writestr(name, content)
        return SimpleUploadedFile("rewritten.zip", target.getvalue())

    def test_v5_compatibility_and_v6_missing_payload_is_rejected(self):
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        upload = self.rewritten_snapshot(version=5, omit_revisions=True)
        preview = preview_system_state_restore(upload)
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.assertFalse(FormattingRevision.objects.exists())
        self.assertEqual(FinalSubmission.objects.get(pk=self.submission.pk).paper_id_verified, before["paper_id_verified"])
        with self.assertRaisesRegex(SystemStateError, "incomplete"):
            preview_system_state_restore(self.rewritten_snapshot(omit_revisions=True))

    def test_v6_restore_keeps_revision_pair_and_current_publication_binding(self):
        from submissions.models import CrossCheckServiceJob
        from submissions.services.file_manager import publication_pdf_info, publication_source_info

        self.upload(pdf=b"v6 publication pdf", source=b"v6 publication source")
        self.mark_submission_publication_ready(self.submission)
        self.submission.current_file_path = ""
        self.submission.source_current_file_path = ""
        thumbnails = self.media_root.resolve() / "pdf_thumbnails/v6"
        thumbnails.mkdir(parents=True)
        (thumbnails / "page-1.png").write_bytes(b"v6 thumbnail")
        self.submission.thumbnail_folder = str(thumbnails)
        self.submission.save()
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        revision_before = FormattingRevision.objects.values().get()
        preview = preview_system_state_restore(self.rewritten_snapshot(version=6))
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.submission.refresh_from_db()
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        revision = FormattingRevision.objects.get()
        self.assertEqual(FormattingRevision.objects.values().get(), revision_before)
        self.assertEqual(self.read_revision(revision, "pdf"), b"v6 publication pdf")
        self.assertEqual(self.read_revision(revision, "source"), b"v6 publication source")
        self.assertEqual(Path(publication_pdf_info(self.submission)["path"]).read_bytes(), b"v6 publication pdf")
        self.assertEqual(Path(publication_source_info(self.submission)["path"]).read_bytes(), b"v6 publication source")
        self.assertEqual((Path(self.submission.thumbnail_folder) / "page-1.png").read_bytes(), b"v6 thumbnail")
        self.assertFalse(CrossCheckServiceJob.objects.exists())

    def test_final_and_crosscheck_exports_use_live_bytes_and_readiness_is_unchanged(self):
        from submissions.services.checks import publication_readiness_rows
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import export_publication_package

        self.upload(pdf=b"first", source=b"first source")
        self.upload(pdf=b"publication pdf", source=b"publication source")
        self.mark_submission_publication_ready(self.submission)
        findings = publication_readiness_rows()
        self.assertEqual(findings, [])
        with zipfile.ZipFile(export_publication_package()) as archive:
            pdf_entries = [name for name in archive.namelist() if name.startswith("PDF/")]
            self.assertEqual(len(pdf_entries), 1)
            self.assertEqual(archive.read(pdf_entries[0]), b"publication pdf")
        result = prepare_crosscheck_upload("HISTORYQA")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_HISTORYQA.pdf"), b"publication pdf")
        self.client.get(reverse("submissions:formatting_history", args=[self.submission.pk]))
        old = FormattingRevision.objects.get(number=1)
        Path(old.pdf_file.path).unlink()
        self.assertEqual(publication_readiness_rows(), findings)

    def test_title_guard_preview_does_not_capture_until_confirm_and_rejects_replay(self):
        token = create_formatting_review_snapshot({}, self.submission)
        snapshot = load_formatting_review_snapshot({}, token, submission_id=self.submission.pk)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with patch("submissions.services.formatting.get_title_author", return_value=("Wrong title", "Someone", 1)):
            preview = preview_formatting_upload(self.submission, {
                "corrected_pdf": SimpleUploadedFile("wrong.pdf", b"wrong PDF"),
                "corrected_source": None, "format_status": "pending", "format_notes": "Guarded upload",
            }, review_snapshot=snapshot)
        self.assertTrue(preview["requires_confirmation"])
        self.assertFalse(FormattingRevision.objects.exists())
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        apply_formatting_upload_preview(preview["token"])
        self.assertEqual(FormattingRevision.objects.count(), 1)
        with self.assertRaises(ValueError):
            save_formatting_review(self.submission.pk, {}, snapshot)
        self.assertEqual(FormattingRevision.objects.count(), 1)

    def test_baseline_backup_restore_and_missing_source_metadata(self):
        self.submission.formatted_pdf_file.save("existing.pdf", ContentFile(b"existing"))
        self.submission.source_file = ""
        self.submission.save()
        capture_existing_formatting(self.submission.pk)
        revision = FormattingRevision.objects.get()
        self.assertEqual(revision.changed_files, [])
        self.assertFalse(revision.source_metadata["exists"])
        snapshot = export_system_state()
        preview = preview_system_state_restore(SimpleUploadedFile("baseline.zip", snapshot["path"].read_bytes()))
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "pdf"), b"existing")

    def test_outer_rollback_removes_baseline_copies_and_preserves_current_files(self):
        self.submission.formatted_pdf_file.save("existing.pdf", ContentFile(b"existing"))
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with self.assertRaisesRegex(RuntimeError, "outer failure"):
            with revision_files_transaction():
                capture_existing_formatting(self.submission.pk)
                raise RuntimeError("outer failure")
        self.assertFalse(FormattingRevision.objects.exists())
        self.assertEqual(list((self.media_root / "formatting_revisions").rglob("*.pdf")), [])
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), b"existing")

    def test_clear_database_failure_restores_history_then_success_removes_it(self):
        from django.db.models import QuerySet
        from submissions.models import InitialPaper
        self.upload(pdf=b"history")
        revision = FormattingRevision.objects.get()
        original_delete = QuerySet.delete

        def fail_paper_delete(queryset):
            if queryset.model is InitialPaper:
                raise RuntimeError("clear failed")
            return original_delete(queryset)

        with patch.object(QuerySet, "delete", new=fail_paper_delete):
            self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "pdf"), b"history")
        self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertFalse(FormattingRevision.objects.exists())
        self.assertFalse(Path(revision.pdf_file.path).exists())

    def test_revision_endpoint_rejects_path_outside_its_own_folder(self):
        self.upload(pdf=b"history")
        revision = FormattingRevision.objects.get()
        revision.pdf_file = self.submission.pdf_file.name
        revision.save()
        with self.assertRaisesRegex(ValueError, "outside"):
            open_revision_file(revision, "pdf")

    def test_baseline_copy_error_blocks_replacement_of_existing_corrected_file(self):
        self.submission.formatted_pdf_file.save("existing.pdf", ContentFile(b"existing"))
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with patch("submissions.services.formatting_history.shutil.copyfileobj", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.upload(pdf=b"new")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertFalse(FormattingRevision.objects.exists())
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), b"existing")

    def test_import_and_manual_original_replacement_keep_correct_previous_pair(self):
        from submissions.forms import FinalSubmissionForm
        from submissions.services.import_preview import apply_import_preview, preview_final_import
        from submissions.services.manual_edit import apply_final_submission_manual_edit
        from submissions.services.workflow_evidence import final_submission_edit_evidence, make_evidence_token

        self.submission.formatted_pdf_file.save("old-corrected.pdf", ContentFile(b"before import"))
        old_source = Path(self.submission.source_file.path).read_bytes()
        payload = preview_final_import(
            self.uploaded_csv("final.csv", "final_submission_id,author_entered_paper_id,final_submission_title\n100,P001,Ready Paper\n"),
            [SimpleUploadedFile("100_file_Submit_PDF.pdf", b"replacement original")],
        )
        apply_import_preview(payload["token"])
        revision = FormattingRevision.objects.get()
        self.assertEqual(self.read_revision(revision, "pdf"), b"before import")
        self.assertEqual(self.read_revision(revision, "source"), old_source)
        self.submission.refresh_from_db()
        self.submission.formatted_pdf_file.save("second-corrected.pdf", ContentFile(b"before edit"))
        evidence = make_evidence_token("final-submission-edit", final_submission_edit_evidence(self.submission))
        form = FinalSubmissionForm(
            self.final_submission_form_data(self.submission),
            {"source_file": SimpleUploadedFile("new-original.zip", b"manual source")},
            instance=self.submission,
        )
        self.assertTrue(form.is_valid(), form.errors)
        current = FinalSubmission.objects.get(pk=self.submission.pk)
        apply_final_submission_manual_edit(current, form, expected_evidence_token=evidence)
        second = FormattingRevision.objects.get(number=2)
        self.assertEqual(self.read_revision(second, "pdf"), b"before edit")
        self.assertEqual(self.read_revision(second, "source"), old_source)

    def test_restore_failure_rolls_back_revision_records_and_files(self):
        self.upload(pdf=b"archived")
        snapshot = export_system_state()
        self.upload(pdf=b"current")
        preview = preview_system_state_restore(SimpleUploadedFile("snapshot.zip", snapshot["path"].read_bytes()))
        with patch("submissions.services.system_state._RestoreFileTransaction.activate", side_effect=OSError("restore failed")):
            with self.assertRaises(OSError):
                apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.assertEqual(FormattingRevision.objects.count(), 2)
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(number=2), "pdf"), b"current")
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(number=1), "pdf"), b"archived")

    def test_restore_rejects_invalid_revision_parent_and_hash_without_changes(self):
        self.upload(pdf=b"valid history")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)

        def wrong_parent(manifest, state):
            state["models"]["formatting_revisions"][0]["submission_id"] = 987654321

        def wrong_hash(manifest, state):
            state["models"]["formatting_revisions"][0]["pdf_metadata"]["sha256"] = "0" * 64

        for mutate in (wrong_parent, wrong_hash):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemStateError):
                preview_system_state_restore(self.rewritten_snapshot(mutate=mutate))
            self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "pdf"), b"valid history")

    def test_publication_origin_matrix_ignores_history_legacy_and_debug_files(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import export_publication_package

        original_pdf = Path(self.submission.pdf_file.path).read_bytes()
        original_source = Path(self.submission.source_file.path).read_bytes()
        self.upload(pdf=b"historical PDF", source=b"historical source")
        legacy = self.make_file("legacy/not-publication.pdf", b"wrong legacy PDF")
        self.submission.current_file_path = str(legacy)
        self.submission.source_current_file_path = str(legacy)
        self.submission.save()
        self.make_file("publication_debug/not-publication.pdf", b"wrong debug PDF")

        for index, (corrected_pdf, corrected_source) in enumerate(
            ((False, False), (True, False), (False, True), (True, True))
        ):
            with self.subTest(pdf=corrected_pdf, source=corrected_source):
                self.submission.formatted_pdf_file = ""
                self.submission.formatted_source_file = ""
                self.submission.save()
                if corrected_pdf or corrected_source:
                    self.upload(
                        pdf=b"selected PDF" if corrected_pdf else None,
                        source=b"selected source" if corrected_source else None,
                    )
                self.mark_submission_publication_ready(self.submission)
                expected_pdf = b"selected PDF" if corrected_pdf else original_pdf
                expected_source = b"selected source" if corrected_source else original_source
                with zipfile.ZipFile(export_publication_package()) as archive:
                    pdfs = [name for name in archive.namelist() if name.startswith("PDF/")]
                    sources = [name for name in archive.namelist() if name.startswith("Source/")]
                    self.assertEqual(len(pdfs), 1)
                    self.assertEqual(len(sources), 1)
                    self.assertEqual(archive.read(pdfs[0]), expected_pdf)
                    self.assertEqual(archive.read(sources[0]), expected_source)
                    manifest = next(name for name in archive.namelist() if name.endswith(".csv"))
                    records = list(csv.DictReader(io.StringIO(archive.read(manifest).decode("utf-8-sig"))))
                    self.assertEqual([row["ID"] for row in records], ["P001"])

                self.submission.similarity_score = None
                self.submission.save()
                token = f"ORIGINMATRIX{index}"
                for scope in ("all", "missing_results"):
                    result = prepare_crosscheck_upload(token, scope=scope)
                    with zipfile.ZipFile(result["zip_path"]) as archive:
                        self.assertEqual(archive.read(f"P001_{token}.pdf"), expected_pdf)

    def test_missing_current_corrected_pair_cannot_fall_back_to_history_or_original(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import PublicationPackageBlocked, export_publication_package

        self.make_master_paper("P002")
        other = self.make_final_submission(final_submission_id="200", paper_id_filled="P002")
        for kind in ("pdf", "source"):
            with self.subTest(missing=kind):
                self.upload(pdf=b"selected PDF", source=b"selected source")
                self.mark_submission_publication_ready(self.submission)
                revision = FormattingRevision.objects.first()
                Path(getattr(self.submission, f"formatted_{kind}_file").path).unlink()
                self.assertTrue(Path(getattr(self.submission, f"{kind}_file").path).is_file())
                self.assertTrue(Path(getattr(revision, f"{kind}_file").path).is_file())
                with self.assertRaises(PublicationPackageBlocked):
                    export_publication_package()
                with zipfile.ZipFile(export_publication_package(force=True)) as archive:
                    pdfs = [name for name in archive.namelist() if name.startswith("PDF/")]
                    self.assertEqual(len(pdfs), 1)
                    self.assertTrue(pdfs[0].startswith("PDF/P002-"))
                    warning = next(name for name in archive.namelist() if "warnings_" in name)
                    rows = list(csv.DictReader(io.StringIO(archive.read(warning).decode("utf-8-sig"))))
                    self.assertTrue(any(row["Type"] == "Skipped from draft package" and row["Paper ID"] == "P001" for row in rows))
                result = prepare_crosscheck_upload(f"MISSING{kind.upper()}")
                with zipfile.ZipFile(result["zip_path"]) as archive:
                    if kind == "pdf":
                        self.assertNotIn("P001_MISSINGPDF.pdf", archive.namelist())
                        self.assertEqual(result["skipped_count"], 1)
                    else:
                        self.assertEqual(archive.read("P001_MISSINGSOURCE.pdf"), b"selected PDF")
                    self.assertEqual(
                        archive.read(f"P002_MISSING{kind.upper()}.pdf"),
                        Path(other.pdf_file.path).read_bytes(),
                    )

    def test_real_pdf_processing_after_original_reimport_never_uses_latest_revision(self):
        import fitz
        from submissions.services.checks import publication_readiness_rows
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.import_preview import apply_import_preview, preview_final_import
        from submissions.services.pdf_processor import process_all_pdfs
        from submissions.services.reports import export_publication_package

        def pdf_bytes(label):
            with fitz.open() as document:
                for page in range(8):
                    document.new_page().insert_text((72, 72), f"{label} - page {page + 1}")
                return document.tobytes()

        corrected = pdf_bytes("Old corrected version")
        replacement = pdf_bytes("New original version")
        self.upload(pdf=corrected, source=b"old corrected source")
        self.mark_submission_publication_ready(self.submission)
        preview = preview_final_import(
            self.uploaded_csv("final.csv", "final_submission_id,author_entered_paper_id,final_submission_title\n100,P001,Ready Paper\n"),
            [
                SimpleUploadedFile("100_file_Submit_PDF.pdf", replacement),
                SimpleUploadedFile("100_file_Submit_Source.zip", b"new original source"),
            ],
        )
        apply_import_preview(preview["token"])
        self.submission.refresh_from_db()
        self.assertFalse(self.submission.formatted_pdf_file)
        self.assertFalse(self.submission.formatted_source_file)
        self.assertEqual(self.read_revision(FormattingRevision.objects.first(), "pdf"), corrected)
        original_paths = [Path(self.submission.pdf_file.path), Path(self.submission.source_file.path)]
        before = {path: path.read_bytes() for path in original_paths}
        process_all_pdfs()
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.page_count, 8)
        self.assertEqual(self.submission.pdf_hash, hashlib.sha256(replacement).hexdigest())
        self.assertEqual(self.submission.format_status, "pending")
        self.assertFalse(self.submission.title_author_verified)
        self.assertTrue(publication_readiness_rows())
        self.assertEqual({path: path.read_bytes() for path in original_paths}, before)
        self.mark_submission_publication_ready(self.submission)
        with zipfile.ZipFile(export_publication_package()) as archive:
            pdf = next(name for name in archive.namelist() if name.startswith("PDF/"))
            source = next(name for name in archive.namelist() if name.startswith("Source/"))
            self.assertEqual(archive.read(pdf), replacement)
            self.assertEqual(archive.read(source), b"new original source")
        result = prepare_crosscheck_upload("REIMPORT")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_REIMPORT.pdf"), replacement)

    def test_source_copy_failure_after_pdf_copy_rolls_back_the_complete_pair(self):
        from submissions.services.formatting_history import _copy_snapshot

        self.upload(pdf=b"previous PDF", source=b"previous source")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        before_files = {path for path in self.media_root.rglob("*") if path.is_file()}

        def fail_source(submission, kind, info, directory):
            if kind == "source":
                self.assertTrue((directory / "pdf.pdf").is_file())
                raise OSError("source copy failed after PDF was captured")
            return _copy_snapshot(submission, kind, info, directory)

        with patch("submissions.services.formatting_history._copy_snapshot", side_effect=fail_source):
            with self.assertRaisesRegex(OSError, "source copy failed"):
                self.upload(pdf=b"new PDF", source=b"new source")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual({path for path in self.media_root.rglob("*") if path.is_file()}, before_files)
        self.assertEqual(FormattingRevision.objects.count(), 1)
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "source"), b"previous source")

    def test_same_size_same_mtime_mutation_during_capture_is_rejected(self):
        from submissions.services.formatting_history import shutil

        self.upload(pdf=b"previous PDF", source=b"previous source")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        before_files = {path for path in self.media_root.rglob("*") if path.is_file()}
        original_copy = shutil.copyfileobj

        def copy_then_mutate(source, target, *args, **kwargs):
            result = original_copy(source, target, *args, **kwargs)
            path = Path(source.name)
            stat = path.stat()
            path.write_bytes(b"X" * stat.st_size)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return result

        with patch("submissions.services.formatting_history.shutil.copyfileobj", side_effect=copy_then_mutate):
            with self.assertRaisesRegex(ValueError, "changed while"):
                self.upload(pdf=b"new PDF")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual({path for path in self.media_root.rglob("*") if path.is_file()}, before_files)
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "pdf"), b"previous PDF")

    def test_two_editors_with_same_evidence_cannot_apply_a_second_upload(self):
        token = create_formatting_review_snapshot({}, self.submission)
        first_snapshot = load_formatting_review_snapshot({}, token, submission_id=self.submission.pk)
        second_snapshot = load_formatting_review_snapshot({}, token, submission_id=self.submission.pk)
        save_formatting_review(self.submission.pk, {
            "format_status": "pending", "format_notes": "First editor",
            "corrected_pdf": SimpleUploadedFile("first.pdf", b"first editor PDF"),
        }, first_snapshot)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        before_files = {path for path in self.media_root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ValueError, "changed after"):
            save_formatting_review(self.submission.pk, {
                "format_status": "review_ok", "format_notes": "Second editor",
                "corrected_pdf": SimpleUploadedFile("second.pdf", b"second editor PDF"),
            }, second_snapshot)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual({path for path in self.media_root.rglob("*") if path.is_file()}, before_files)
        self.assertEqual(FormattingRevision.objects.count(), 1)
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "pdf"), b"first editor PDF")

    def test_revision_capture_does_not_restore_reviews_or_expand_source_only_resets(self):
        self.mark_submission_publication_ready(self.submission)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        self.upload(source=b"source-only replacement", format_status="review_ok")
        for field in ("paper_id_verified", "verification_status", "page_count", "pdf_hash", "thumbnail_folder", "processing_status", "similarity_score", "single_similarity_score"):
            self.assertEqual(getattr(self.submission, field), before[field], field)
        self.assertEqual(self.submission.source_hash, "")
        self.assertEqual(self.submission.format_status, "pending")
        self.assertFalse(self.submission.title_author_verified)
        self.assertFalse(self.submission.extracted_title_verified)
        self.assertEqual(FormattingRevision.objects.get().format_status, "pending")

        self.mark_submission_publication_ready(self.submission)
        self.upload(pdf=b"PDF-only replacement", format_status="review_ok")
        self.assertTrue(self.submission.paper_id_verified)
        for field in ("page_count", "similarity_score", "single_similarity_score"):
            self.assertIsNone(getattr(self.submission, field), field)
        for field in ("pdf_hash", "source_hash", "thumbnail_folder"):
            self.assertEqual(getattr(self.submission, field), "", field)
        self.assertEqual(self.submission.processing_status, "pending")
        self.assertEqual(self.submission.format_status, "pending")
        self.assertFalse(self.submission.title_author_verified)
        self.assertEqual(FormattingRevision.objects.first().source_metadata["origin"], "corrected")

    def test_upload_during_export_aborts_without_retaining_a_mixed_package(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.file_inspection import FileInspectionContext
        from submissions.services.publication_read import PublicationStateChangedDuringExport
        from submissions.services.reports import export_publication_package

        for scope in ("publication", "crosscheck"):
            with self.subTest(scope=scope):
                self.upload(pdf=b"ready PDF", source=b"ready source")
                self.mark_submission_publication_ready(self.submission)
                before_count = FormattingRevision.objects.count()
                original_read = FileInspectionContext.read_snapshot_bytes
                changed = False

                def read_then_upload(inspection, path):
                    nonlocal changed
                    data = original_read(inspection, path)
                    if not changed:
                        changed = True
                        self.upload(pdf=b"new concurrent PDF", source=b"new concurrent source")
                    return data

                with patch.object(FileInspectionContext, "read_snapshot_bytes", new=read_then_upload):
                    with self.assertRaises(PublicationStateChangedDuringExport):
                        if scope == "publication":
                            export_publication_package()
                        else:
                            prepare_crosscheck_upload("CONCURRENT")
                self.assertTrue(changed)
                self.assertEqual(FormattingRevision.objects.count(), before_count + 1)
                self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), b"new concurrent PDF")
                self.assertEqual(list((self.root / "reports").glob("publication_*")), [])
                self.assertFalse(any((self.root / "crosscheck_upload").rglob("*.zip")))

    def test_baseline_capture_during_export_is_history_only_and_preserves_package(self):
        from submissions.services.file_inspection import FileInspectionContext
        from submissions.services.publication_read import publication_database_signature
        from submissions.services.reports import export_publication_package

        self.submission.formatted_pdf_file.save("existing.pdf", ContentFile(b"selected PDF"))
        self.submission.formatted_source_file.save("existing.zip", ContentFile(b"selected source"))
        self.mark_submission_publication_ready(self.submission)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        original_read = FileInspectionContext.read_snapshot_bytes

        def read_and_capture(inspection, path):
            data = original_read(inspection, path)
            capture_existing_formatting(self.submission.pk)
            return data

        with patch.object(FileInspectionContext, "read_snapshot_bytes", new=read_and_capture):
            with zipfile.ZipFile(export_publication_package()) as archive:
                pdf = next(name for name in archive.namelist() if name.startswith("PDF/"))
                source = next(name for name in archive.namelist() if name.startswith("Source/"))
                self.assertEqual(archive.read(pdf), b"selected PDF")
                self.assertEqual(archive.read(source), b"selected source")
        self.assertEqual(FormattingRevision.objects.count(), 1)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(publication_database_signature(), signature)

    def test_editor_priority_conflict_and_discard_keep_history_out_of_selection(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.editor_uploads import discard_submission
        from submissions.services.pdf_processor import determine_active_versions
        from submissions.services.reports import PublicationPackageBlocked, export_publication_package

        self.upload(pdf=b"Start2 corrected PDF", source=b"Start2 corrected source")
        self.mark_submission_publication_ready(self.submission)
        start2 = self.submission
        self.submission = self.make_final_submission(
            final_submission_id="EDITOR-P001-001", submission_origin="editor_upload",
        )
        self.upload(pdf=b"Editor corrected PDF", source=b"Editor corrected source")
        determine_active_versions()
        self.submission.refresh_from_db()
        self.mark_submission_publication_ready(self.submission)
        start2.refresh_from_db()
        self.assertFalse(start2.active_version)
        self.assertTrue(self.submission.active_version)
        with self.assertRaises(PublicationPackageBlocked) as blocked:
            export_publication_package()
        self.assertIn("Start2/Editor Version Conflict", {row["category"] for row in blocked.exception.blockers})
        with zipfile.ZipFile(export_publication_package(force=True)) as archive:
            pdf = next(name for name in archive.namelist() if name.startswith("PDF/"))
            source = next(name for name in archive.namelist() if name.startswith("Source/"))
            self.assertEqual(archive.read(pdf), b"Editor corrected PDF")
            self.assertEqual(archive.read(source), b"Editor corrected source")
            warnings = next(name for name in archive.namelist() if "warnings_" in name)
            self.assertIn("Start2/Editor Version Conflict", archive.read(warnings).decode("utf-8-sig"))

        discard_submission(self.submission, "Use the Start2 version")
        start2.refresh_from_db()
        self.assertTrue(start2.active_version)
        self.assertEqual(FormattingRevision.objects.count(), 2)
        with zipfile.ZipFile(export_publication_package()) as archive:
            pdf = next(name for name in archive.namelist() if name.startswith("PDF/"))
            self.assertEqual(archive.read(pdf), b"Start2 corrected PDF")
        result = prepare_crosscheck_upload("AFTERDISCARD")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_AFTERDISCARD.pdf"), b"Start2 corrected PDF")
        discarded_history = FormattingRevision.objects.get(submission=self.submission)
        self.assertEqual(self.read_revision(discarded_history, "pdf"), b"Editor corrected PDF")

    def test_not_publishing_keeps_history_but_excludes_both_exports(self):
        from submissions.models import InitialPaper
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.publication_decisions import mark_paper_not_publishing
        from submissions.services.reports import export_publication_package
        from submissions.services.workflow_evidence import make_evidence_token, paper_publication_decision_evidence

        self.upload(pdf=b"excluded PDF", source=b"excluded source")
        self.mark_submission_publication_ready(self.submission)
        paper = InitialPaper.objects.get(paper_id="P001")
        token = make_evidence_token(
            "paper-publication-decision",
            paper_publication_decision_evidence(paper, [self.submission]),
        )
        mark_paper_not_publishing(paper, "withdrawn", expected_evidence_token=token)
        self.make_master_paper("P002")
        other = self.make_final_submission(final_submission_id="200", paper_id_filled="P002")
        for force in (False, True):
            with self.subTest(draft=force), zipfile.ZipFile(export_publication_package(force=force)) as archive:
                pdfs = [name for name in archive.namelist() if name.startswith("PDF/")]
                sources = [name for name in archive.namelist() if name.startswith("Source/")]
                self.assertEqual(len(pdfs), 1)
                self.assertEqual(len(sources), 1)
                self.assertTrue(pdfs[0].startswith("PDF/P002-"))
                self.assertEqual(archive.read(pdfs[0]), Path(other.pdf_file.path).read_bytes())
        result = prepare_crosscheck_upload("EXCLUDEDHISTORY")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertNotIn("P001_EXCLUDEDHISTORY.pdf", archive.namelist())
            self.assertIn("P002_EXCLUDEDHISTORY.pdf", archive.namelist())
        self.assertEqual(self.read_revision(FormattingRevision.objects.get(), "pdf"), b"excluded PDF")

    def test_revision_history_cannot_bypass_ambiguous_active_version_blockers(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import PublicationPackageBlocked, export_publication_package

        self.upload(pdf=b"current PDF", source=b"current source")
        self.mark_submission_publication_ready(self.submission)
        self.make_final_submission(final_submission_id="101", paper_id_filled="P001")
        for force in (False, True):
            with self.subTest(draft=force), self.assertRaises(PublicationPackageBlocked) as blocked:
                export_publication_package(force=force)
            self.assertIn("Multiple Active Final Submissions", {row["category"] for row in blocked.exception.blockers})
        with self.assertRaisesRegex(ValueError, "multiple active"):
            prepare_crosscheck_upload("AMBIGUOUSHISTORY")
        self.assertEqual(FormattingRevision.objects.count(), 1)
