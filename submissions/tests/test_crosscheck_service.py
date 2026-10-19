import csv
import hashlib
import io
import json
import tempfile
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from submissions.forms import AppSettingForm
from submissions.models import AppSetting, CrossCheckServiceJob, FinalSubmission, InitialPaper
from submissions.services.crosscheck_client import CrossCheckClient, CrossCheckServiceError, read_service_secrets, save_service_secrets
from submissions.services.crosscheck_service import (
    apply_service_results, batch_prefix, job_evidence_token, job_root,
    parse_service_archive, preview_service_job, refresh_service_job, start_service_job,
)
from submissions.services.storage_inventory import preview_storage_cleanup


class CrossCheckServiceTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.root = Path(temp.name).resolve()
        self.addCleanup(temp.cleanup)
        override = override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.root / "data/media")
        override.enable()
        self.addCleanup(override.disable)
        config = AppSetting.load()
        config.conference_name = "DSA 2026"
        config.crosscheck_service_enabled = True
        config.crosscheck_service_folder = "DSA 2026 folder"
        config.crosscheck_service_conference = config.conference_name
        config.save()
        self.paper = InitialPaper.objects.create(paper_id="P001", title="Test paper", authors="Ada")
        self.submission = FinalSubmission.objects.create(final_submission_id="10", paper_id_filled="P001", active_version=True,
                                                        similarity_score=8, single_similarity_score=2, paper_id_verified=True)
        self.submission.pdf_file.save("original.pdf", ContentFile(b"%PDF-original publication"), save=True)
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(b"%PDF-corrected publication"), save=True)

    def preview(self):
        return preview_service_job("all")

    def test_folder_selector_preserves_complete_saved_and_bound_names(self):
        name = "2026 International Conference on Reliable, Dependable and Distributed Systems (DSA)"
        config = AppSetting.load()
        config.crosscheck_service_folder = name
        form = AppSettingForm(instance=config)
        self.assertIn((name, name), form.fields["crosscheck_service_folder"].widget.choices)
        self.assertInHTML(f'<option value="{name}" selected>{name}</option>', str(form["crosscheck_service_folder"]))
        imported_name = name + " - Alternative destination"
        bound = AppSettingForm(data={"crosscheck_service_folder": imported_name}, instance=config)
        self.assertIn((imported_name, imported_name), bound.fields["crosscheck_service_folder"].widget.choices)
        self.assertEqual(bound["crosscheck_service_folder"].value(), imported_name)

    def remote(self, job):
        return {"id": "remote123", "settings": {"folder": job.folder, "postfix": "_" + job.token, "file_count": job.items.count()}}

    def archive(self, job, *, status="downloaded", upload="success", extra=None):
        path = job_root(job) / "fixture.zip"
        def table(fields, rows, delimiter=","):
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fields, delimiter=delimiter)
            writer.writeheader()
            writer.writerows(rows)
            return buf.getvalue()
        manifests, uploads, batches, scores, reports = [], [], [], [], []
        for item in job.items.all():
            original = f"{item.paper_id}.pdf"
            filename = f"{item.paper_id}_{job.token}.pdf"
            manifests.append({"original_name": original, "upload_name": filename})
            uploads.append({"OriginalFile": original, "UploadedFile": filename, "Status": upload})
            batches.append({"OriginalFile": original, "UploadedFile": filename, "DocumentID": "doc" + item.paper_id, "Status": status})
            if status == "downloaded":
                scores.append({"FileName": filename, "TotalSimilarity": "42%", "SingleSimilarity": "<1%", "Download": "True"})
                reports.append(("reports/" + filename + ".pdf", b"%PDF-report bytes"))
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(".info/upload_manifest.json", json.dumps(manifests))
            z.writestr(".info/upload_results.csv", table(["OriginalFile", "UploadedFile", "Status"], uploads))
            z.writestr(".info/batch_results.csv", table(["OriginalFile", "UploadedFile", "DocumentID", "Status"], batches))
            z.writestr("CrossCheck_output.txt", table(["FileName", "TotalSimilarity", "SingleSimilarity", "Download"], scores, "\t"))
            for name, data in reports:
                z.writestr(name, data)
            if extra:
                z.writestr(*extra)
        return path

    def rewrite_archive(self, path, name, transform):
        with zipfile.ZipFile(path) as archive:
            entries = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        entries[name] = transform(entries[name])
        with zipfile.ZipFile(path, "w") as archive:
            for member, content in entries.items():
                archive.writestr(member, content)

    def second_submission(self):
        InitialPaper.objects.create(paper_id="P002", title="Second paper", authors="Grace")
        submission = FinalSubmission.objects.create(final_submission_id="20", paper_id_filled="P002", active_version=True,
                                                    similarity_score=7, single_similarity_score=3, paper_id_verified=True)
        submission.pdf_file.save("second.pdf", ContentFile(b"%PDF-second publication"), save=True)
        return submission

    def ready(self, job, **kwargs):
        path = self.archive(job, **kwargs)
        for item, values in parse_service_archive(job, path):
            for key, value in values.items():
                setattr(item, key, value)
            item.save()
        target = job_root(job) / "results.zip"
        path.replace(target)
        job.archive_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        job.status = "ready"
        job.save()

    def test_token_and_snapshot_are_publication_bytes_and_preview_is_read_only(self):
        self.assertEqual(batch_prefix("DSA / 2026!", date(2026, 10, 18)), "DSA_2026_CFM_OCT182026_")
        before = self.submission.updated_at
        job = self.preview()
        second = self.preview()
        self.assertEqual(second.batch_number, job.batch_number + 1)
        self.assertEqual((job_root(job) / "inputs/P001.pdf").read_bytes(), b"%PDF-corrected publication")
        item = job.items.get()
        self.assertEqual(item.source, "corrected")
        self.assertEqual(item.pdf_sha256, hashlib.sha256(b"%PDF-corrected publication").hexdigest())
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.updated_at, before)
        self.assertEqual(self.submission.similarity_score, 8)

    def test_default_account_upload_and_busy_do_not_retry(self):
        job = self.preview()
        client = Mock()
        client.folders.return_value = {"folders": [job.folder]}
        client.json.return_value = {"active_id": None}
        client.upload.return_value = self.remote(job)
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            start_service_job(job.pk, job_evidence_token(job))
            with self.assertRaises(ValueError):
                start_service_job(job.pk, job_evidence_token(job))
        self.assertEqual(client.upload.call_count, 1)
        inputs = client.upload.call_args.args[1]
        self.assertEqual(inputs[0][0], "P001.pdf")
        self.assertEqual(read_service_secrets(job.service_url)["ieee_password"], "")

    def test_http_upload_uses_only_paper_id_and_verified_snapshot_bytes(self):
        job = self.preview()
        path = job_root(job) / "inputs/P001.pdf"
        item = job.items.get()
        captured = {}
        def opened(endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured["body"] = b"".join(kwargs["data"])
            return io.BytesIO(b'{"id":"remote123"}')
        client = CrossCheckClient(job.service_url)
        checked = Mock()
        with patch.object(client, "_open", side_effect=opened):
            client.upload(job, [("P001.pdf", path, item.pdf_sha256)], before_send=checked)
        self.assertIn(b'filename="P001.pdf"', captured["body"])
        self.assertIn(b"%PDF-corrected publication", captured["body"])
        self.assertIn(("_" + job.token).encode(), captured["body"])
        self.assertNotIn(b'name="username"', captured["body"])
        self.assertNotIn(b'name="password"', captured["body"])
        checked.assert_called_once()
        with patch.object(client, "_open") as transport:
            with self.assertRaisesRegex(CrossCheckServiceError, "changed"):
                client.upload(job, [("P001.pdf", path, "0" * 64)])
            transport.assert_not_called()

    def test_ambiguous_submit_is_recovered_without_reupload(self):
        job = self.preview()
        client = Mock()
        client.folders.return_value = {"folders": [job.folder]}
        client.json.return_value = {"active_id": None}
        client.upload.side_effect = CrossCheckServiceError("Connection timed out")
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaises(CrossCheckServiceError):
                start_service_job(job.pk, job_evidence_token(job))
            job.refresh_from_db()
            self.assertEqual(job.status, "uncertain")
            remote = self.remote(job)
            remote.update(status="running", progress={"ready": 0})
            client.json.side_effect = [{"jobs": [remote]}, remote]
            recovered = refresh_service_job(job.pk)
            self.assertEqual(recovered.remote_id, "remote123")
        self.assertEqual(client.upload.call_count, 1)

    def test_pdf_change_or_active_replacement_blocks_submission_and_apply(self):
        job = self.preview()
        self.ready(job)
        old = self.submission.formatted_pdf_file.path
        Path(old).write_bytes(b"%PDF-new publication")
        apply_service_results(job.pk, job_evidence_token(job))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.similarity_score, 8)
        self.assertEqual(job.items.get().status, "stale")

    def test_apply_is_atomic_syncs_mirror_preserves_review_and_makes_waiver_stale(self):
        self.submission.similarity_score = 40
        self.submission.plagiarism_percent_exception_approved = True
        self.submission.plagiarism_percent_exception_reason = "Approved old score"
        self.submission.plagiarism_percent_exception_approved_score = 40
        self.submission.save()
        job = self.preview()
        self.ready(job)
        apply_service_results(job.pk, job_evidence_token(job))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.similarity_score, 42)
        self.assertEqual(self.submission.single_similarity_score, 1)
        self.assertTrue(self.submission.paper_id_verified)
        self.assertEqual(self.submission.plagiarism_percent_exception_approved_score, 40)
        self.assertEqual(self.submission.plagiarism_state.similarity_score, 42)
        self.assertEqual(Path(self.submission.plagiarism_report_path).read_bytes(), b"%PDF-report bytes")
        with self.assertRaises(ValueError):
            apply_service_results(job.pk, job_evidence_token(job))

    def test_apply_failure_rolls_back_scores_and_new_files(self):
        job = self.preview()
        self.ready(job)
        with patch("submissions.services.crosscheck_service.bulk_update_submissions", side_effect=RuntimeError("DB failure")):
            with self.assertRaises(RuntimeError):
                apply_service_results(job.pk, job_evidence_token(job))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.similarity_score, 8)
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))
        self.assertEqual(job.items.get().status, "downloaded")

    def test_partial_and_unconfirmed_results_never_clear_scores(self):
        for kwargs in ({"status": "wait_timeout"}, {"upload": "unconfirmed"}):
            job = self.preview()
            self.ready(job, **kwargs)
            apply_service_results(job.pk, job_evidence_token(job))
            self.submission.refresh_from_db()
            self.assertEqual(self.submission.similarity_score, 8)
            self.assertEqual(self.submission.plagiarism_report_path, "")

    def test_rejects_unsafe_zip_and_other_batch_manifest(self):
        job = self.preview()
        path = self.archive(job, extra=("../escape.txt", b"bad"))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            parse_service_archive(job, path)

    def test_score_change_after_preview_is_not_overwritten(self):
        job = self.preview()
        self.ready(job)
        self.submission.similarity_score = 9
        self.submission.save()
        apply_service_results(job.pk, job_evidence_token(job))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.similarity_score, 9)

    def test_configuration_binding_and_missing_scope(self):
        self.submission.similarity_score = 0
        self.submission.single_similarity_score = 0
        self.submission.save()
        with self.assertRaisesRegex(ValueError, "No unambiguous"):
            preview_service_job("missing_results")
        config = AppSetting.load()
        config.conference_name = "Another conference"
        config.save()
        with self.assertRaisesRegex(ValueError, "Conference name changed"):
            self.preview()

    def test_views_show_integrated_tabs_and_evidence_without_get_mutations(self):
        job = self.preview()
        page = self.client.get(reverse("submissions:crosscheck_service_job", args=[job.pk]))
        self.assertContains(page, "Confirm and start CrossCheck")
        self.assertContains(page, "P001")
        self.assertEqual(CrossCheckServiceJob.objects.get(pk=job.pk).status, "preview")
        self.assertContains(self.client.get(reverse("submissions:integration")), "Automatic service")
        self.assertContains(self.client.get(reverse("submissions:integration") + "?tab=manual"), "Prepare All Publication PDFs ZIP")
        self.assertContains(self.client.get(reverse("submissions:settings")), "Optional account settings")
        item = job.items.get()
        response = self.client.get(reverse("submissions:crosscheck_service_input_pdf", args=[job.pk, item.pk]))
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-corrected publication")
        response.close()
        self.ready(job)
        item.refresh_from_db()
        page = self.client.get(reverse("submissions:crosscheck_service_job", args=[job.pk]))
        self.assertContains(page, "<span>Ready to apply</span>", html=True)
        self.assertContains(page, 'class="d-flex flex-column align-items-start gap-2"')
        self.assertContains(page, "Open report")
        response = self.client.get(reverse("submissions:crosscheck_service_report", args=[job.pk, item.pk]))
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-report bytes")
        response.close()
        (job_root(job) / "results.zip").write_bytes(b"tampered")
        self.assertEqual(self.client.get(reverse("submissions:crosscheck_service_report", args=[job.pk, item.pk])).status_code, 404)

    def test_cleanup_preserves_job_evidence_and_secrets_are_host_bound(self):
        job = self.preview()
        self.ready(job)
        save_service_secrets(job.service_url, {"ieee_username": "editor", "ieee_password": "very-secret"})
        secret = self.root / "data/secrets/crosscheck.json"
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_service_secrets("http://other-host:18765")["ieee_password"], "")
        log = (self.root / "data/logs/audit.log").read_text()
        self.assertNotIn("very-secret", log)
        paths = [x["path"] for x in preview_storage_cleanup("generated_reports_exports")["files"]]
        self.assertNotIn(str(job_root(job) / "results.zip"), paths)

    def test_secret_update_rolls_back_if_audit_cannot_be_written(self):
        url = AppSetting.read().crosscheck_service_url
        save_service_secrets(url, {"ieee_username": "editor", "ieee_password": "previous-secret"})
        with patch("submissions.services.audit.audit_success", side_effect=OSError("Audit unavailable")):
            with self.assertRaises(OSError):
                save_service_secrets(url, {"ieee_password": "new-secret"})
        self.assertEqual(read_service_secrets(url)["ieee_password"], "previous-secret")

    def test_state_roundtrip_keeps_job_evidence_and_excludes_secrets(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from submissions.services.system_state import export_system_state, preview_system_state_restore, apply_system_state_restore

        job = self.preview()
        self.ready(job)
        save_service_secrets(job.service_url, {"ieee_username": "editor", "ieee_password": "secret-not-in-archive"})
        snapshot = export_system_state()
        body = Path(snapshot["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertFalse(any("/secrets/" in name for name in archive.namelist()))
            state = json.loads(archive.read("state.json"))
            self.assertEqual(state["state_archive_version"], 7)
            self.assertEqual(len(state["models"]["crosscheck_service_jobs"]), 1)
            self.assertNotIn("secret-not-in-archive", archive.read("state.json").decode())
        with override_settings(SECRET_KEY="receiving-machine-key"):
            preview = preview_system_state_restore(SimpleUploadedFile("snapshot.zip", body))
            apply_system_state_restore(preview["token"], "RESTORE SYSTEM STATE")
            restored = CrossCheckServiceJob.objects.get(pk=job.pk)
            self.assertEqual((job_root(restored) / "inputs/P001.pdf").read_bytes(), b"%PDF-corrected publication")
            self.assertTrue((self.root / "data/secrets/crosscheck.json").exists())
            apply_service_results(restored.pk, job_evidence_token(restored))
            self.submission.refresh_from_db()
            self.assertEqual(self.submission.similarity_score, 42)

    def test_missing_selected_corrected_never_falls_back_to_original(self):
        Path(self.submission.formatted_pdf_file.path).unlink()
        with self.assertRaisesRegex(ValueError, "No unambiguous"):
            self.preview()

    def test_new_active_version_does_not_receive_old_results(self):
        job = self.preview()
        self.ready(job)
        self.submission.active_version = False
        self.submission.save()
        replacement = FinalSubmission.objects.create(final_submission_id="11", paper_id_filled="P001", active_version=True)
        replacement.pdf_file.save("replacement.pdf", ContentFile(b"%PDF-replacement"), save=True)
        apply_service_results(job.pk, job_evidence_token(job))
        replacement.refresh_from_db()
        self.assertIsNone(replacement.similarity_score)
        self.assertEqual(job.items.get().status, "stale")

    def test_ready_response_is_validated_before_local_state_changes(self):
        job = self.preview()
        job.status = "running"
        job.remote_id = "remote123"
        job.save()
        path = self.archive(job)
        client = Mock()
        remote = self.remote(job)
        remote.update(can_download=True, progress={"downloaded": 1}, status="completed")
        client.json.return_value = remote
        client.download.side_effect = lambda remote_id, target: target.write_bytes(path.read_bytes())
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            ready = refresh_service_job(job.pk)
        self.assertEqual(ready.status, "ready")
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.similarity_score, 8)

    def test_clear_database_removes_jobs_and_local_secrets(self):
        job = self.preview()
        save_service_secrets(job.service_url, {"ieee_username": "editor", "ieee_password": "test-secret"})
        response = self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CrossCheckServiceJob.objects.exists())
        self.assertFalse(job_root(job).exists())
        self.assertFalse((self.root / "data/secrets/crosscheck.json").exists())

    def test_same_bytes_on_a_different_final_never_receive_previous_results(self):
        job = self.preview()
        self.ready(job)
        self.submission.active_version = False
        self.submission.save()
        replacement = FinalSubmission.objects.create(final_submission_id="11", paper_id_filled="P001", active_version=True)
        replacement.pdf_file.save("same-bytes.pdf", ContentFile(Path(self.submission.formatted_pdf_file.path).read_bytes()), save=True)
        before = list(FinalSubmission.objects.order_by("pk").values())
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        self.assertEqual(job.items.get().status, "stale")
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))

    def test_scope_changes_after_result_preview_do_not_write_scores(self):
        for transition in ("discard", "not_publishing", "decision_required", "remove_master"):
            with self.subTest(transition=transition):
                job = self.preview()
                self.ready(job)
                try:
                    if transition == "discard":
                        self.submission.discarded = True
                        self.submission.save()
                    elif transition == "remove_master":
                        InitialPaper.objects.filter(pk=self.paper.pk).delete()
                    else:
                        decision = {"publication_decision_status": transition}
                        if transition == "not_publishing":
                            decision.update(publication_exclusion_reason="withdrawn", publication_excluded_at=timezone.now())
                            self.submission.excluded_from_publication = True
                            self.submission.save()
                        InitialPaper.objects.filter(pk=self.paper.pk).update(**decision)
                    before = FinalSubmission.objects.values().get(pk=self.submission.pk)
                    if transition == "decision_required":
                        with self.assertRaisesRegex(ValueError, "decisions are unresolved"):
                            apply_service_results(job.pk, job_evidence_token(job))
                    else:
                        apply_service_results(job.pk, job_evidence_token(job))
                    self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
                    self.assertEqual(job.items.get().status, "downloaded" if transition == "decision_required" else "stale")
                finally:
                    self.submission.discarded = False
                    self.submission.excluded_from_publication = False
                    self.submission.save()
                    self.paper.publication_decision_status = "publishing"
                    self.paper.save()

    def test_missing_corrected_after_preview_does_not_use_original_or_debug(self):
        job = self.preview()
        self.ready(job)
        debug = self.root / "data/publication_pdf_debug/P001.pdf"
        debug.parent.mkdir(parents=True)
        debug.write_bytes(Path(self.submission.formatted_pdf_file.path).read_bytes())
        Path(self.submission.formatted_pdf_file.path).unlink()
        self.submission.current_file_path = str(debug)
        self.submission.save()
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(job.items.get().status, "stale")

    def test_mixed_stale_and_valid_batch_updates_only_the_bound_paper(self):
        second = self.second_submission()
        job = self.preview()
        self.ready(job)
        Path(self.submission.formatted_pdf_file.path).write_bytes(b"%PDF-changed after results")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        second.refresh_from_db()
        self.assertEqual(second.similarity_score, 42)
        self.assertEqual(second.single_similarity_score, 1)
        self.assertEqual(dict(job.items.values_list("paper_id", "status")), {"P001": "stale", "P002": "applied"})
        self.assertEqual(len(list((self.root / "data/plagiarism_reports").glob("*.pdf"))), 1)

    def test_late_second_item_failure_rolls_back_the_entire_batch(self):
        self.second_submission()
        job = self.preview()
        self.ready(job)
        job.items.filter(paper_id="P002").update(report_sha256="0" * 64)
        before = list(FinalSubmission.objects.order_by("pk").values())
        items_before = list(job.items.values())
        with self.assertRaisesRegex(ValueError, "Report bytes changed"):
            apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)
        self.assertEqual(list(job.items.values()), items_before)
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))

    def test_result_audit_failure_preserves_existing_report_and_all_flags(self):
        old_report = self.root / "data/plagiarism_reports/previous.pdf"
        old_report.parent.mkdir(parents=True)
        old_report.write_bytes(b"%PDF-previous report")
        self.submission.plagiarism_report_path = str(old_report)
        self.submission.save()
        job = self.preview()
        self.ready(job)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with patch("submissions.services.crosscheck_service._job_event", side_effect=OSError("Audit unavailable")):
            with self.assertRaises(OSError):
                apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(list(old_report.parent.glob("*.pdf")), [old_report])
        self.assertEqual(old_report.read_bytes(), b"%PDF-previous report")
        self.assertEqual(job.items.get().status, "downloaded")

    def test_tampered_or_stale_apply_evidence_does_not_modify_files_or_scores(self):
        job = self.preview()
        token = job_evidence_token(job)
        self.ready(job)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        for evidence in ("", "invalid-token", token):
            with self.subTest(evidence=evidence):
                with self.assertRaises(ValueError):
                    apply_service_results(job.pk, evidence)
                self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))

    def test_changed_archive_blocks_apply_without_reading_untrusted_reports(self):
        job = self.preview()
        self.ready(job)
        path = job_root(job) / "results.zip"
        self.rewrite_archive(path, job.items.get().report_member, lambda _: b"%PDF-another report")
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with self.assertRaisesRegex(ValueError, "ZIP is missing or changed"):
            apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertFalse(list((self.root / "data/plagiarism_reports").glob("*.pdf")))

    def test_prepared_input_tampering_blocks_upload_before_network_side_effect(self):
        job = self.preview()
        (job_root(job) / "inputs/P001.pdf").write_bytes(b"%PDF-another input")
        client = Mock()
        client.folders.return_value = {"folders": [job.folder]}
        client.json.return_value = {"active_id": None}
        with patch("submissions.services.crosscheck_service.CrossCheckClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "Prepared PDF is missing or changed"):
                start_service_job(job.pk, job_evidence_token(job))
        client.upload.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "preview")

    def test_expired_or_reconfigured_preview_never_contacts_service(self):
        job = self.preview()
        CrossCheckServiceJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=3))
        with patch("submissions.services.crosscheck_service.CrossCheckClient") as client:
            with self.assertRaisesRegex(ValueError, "expired"):
                start_service_job(job.pk, job_evidence_token(job))
            client.assert_not_called()
        job = self.preview()
        AppSetting.objects.update(crosscheck_service_folder="Other conference folder")
        with patch("submissions.services.crosscheck_service.CrossCheckClient") as client:
            with self.assertRaisesRegex(ValueError, "settings changed"):
                start_service_job(job.pk, job_evidence_token(job))
            client.assert_not_called()

    def test_multiple_active_candidates_block_result_apply(self):
        job = self.preview()
        self.ready(job)
        duplicate = FinalSubmission.objects.create(final_submission_id="11", paper_id_filled="P001", active_version=True)
        duplicate.pdf_file.save("ambiguous.pdf", ContentFile(b"%PDF-ambiguous"), save=True)
        before = list(FinalSubmission.objects.order_by("pk").values())
        with self.assertRaises(ValueError):
            apply_service_results(job.pk, job_evidence_token(job))
        self.assertEqual(list(FinalSubmission.objects.order_by("pk").values()), before)

    def test_invalid_scores_and_conflicting_original_identity_are_rejected(self):
        job = self.preview()
        for score in ("NaN", "Infinity", "-1%", "101%", ""):
            with self.subTest(score=score):
                path = self.archive(job)
                self.rewrite_archive(path, "CrossCheck_output.txt", lambda data: data.replace(b"42%", score.encode()))
                with self.assertRaises(ValueError):
                    parse_service_archive(job, path)
        path = self.archive(job)
        self.rewrite_archive(path, ".info/batch_results.csv", lambda data: data.replace(b"P001.pdf", b"P999.pdf"))
        with self.assertRaisesRegex(ValueError, "conflicting original filename"):
            parse_service_archive(job, path)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.similarity_score, 8)

    def test_job_file_endpoints_reject_wrong_parent_and_changed_input(self):
        job = self.preview()
        second_job = self.preview()
        self.ready(job)
        item = job.items.get()
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        for endpoint in ("crosscheck_service_input_pdf", "crosscheck_service_report"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(reverse("submissions:" + endpoint, args=[second_job.pk, item.pk])).status_code, 404)
        (job_root(job) / "inputs/P001.pdf").write_bytes(b"%PDF-changed input")
        self.assertEqual(self.client.get(reverse("submissions:crosscheck_service_input_pdf", args=[job.pk, item.pk])).status_code, 404)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

    def test_v7_restore_rejects_missing_job_payload_and_corrupt_evidence_without_changes(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from submissions.services.system_state import (
            CONFIRMATION_TEXT, SystemStateError, _model_payload_sha256,
            apply_system_state_restore, export_system_state, preview_system_state_restore,
        )

        job = self.preview()
        self.ready(job)
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        items_before = list(job.items.values())
        input_path = job_root(job) / "inputs/P001.pdf"
        for defect in ("crosscheck_service_jobs", "crosscheck_service_items", "input_bytes"):
            with self.subTest(defect=defect):
                entries = original.copy()
                if defect == "input_bytes":
                    member = next(name for name in entries if name.endswith(f"{job.pk}/inputs/P001.pdf"))
                    entries[member] = b"%PDF-corrupt archived input"
                else:
                    state, manifest = (json.loads(entries[name]) for name in ("state.json", "manifest.json"))
                    state["models"].pop(defect)
                    manifest["record_counts"].pop(defect)
                    for payload in (state, manifest):
                        payload["model_payload_sha256"] = _model_payload_sha256(state["models"])
                    entries["state.json"] = json.dumps(state).encode()
                    entries["manifest.json"] = json.dumps(manifest).encode()
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    for name, data in entries.items():
                        archive.writestr(name, data)
                upload = SimpleUploadedFile("invalid-state.zip", output.getvalue())
                if defect == "input_bytes":
                    preview = preview_system_state_restore(upload)
                    self.assertTrue(preview["corrupt_files"])
                    with self.assertRaisesRegex(SystemStateError, "did not pass validation"):
                        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
                else:
                    with self.assertRaisesRegex(SystemStateError, "incomplete"):
                        preview_system_state_restore(upload)
                self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
                self.assertEqual(list(job.items.values()), items_before)
                self.assertEqual(input_path.read_bytes(), b"%PDF-corrected publication")

    def test_restore_preserves_record_times_and_does_not_revive_expired_upload_preview(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils.dateparse import parse_datetime
        from submissions.services.system_state import (
            CONFIRMATION_TEXT, MODEL_SPECS, apply_system_state_restore,
            export_system_state, preview_system_state_restore,
        )

        job = self.preview()
        created = timezone.now() - timedelta(hours=3)
        updated = created + timedelta(minutes=10)
        CrossCheckServiceJob.objects.filter(pk=job.pk).update(created_at=created, updated_at=updated)
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            models = json.loads(archive.read("state.json"))["models"]
        preview = preview_system_state_restore(SimpleUploadedFile("state.zip", Path(snapshot["path"]).read_bytes()))
        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)
        for key, model in MODEL_SPECS:
            fields = [field for field in model._meta.fields if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)]
            for row in models[key]:
                restored = model.objects.get(pk=row[model._meta.pk.attname])
                for field in fields:
                    if field.attname in row:
                        with self.subTest(model=key, pk=str(restored.pk), field=field.name):
                            self.assertEqual(getattr(restored, field.attname), parse_datetime(row[field.attname]))
        restored_job = CrossCheckServiceJob.objects.get(pk=job.pk)
        with patch("submissions.services.crosscheck_service.CrossCheckClient") as client:
            with self.assertRaisesRegex(ValueError, "expired"):
                start_service_job(restored_job.pk, job_evidence_token(restored_job))
            client.assert_not_called()
