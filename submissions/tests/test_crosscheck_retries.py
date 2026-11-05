import hashlib
import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from submissions.models import CrossCheckServiceAttempt, CrossCheckServiceJob, CrossCheckServiceResult, FinalSubmission, InitialPaper
from submissions.services.crosscheck_client import CrossCheckServiceError
from submissions.services.crosscheck_service import (
    apply_service_results, job_evidence_token, refresh_service_job, repreview_service_results, retry_service_downloads,
)
from submissions.services.crosscheck_service_history import active_attempt, archive_path, initial_attempt, job_root, batch_context
from submissions.services.system_state import (
    CONFIRMATION_TEXT, SystemStateError, _model_payload_sha256,
    apply_system_state_restore, export_system_state, preview_system_state_restore,
)
from submissions.tests import test_crosscheck_service as fixtures


class CrossCheckRetryTests(TestCase):
    setUp = fixtures.CrossCheckServiceTests.setUp
    preview = fixtures.CrossCheckServiceTests.preview
    archive = fixtures.CrossCheckServiceTests.archive
    ready = fixtures.CrossCheckServiceTests.ready
    second_submission = fixtures.CrossCheckServiceTests.second_submission
    rewrite_archive = fixtures.CrossCheckServiceTests.rewrite_archive

    def partial(self):
        self.second = self.second_submission()
        job = self.preview()
        self.ready(job, outcomes={"P002": {"status": "wait_timeout"}})
        return job

    def response(self, job, execution=None, **values):
        execution = execution or initial_attempt(job)
        response = {"id": execution.remote_id, "kind": "pipeline", "status": "completed",
                    "settings": {"folder": job.folder, "postfix": "_" + job.token,
                                 "file_count": execution.remote_file_count, "wait_minutes": execution.wait_minutes},
                    "progress": {}, "can_download": True, "retry_remaining": 1, "can_retry_download": True}
        if execution.kind == "retry_download":
            response.update(kind="retry_download")
            response["settings"].update(mode="retry_download", source_job_id=initial_attempt(job).remote_id,
                                        parent_job_id=execution.parent.remote_id)
        response.update(values)
        return response

    def begin(self, job, *, remote_id="retry1", count=1, acknowledge_stale=False):
        parent = job.attempts.exclude(remote_id="").last()
        accepted = {"id": remote_id, "kind": "retry_download", "settings": {
            "folder": job.folder, "postfix": "_" + job.token, "file_count": count,
            "mode": "retry_download", "source_job_id": initial_attempt(job).remote_id, "parent_job_id": parent.remote_id}}
        client = Mock()
        client.json.return_value = self.response(job, parent, retry_remaining=count)
        client.retry_download.return_value = accepted
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            retry_service_downloads(job.pk, job_evidence_token(job), 10, acknowledge_stale=acknowledge_stale)
        client.upload.assert_not_called()
        client.retry_download.assert_called_once_with(parent.remote_id, 10)
        job.refresh_from_db()
        return active_attempt(job)

    def receive(self, job, paper_ids=None, outcomes=None):
        execution = active_attempt(job)
        path = self.archive(job, paper_ids=paper_ids or ["P002"], outcomes=outcomes)
        client = Mock()
        client.json.return_value = self.response(job, execution, retry_remaining=0, can_retry_download=False)
        client.download.side_effect = lambda remote_id, target: target.write_bytes(path.read_bytes())
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            refresh_service_job(job.pk)
        job.refresh_from_db()
        return execution

    def test_retry_reuses_batch_and_keeps_both_archives_then_applies_only_new_results(self):
        job = self.partial()
        original = initial_attempt(job)
        original_bytes = archive_path(job, original).read_bytes()
        apply_service_results(job.pk, job_evidence_token(job))
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "partial")
        job.created_at -= timedelta(days=3)
        job.save(update_fields=["created_at"])
        execution = self.begin(job)
        self.receive(job)
        repreview_service_results(job.pk)
        job.refresh_from_db()
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.second.refresh_from_db()
        self.assertEqual(self.second.similarity_score, 42)
        self.assertEqual(archive_path(job, original).read_bytes(), original_bytes)
        self.assertNotEqual(archive_path(job, execution), archive_path(job, original))
        self.assertEqual(job.items.filter(status="applied").count(), 2)
        self.assertEqual(CrossCheckServiceJob.objects.count(), 1)
        self.assertEqual(job.attempts.count(), 2)
        self.assertEqual(job.remote_id, original.remote_id)
        self.assertEqual(job.items.get(paper_id="P001").results.get().applied_at, original.results.get(item__paper_id="P001").applied_at)

    def test_can_retry_before_first_apply_and_apply_existing_results_during_retry(self):
        job = self.partial()
        execution = self.begin(job)
        token = job_evidence_token(job)
        client = Mock()
        client.json.return_value = self.response(job, execution, status="running", can_download=False,
                                               progress={"ready": 0, "total": 1, "phase": "waiting"})
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            refresh_service_job(job.pk)
        apply_service_results(job.pk, token)
        first = FinalSubmission.objects.values().get(pk=self.submission.pk)
        self.receive(job)
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), first)

    def test_repeated_partial_retry_only_targets_still_missing_reports(self):
        job = self.partial()
        first_retry = self.begin(job)
        self.receive(job, outcomes={"P002": {"status": "wait_timeout"}})
        second_retry = self.begin(job, remote_id="retry2")
        self.receive(job)
        self.assertEqual(second_retry.parent_id, first_retry.pk)
        self.assertEqual(second_retry.eligible_paper_ids, ["P002"])
        self.assertTrue(archive_path(job, first_retry).exists())
        self.assertTrue(archive_path(job, second_retry).exists())
        self.assertEqual(job.items.count(), 2)

    def test_double_click_and_ambiguous_request_never_repeat_retry_post(self):
        job = self.partial()
        client = Mock()
        client.json.return_value = self.response(job)
        client.retry_download.side_effect = CrossCheckServiceError("Connection timed out")
        token = job_evidence_token(job)
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaises(CrossCheckServiceError):
                retry_service_downloads(job.pk, token, 10)
            job.refresh_from_db()
            self.assertEqual(active_attempt(job).status, "uncertain")
            for evidence in (token, job_evidence_token(job)):
                with self.assertRaises(ValueError):
                    retry_service_downloads(job.pk, evidence, 10)
        self.assertEqual(client.retry_download.call_count, 1)

    def test_uncertain_retry_recovers_by_lineage_without_upload_or_second_retry(self):
        job = self.partial()
        self.test_client = Mock()
        self.test_client.json.return_value = self.response(job)
        self.test_client.retry_download.side_effect = CrossCheckServiceError("Lost response")
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=self.test_client):
            with self.assertRaises(CrossCheckServiceError):
                retry_service_downloads(job.pk, job_evidence_token(job), 10)
        execution = active_attempt(job)
        remote = self.response(job, execution, id="recovered1", status="running", can_download=False)
        remote["settings"]["file_count"] = 1
        self.test_client.json.side_effect = [{"jobs": [remote]}, remote]
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=self.test_client):
            refresh_service_job(job.pk)
        execution.refresh_from_db()
        self.assertEqual(execution.remote_id, "recovered1")
        self.assertEqual(self.test_client.retry_download.call_count, 1)
        self.test_client.upload.assert_not_called()

    def test_busy_rejection_preserves_original_results_and_allows_later_explicit_retry(self):
        job = self.partial()
        original = archive_path(job, initial_attempt(job)).read_bytes()
        client = Mock()
        client.json.return_value = self.response(job)
        client.retry_download.side_effect = CrossCheckServiceError("Service busy", http_status=409)
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaises(CrossCheckServiceError):
                retry_service_downloads(job.pk, job_evidence_token(job), 10)
        self.assertIsNone(active_attempt(job))
        self.assertEqual(job.attempts.last().status, "rejected")
        self.assertEqual(archive_path(job, initial_attempt(job)).read_bytes(), original)
        job.refresh_from_db()
        self.begin(job)

    def test_old_service_or_no_remaining_does_not_start_retry(self):
        job = self.partial()
        for changes in ({"retry_remaining": None, "can_retry_download": None},
                        {"retry_remaining": 0, "can_retry_download": False},
                        {"retry_remaining": 1, "can_retry_download": False}):
            client = Mock()
            client.json.return_value = self.response(job, **changes)
            with self.subTest(changes=changes), patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
                with self.assertRaises(ValueError):
                    retry_service_downloads(job.pk, job_evidence_token(job), 10)
                client.retry_download.assert_not_called()
        self.assertEqual(job.attempts.count(), 1)

    def test_changed_only_remaining_publication_version_cannot_be_retried_or_applied(self):
        job = self.partial()
        Path(self.second.pdf_file.path).write_bytes(b"%PDF-new publication")
        client = Mock()
        client.json.return_value = self.response(job)
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "old publication versions"):
                retry_service_downloads(job.pk, job_evidence_token(job), 10)
        client.retry_download.assert_not_called()
        self.assertEqual(job.attempts.count(), 1)

    def test_retry_response_with_foreign_parent_is_uncertain_and_never_rebound(self):
        job = self.partial()
        client = Mock()
        client.json.return_value = self.response(job)
        client.retry_download.return_value = {"id": "foreign", "kind": "retry_download", "settings": {
            "folder": job.folder, "postfix": "_" + job.token, "file_count": 1, "mode": "retry_download",
            "source_job_id": "otherbatch", "parent_job_id": "otherbatch"}}
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaises(ValueError):
                retry_service_downloads(job.pk, job_evidence_token(job), 10)
        self.assertEqual(active_attempt(job).status, "uncertain")
        self.assertEqual(active_attempt(job).remote_id, "")
        self.assertEqual(job.remote_id, "remote123")

    def test_retry_zip_cannot_include_papers_already_retained(self):
        job = self.partial()
        before = list(FinalSubmission.objects.order_by("pk").values())
        self.begin(job)
        with self.assertRaisesRegex(ValueError, "allowed papers"):
            self.receive(job, paper_ids=["P001"])
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        self.assertFalse(active_attempt(job).archive_sha256)
        self.assertEqual(job.items.get(paper_id="P001").status, "downloaded")

    def test_corrupt_second_archive_rolls_back_application_across_executions(self):
        job = self.partial()
        execution = self.begin(job)
        self.receive(job)
        archive_path(job, execution).write_bytes(b"corrupt")
        before = list(FinalSubmission.objects.order_by("pk").values())
        with self.assertRaises(ValueError):
            apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        self.assertFalse(job.items.filter(status="applied").exists())
        self.assertFalse(CrossCheckServiceResult.objects.filter(applied_at__isnull=False).exists())
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))

    def test_retry_report_with_different_known_manuscript_identity_is_rejected(self):
        from submissions.services.crosscheck_service_history import parse_service_archive
        job = self.partial()
        execution = self.begin(job)
        execution.remote_file_count = 1
        path = self.archive(job, paper_ids=["P002"])
        self.rewrite_archive(path, ".info/batch_results.csv", lambda raw: raw.replace(b"docP002", b"wrong-document"))
        before = list(FinalSubmission.objects.order_by("pk").values())
        with self.assertRaisesRegex(ValueError, "manuscript identity"):
            parse_service_archive(job, path, attempt=execution)
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        self.assertFalse(execution.results.exists())

    def test_multiple_execution_backup_restore_preserves_hashes_links_and_applied_results(self):
        job = self.partial()
        apply_service_results(job.pk, job_evidence_token(job))
        job.refresh_from_db()
        execution = self.begin(job)
        self.receive(job)
        before = list(FinalSubmission.objects.order_by("pk").values())
        archives = {str(run.pk): hashlib.sha256(archive_path(job, run).read_bytes()).hexdigest() for run in job.attempts.all()}
        snapshot = export_system_state()
        preview = preview_system_state_restore(SimpleUploadedFile("state.zip", Path(snapshot["path"]).read_bytes()))
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        restored = CrossCheckServiceJob.objects.get(pk=job.pk)
        self.assertEqual(restored.attempts.get(number=2).parent_id, initial_attempt(restored).pk)
        for run in restored.attempts.all():
            self.assertEqual(hashlib.sha256(archive_path(restored, run).read_bytes()).hexdigest(), archives[str(run.pk)])
        apply_service_results(restored.pk, job_evidence_token(restored))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before[0])

    def test_v7_restore_captures_legacy_history_without_reset_or_reapply(self):
        job = self.partial()
        apply_service_results(job.pk, job_evidence_token(job))
        before = list(FinalSubmission.objects.order_by("pk").values())
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        state, manifest = (json.loads(entries[name]) for name in ("state.json", "manifest.json"))
        for key in ("crosscheck_service_attempts", "crosscheck_service_results"):
            state["models"].pop(key)
            manifest["record_counts"].pop(key)
        for payload in (state, manifest):
            payload.update(state_archive_version=7, snapshot_version=7, model_payload_sha256=_model_payload_sha256(state["models"]))
        entries["state.json"], entries["manifest.json"] = json.dumps(state), json.dumps(manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        preview = preview_system_state_restore(SimpleUploadedFile("v7.zip", output.getvalue()))
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        restored = CrossCheckServiceJob.objects.get(pk=job.pk)
        self.assertEqual(restored.attempts.count(), 1)
        self.assertIsNotNone(restored.items.get(paper_id="P001").results.get().applied_at)
        self.begin(restored)

    def test_restore_failure_after_history_insertion_rolls_back_records_and_archives(self):
        job = self.partial()
        self.begin(job)
        self.receive(job)
        snapshot = export_system_state()
        preview = preview_system_state_restore(SimpleUploadedFile("state.zip", Path(snapshot["path"]).read_bytes()))
        before_submissions = list(FinalSubmission.objects.order_by("pk").values())
        before_results = list(CrossCheckServiceResult.objects.order_by("pk").values())
        before_attempts = list(CrossCheckServiceAttempt.objects.order_by("number").values())
        archive_bytes = {run.pk: archive_path(job, run).read_bytes() for run in job.attempts.all()}
        with patch("submissions.services.system_state.sync_final_submission_state_records", side_effect=OSError("restore sync failed")):
            with self.assertRaises(OSError):
                apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before_submissions)
        self.assertEqual(list(CrossCheckServiceResult.objects.order_by("pk").values()), before_results)
        self.assertEqual(list(CrossCheckServiceAttempt.objects.order_by("number").values()), before_attempts)
        for run in job.attempts.all():
            self.assertEqual(archive_path(job, run).read_bytes(), archive_bytes[run.pk])

    def test_clear_database_cascades_retry_parent_links_and_results(self):
        job = self.partial()
        self.begin(job)
        self.receive(job)
        response = self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CrossCheckServiceAttempt.objects.exists())
        self.assertFalse(CrossCheckServiceResult.objects.exists())
        self.assertFalse(job_root(job).exists())

    def test_applied_results_are_not_replayed_after_later_manual_score_edits(self):
        job = self.partial()
        apply_service_results(job.pk, job_evidence_token(job))
        self.submission.refresh_from_db()
        self.submission.similarity_score = 9
        self.submission.single_similarity_score = 4
        self.submission.save()
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        job.refresh_from_db()
        self.begin(job)
        self.receive(job)
        repreview_service_results(job.pk)
        job.refresh_from_db()
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

    def test_new_active_final_with_identical_bytes_cannot_receive_old_retry_result(self):
        job = self.partial()
        self.begin(job)
        self.second.active_version = False
        self.second.save()
        replacement = FinalSubmission.objects.create(final_submission_id="999", paper_id_filled="P002", active_version=True)
        replacement.pdf_file.save("replacement.pdf", ContentFile(Path(self.second.pdf_file.path).read_bytes()))
        before = FinalSubmission.objects.values().get(pk=replacement.pk)
        self.receive(job)
        self.assertEqual(job.items.get(paper_id="P002").status, "stale")
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=replacement.pk), before)

    def test_mixed_changed_publications_require_acknowledgment_and_still_cannot_apply_old_results(self):
        self.second = self.second_submission()
        third_master = InitialPaper.objects.create(paper_id="P003", title="Third paper")
        third = FinalSubmission.objects.create(final_submission_id="30", paper_id_filled=third_master.paper_id, active_version=True)
        third.pdf_file.save("third.pdf", ContentFile(b"%PDF-third"))
        job = self.preview()
        self.ready(job, outcomes={"P002": {"status": "wait_timeout"}, "P003": {"status": "wait_timeout"}})
        before = FinalSubmission.objects.values().get(pk=self.second.pk)
        Path(self.second.pdf_file.path).write_bytes(b"%PDF-changed")
        client = Mock()
        client.json.return_value = self.response(job, retry_remaining=2)
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "Confirm"):
                retry_service_downloads(job.pk, job_evidence_token(job), 10)
        client.retry_download.assert_not_called()
        self.begin(job, count=2, acknowledge_stale=True)
        self.receive(job, paper_ids=["P002", "P003"])
        apply_service_results(job.pk, job_evidence_token(job))
        self.second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.second.pk), before)
        self.assertEqual(third.similarity_score, 42)

    def test_external_completed_retry_is_recovered_with_exact_parent_without_post(self):
        job = self.partial()
        original = initial_attempt(job)
        external = {"id": "externalretry", "kind": "retry_download", "status": "completed", "can_download": True,
            "settings": {"folder": job.folder, "postfix": "_" + job.token, "file_count": 1,
                         "mode": "retry_download", "source_job_id": original.remote_id,
                         "parent_job_id": original.remote_id, "wait_minutes": 10}}
        client = Mock()
        client.json.side_effect = [self.response(job), {"jobs": [external]}]
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            refresh_service_job(job.pk)
        self.assertEqual(active_attempt(job).remote_id, "externalretry")
        self.receive(job)
        client.retry_download.assert_not_called()
        client.upload.assert_not_called()
        self.assertEqual(job.items.get(paper_id="P002").status, "downloaded")

    def test_result_retrieval_audit_failure_leaves_no_partial_archive_or_result(self):
        job = self.partial()
        execution = self.begin(job)
        before = list(job.items.values())
        with patch("submissions.services.crosscheck_service_history.audit_success", side_effect=OSError("audit unavailable")):
            with self.assertRaises(OSError):
                self.receive(job)
        self.assertFalse(archive_path(job, execution).exists())
        self.assertFalse(execution.results.exists())
        self.assertEqual(list(job.items.values()), before)

    def test_apply_audit_failure_rolls_back_both_executions_and_created_reports(self):
        job = self.partial()
        self.begin(job)
        self.receive(job)
        before = list(FinalSubmission.objects.order_by("pk").values())
        with patch("submissions.services.crosscheck_service.audit_success", side_effect=OSError("audit unavailable")):
            with self.assertRaises(OSError):
                apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        self.assertFalse(job.items.filter(status="applied").exists())
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))

    def test_retry_wait_limits_invalid_values_do_not_contact_service(self):
        job = self.partial()
        for value in (0, 1441, "1.5", True, "", None):
            with self.subTest(value=value), patch("submissions.services.crosscheck_service.CrossCheckClient") as client:
                with self.assertRaises(ValueError):
                    retry_service_downloads(job.pk, job_evidence_token(job), value)
                client.assert_not_called()
        self.assertEqual(job.attempts.count(), 1)

    def test_submitted_papers_do_not_claim_they_are_still_awaiting_upload(self):
        job = self.preview()
        execution = initial_attempt(job)
        execution.status = job.status = "running"
        execution.remote_id = job.remote_id = "running-upload"
        execution.progress = {"phase": "waiting", "succeeded": 1, "ready": 0, "downloaded": 0}
        execution.save()
        job.save()
        before = list(FinalSubmission.objects.values())
        context = batch_context(job)
        self.assertEqual(context["items"][0].status_label, "Awaiting results")
        page = self.client.get(reverse("submissions:crosscheck_service_job", args=[job.pk]))
        self.assertContains(page, "Uploads confirmed: 1 / 1")
        self.assertContains(page, "Reports ready: 0")
        self.assertNotContains(page, "Awaiting upload")
        self.assertEqual(list(FinalSubmission.objects.values()), before)

    def test_new_archive_requires_both_history_payloads_and_valid_parent_scope(self):
        job = self.partial()
        self.begin(job)
        self.receive(job)
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        for defect in ("missing", "parent", "result"):
            entries = dict(original)
            state, manifest = (json.loads(entries[name]) for name in ("state.json", "manifest.json"))
            if defect == "missing":
                state["models"].pop("crosscheck_service_results")
                manifest["record_counts"].pop("crosscheck_service_results")
            elif defect == "parent":
                retry = state["models"]["crosscheck_service_attempts"][1]
                retry["parent_id"] = retry["id"]
            else:
                state["models"]["crosscheck_service_results"][-1]["attempt_id"] = "00000000-0000-4000-8000-000000000000"
            for payload in (state, manifest):
                payload["model_payload_sha256"] = _model_payload_sha256(state["models"])
            entries.update({"state.json": json.dumps(state), "manifest.json": json.dumps(manifest)})
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)
            with self.subTest(defect=defect), self.assertRaises(SystemStateError):
                preview_system_state_restore(SimpleUploadedFile("invalid.zip", output.getvalue()))

    def test_retry_client_sends_only_wait_and_optional_ieee_credentials_as_json(self):
        from submissions.services.crosscheck_client import CrossCheckClient
        client = CrossCheckClient("http://qa-crosscheck:18765")
        with patch.object(client, "json", return_value={}) as send:
            client.retry_download("remote123", 1)
        send.assert_called_once_with("/api/jobs/remote123/retry-download", method="POST", payload={"wait_minutes": 1})
        client.credentials.update(ieee_username="test-user", ieee_password="test-only-secret")
        with patch.object(client, "json", return_value={}) as send:
            client.retry_download("retry1", 1440)
        send.assert_called_once_with("/api/jobs/retry1/retry-download", method="POST",
                                    payload={"wait_minutes": 1440, "username": "test-user", "password": "test-only-secret"})

    def test_original_and_retry_status_cannot_be_swapped_even_with_matching_count(self):
        from submissions.services.crosscheck_service_history import remote_matches
        job = self.partial()
        with self.assertRaises(ValueError):
            remote_matches(job, self.response(job, kind="retry_download"), initial_attempt(job))

    def test_summary_and_scoped_file_endpoints_are_read_only_and_keep_each_execution_bytes(self):
        job = self.partial()
        apply_service_results(job.pk, job_evidence_token(job))
        job.refresh_from_db()
        self.begin(job)
        self.receive(job)
        context = batch_context(job)
        self.assertEqual(context["counts"], {"total": 2, "applied": 1, "ready": 1, "waiting": 0, "attention": 0})
        before = list(FinalSubmission.objects.order_by("pk").values())
        for run in job.attempts.all():
            response = self.client.get(reverse("submissions:crosscheck_service_attempt_archive", args=[job.pk, run.pk]))
            self.assertEqual(b"".join(response.streaming_content), archive_path(job, run).read_bytes())
            response.close()
            for result in run.results.exclude(report_sha256=""):
                response = self.client.get(reverse("submissions:crosscheck_service_result_report", args=[job.pk, result.pk]))
                self.assertEqual(b"".join(response.streaming_content), b"%PDF-report bytes")
                response.close()
        second_job = self.preview()
        result = job.attempts.last().results.get()
        self.assertEqual(self.client.get(reverse("submissions:crosscheck_service_result_report", args=[second_job.pk, result.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submissions:crosscheck_service_attempt_archive", args=[second_job.pk, result.attempt_id])).status_code, 404)
        page = self.client.get(reverse("submissions:crosscheck_service_job", args=[job.pk]))
        self.assertContains(page, "Execution history (2)")
        self.assertContains(page, "Retry 1")
        self.assertNotContains(page, ">Completed<")
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)

    def test_retry_form_is_explicit_and_invalid_post_preserves_input_without_network(self):
        job = self.partial()
        url = reverse("submissions:crosscheck_service_job", args=[job.pk])
        self.assertNotContains(self.client.get(url), 'id="retry-wait-minutes"')
        self.assertContains(self.client.get(url + "?retry=1"), 'id="retry-wait-minutes"')
        with patch("submissions.services.crosscheck_service.CrossCheckClient") as client:
            page = self.client.post(url, {"action": "retry", "evidence_token": job_evidence_token(job), "wait_minutes": "1500"})
            self.assertContains(page, 'value="1500"')
            self.assertContains(page, "Use 1 to 1440")
            client.assert_not_called()

    def test_cleanup_preserves_every_attempt_under_overlapping_report_root(self):
        from submissions.models import AppSetting
        from submissions.services.storage_inventory import preview_storage_cleanup
        job = self.partial()
        self.begin(job)
        self.receive(job)
        config = AppSetting.load()
        config.reports_folder = "data/crosscheck_service"
        config.save()
        for policy in ("generated_cache_or_orphan_output", "generated_reports_exports"):
            preview = preview_storage_cleanup(policy=policy)
            self.assertFalse(any("crosscheck_service" in str(row) for row in preview["files"]))
        for run in job.attempts.all():
            self.assertTrue(archive_path(job, run).is_file())
