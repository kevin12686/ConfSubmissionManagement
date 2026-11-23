import hashlib
import subprocess
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from submissions.controllers.pdf_comparison import _one_shot_slot
from submissions.models import FinalSubmission, FormattingRevision
from submissions.services.checks import publication_readiness_rows
from submissions.services.crosscheck import prepare_crosscheck_upload
from submissions.services.pdf_comparison import MAX_BYTES, create_pdf_comparison
from submissions.services.pdf_comparison_errors import LocalComparisonConfirmationRequired, PDFComparisonError
from submissions.services.publication_read import publication_database_signature
from submissions.services.reports import export_publication_package
from submissions.tests.ocr_fixtures import synthetic_ocr_worker
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase
from submissions.tests.test_pdf_comparison import make_pdf


class PDFComparisonOnceTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(BASE_DIR=self.root.resolve(), CFM_COMPARISON_TRANSPORT="embedded",
                                           CFM_COMPARISON_REMOTE_URL=""))
        self.worker = self.enterContext(patch("submissions.services.pdf_comparison_execution.subprocess.run",
                                              side_effect=synthetic_ocr_worker))
        self.make_master_paper()
        self.submission = self.make_final_submission()
        self.before = make_pdf(["Original submission"])
        self.after = make_pdf(["Corrected submission"])
        self.submission.pdf_file.save("original.pdf", ContentFile(self.before))
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(self.after))
        self.page_url = self.url("formatting_compare_once")
        self.input_url = self.url("formatting_compare_once_input")

    def url(self, name):
        return reverse(f"submissions:{name}", args=[self.submission.pk])

    def baseline(self, selection="original"):
        response = self.client.get(self.input_url, {"baseline": selection})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        return response

    def compare(self, token, draft=None, **headers):
        return self.client.post(self.input_url, self.after if draft is None else draft, content_type="application/pdf",
                                HTTP_X_CFM_BASELINE=token, **headers)

    def files(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_cpu_only_install_keeps_same_engine_result_without_persistence(self):
        reference = create_pdf_comparison(self.submission.pk)["result"]
        baseline = self.baseline()
        self.assertEqual(baseline.content, self.before)
        self.assertContains(self.client.get(self.page_url), "Compare without saving")
        self.assertNotContains(self.client.get(self.page_url), "once-backend")
        listing = self.client.get(reverse("submissions:formatting"), {"mode": "focus", "submission": self.submission.pk})
        self.assertContains(listing, "Compare without saving")
        rows = list(FinalSubmission.objects.values()), list(FormattingRevision.objects.values())
        signature, readiness, files = publication_database_signature(), publication_readiness_rows(strict_hash=True), self.files()
        with patch("submissions.services.comparison_client.one_time_gpu_evidence") as remote, \
                patch("submissions.services.pdf_comparison_execution.comparison_cache_job") as cache, \
                patch("submissions.services.pdf_comparison_execution.tempfile.TemporaryFile") as temporary:
            response = self.compare(baseline["X-CFM-Baseline"], HTTP_X_CFM_DRAFT_NAME="%3Cimg%20src%3Dx%3E.pdf")
            remote.assert_not_called()
            cache.assert_not_called()
            temporary.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        value = response.json()
        self.assertEqual({key: value["data"][key] for key in reference}, reference)
        self.assertEqual(value["data"]["after"]["sha256"], hashlib.sha256(self.after).hexdigest())
        self.assertNotIn("token", value["data"])
        self.assertIn("Unsaved draft", value["workspace"])
        self.assertIn("&lt;img src=x&gt;.pdf", value["workspace"])
        self.assertNotIn("Open PDF", value["workspace"])
        args, kwargs = self.worker.call_args
        self.assertNotIn("--cache-inputs", args[0])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["CFM_OCR_BACKEND"], "cpu")
        self.assertEqual(self.files(), files)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)
        self.assertEqual((list(FinalSubmission.objects.values()), list(FormattingRevision.objects.values())), rows)
        self.assertNotContains(self.client.get(self.page_url), "Compared by Local CPU")

    def test_current_baseline_uses_exact_corrected_bytes_and_missing_corrected_does_not_fall_back(self):
        base = self.baseline("current")
        self.assertEqual(base.content, self.after)
        response = self.compare(base["X-CFM-Baseline"])
        self.assertTrue(response.json()["data"]["identical_bytes"])
        self.assertEqual(response.json()["data"]["before"]["label"], "Current saved PDF (Corrected)")
        Path(self.submission.formatted_pdf_file.path).unlink()
        self.assertEqual(self.client.get(self.input_url, {"baseline": "current"}).status_code, 409)
        self.assertEqual(self.baseline().content, self.before)

    def test_exports_still_contain_saved_corrected_pdf_and_source_not_the_draft(self):
        self.mark_submission_publication_ready(self.submission)
        draft = make_pdf(["This transient draft must never appear in an export."])
        source_bytes = Path(self.submission.source_file.path).read_bytes()
        before = publication_database_signature()
        baseline = self.baseline("current")["X-CFM-Baseline"]
        files = self.files()
        cpu = self.compare(baseline, draft)
        self.assertEqual(cpu.status_code, 200, cpu.content[:400])
        result = {key: value for key, value in cpu.json()["data"].items() if key not in {"before", "after", "execution"}}
        self.worker.reset_mock()
        with override_settings(CFM_COMPARISON_REMOTE_URL="http://example.invalid"), \
                patch("submissions.services.comparison_client.one_time_gpu_evidence", return_value=result) as gpu:
            response = self.compare(baseline, draft)
            gpu.assert_called_once_with(self.after, draft)
            self.worker.assert_not_called()
            gpu.side_effect = LocalComparisonConfirmationRequired("Remote GPU is busy.")
            consent = self.compare(baseline, draft).json()["local_confirmation"]
            self.worker.assert_not_called()
            fallback = self.compare(baseline, draft, HTTP_X_CFM_LOCAL_CONFIRMATION=consent)
            self.assertEqual(fallback.json(), cpu.json())
            self.assertEqual(gpu.call_count, 2)
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.json()["data"], {**cpu.json()["data"], "execution": {"label": "GPU"}})
        self.assertEqual(self.files(), files)
        self.assertEqual(publication_database_signature(), before)
        with zipfile.ZipFile(export_publication_package()) as package:
            [paper] = [name for name in package.namelist() if name.startswith("PDF/") and name.endswith(".pdf")]
            [source] = [name for name in package.namelist() if name.startswith("Source/") and not name.endswith("/")]
            self.assertEqual(package.read(paper), self.after)
            self.assertEqual(package.read(source), source_bytes)
        with zipfile.ZipFile(prepare_crosscheck_upload("ONCE")["zip_path"]) as package:
            self.assertEqual(package.read("P001_ONCE.pdf"), self.after)

    @override_settings(CFM_COMPARISON_REMOTE_URL="http://example.invalid")
    def test_gpu_needs_explicit_cpu_confirmation_bound_to_exact_input_and_config(self):
        baseline = self.baseline()["X-CFM-Baseline"]
        files, signature = self.files(), publication_database_signature()
        with patch("submissions.services.comparison_client.one_time_gpu_evidence") as gpu:
            gpu.side_effect = LocalComparisonConfirmationRequired("Remote GPU is busy.")
            response = self.compare(baseline, HTTP_X_CFM_COMPARE_BACKEND="cpu")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(set(response.json()), {"error", "local_confirmation"})
            consent = response.json()["local_confirmation"]
            gpu.assert_called_once_with(self.before, self.after)

            for selected, draft, confirmation in (
                (baseline, self.after, consent + "tampered"),
                (baseline, self.before, consent),
                (self.baseline("current")["X-CFM-Baseline"], self.after, consent),
            ):
                rejected = self.compare(selected, draft, HTTP_X_CFM_LOCAL_CONFIRMATION=confirmation)
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(set(rejected.json()), {"error"})
            with override_settings(CFM_COMPARISON_REMOTE_TOKEN="rotated"):
                self.assertEqual(self.compare(baseline, HTTP_X_CFM_LOCAL_CONFIRMATION=consent).status_code, 409)
            with patch("django.core.signing.time.time", return_value=time.time() + 301):
                self.assertEqual(self.compare(baseline, HTTP_X_CFM_LOCAL_CONFIRMATION=consent).status_code, 409)
            self.assertEqual(gpu.call_count, 1)
            self.worker.assert_not_called()

            gpu.side_effect = PDFComparisonError("The GPU may still be working.")
            lost_reply = self.compare(baseline)
            self.assertEqual(lost_reply.status_code, 409)
            self.assertEqual(lost_reply.json(), {"error": "The GPU may still be working."})
        self.worker.assert_not_called()
        self.assertEqual(self.files(), files)
        self.assertEqual(publication_database_signature(), signature)

    def test_draft_errors_do_not_open_disk_upload_handlers_or_run_ocr(self):
        token = self.baseline()["X-CFM-Baseline"]
        before = self.files()
        with patch("django.core.files.uploadhandler.TemporaryFileUploadHandler.new_file") as upload:
            self.assertEqual(self.compare(token, b"not a PDF").status_code, 409)
            self.assertEqual(self.client.post(self.input_url, {"baseline": "original"}).status_code, 415)
            self.assertEqual(self.compare(token, CONTENT_LENGTH=str(MAX_BYTES + 1)).status_code, 413)
            self.assertEqual(self.compare(token, CONTENT_LENGTH="-1").status_code, 411)
            self.assertEqual(self.compare(token, CONTENT_LENGTH="0").status_code, 413)
            strict_client = Client(enforce_csrf_checks=True)
            with override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=1):
                rejected = strict_client.post(self.input_url, {"draft": SimpleUploadedFile("draft.pdf", self.after)})
            self.assertEqual(rejected.status_code, 415)
            upload.assert_not_called()
        self.worker.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_bound_token_rejects_other_submission_expiry_and_changed_baseline(self):
        token = self.baseline()["X-CFM-Baseline"]
        other = self.make_master_paper("P002")
        second = self.make_final_submission(paper_id_filled=other.paper_id, final_submission_id="2")
        wrong_url = reverse("submissions:formatting_compare_once_input", args=[second.pk])
        self.assertEqual(self.client.post(wrong_url, self.after, content_type="application/pdf", HTTP_X_CFM_BASELINE=token).status_code, 409)
        self.assertEqual(self.compare(token + "tampered").status_code, 409)
        with patch("django.core.signing.time.time", return_value=time.time() + 901):
            self.assertEqual(self.compare(token).status_code, 409)
        Path(self.submission.pdf_file.path).write_bytes(make_pdf(["Different baseline"]))
        self.assertEqual(self.compare(token).status_code, 409)
        self.worker.assert_not_called()

    def test_baseline_or_active_identity_change_during_compute_is_rejected(self):
        token = self.baseline()["X-CFM-Baseline"]

        def changed(*args, **kwargs):
            result = synthetic_ocr_worker(*args, **kwargs)
            FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=False)
            return result

        self.worker.side_effect = changed
        response = self.compare(token)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("workspace", response.json())

    def test_csrf_busy_and_failed_ocr_do_not_write_or_return_partial_evidence(self):
        token = self.baseline()["X-CFM-Baseline"]
        client = Client(enforce_csrf_checks=True)
        denied = client.post(self.input_url, self.after, content_type="application/pdf", HTTP_X_CFM_BASELINE=token)
        self.assertEqual(denied.status_code, 403)
        before = self.files()
        with _one_shot_slot:
            self.assertEqual(self.compare(token).status_code, 429)
        self.worker.side_effect = subprocess.TimeoutExpired("ocr", 1)
        response = self.compare(token)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(response.json()), {"error"})
        self.assertEqual(self.files(), before)
        self.worker.side_effect = synthetic_ocr_worker
        self.assertEqual(self.compare(token).status_code, 200)
