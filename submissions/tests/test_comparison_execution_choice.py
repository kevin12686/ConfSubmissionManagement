import time
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse

from submissions.models import FinalSubmission
from submissions.services.comparison_execution_choice import execution_display
from submissions.services.pdf_comparison import create_pdf_comparison, PDFComparisonError
from submissions.services.pdf_comparison_errors import LocalComparisonConfirmationRequired
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase
from submissions.tests.test_pdf_comparison import make_pdf
from submissions.tests.ocr_fixtures import synthetic_ocr_worker


class ExecutionChoiceTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(BASE_DIR=self.root.resolve(), CFM_COMPARISON_TRANSPORT="embedded", CFM_COMPARISON_REMOTE_URL="https://remote.example"))
        self.enterContext(patch("submissions.services.pdf_comparison_execution.subprocess.run", side_effect=synthetic_ocr_worker))
        self.make_master_paper()
        self.submission = self.make_final_submission()
        self.before = make_pdf(["The results are shown in Table 2."])
        self.after = make_pdf(["The results are shown in Table 3."])
        self.submission.pdf_file.save("original.pdf", ContentFile(self.before))
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(self.after))
        self.url = reverse("submissions:formatting_pdf_comparison", args=[self.submission.pk])

    def choice(self, **kwargs):
        with self.assertRaises(LocalComparisonConfirmationRequired) as raised:
            create_pdf_comparison(self.submission.pk, **kwargs)
        return raised.exception.confirmation_token

    def test_manual_request_requires_bound_confirmation_and_records_execution(self):
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        token = self.choice()
        self.assertTrue(token)
        payload = create_pdf_comparison(self.submission.pk, local_confirmation=token)
        self.assertEqual(execution_display(payload["execution"])["label"], "Embedded · CPU")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), self.after)
        self.assertIn('"action": "formatting_pdf_compare_local_confirm"', (self.audit_root / "audit.log").read_text())
        page = self.client.get(self.url, {"comparison": payload["token"]})
        self.assertContains(page, "Compared by Embedded · CPU")

    def test_manual_page_shows_integrated_choice_and_get_cannot_grant_local_permission(self):
        response = self.client.post(self.url, {"baseline": "original", "allow_local": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Comparison not started")
        token = response.context["execution_choice"]["token"]
        self.assertEqual(self.client.get(self.url, {"local_confirmation": token}).status_code, 200)
        self.assertEqual(self.client.post(self.url, {"baseline": "original", "local_confirmation": token}).status_code, 302)

    def test_file_baseline_configuration_and_expiry_changes_reject_consent(self):
        token = self.choice()
        with patch("django.core.signing.time.time", return_value=time.time() + 301):
            with self.assertRaisesRegex(PDFComparisonError, "expired"):
                create_pdf_comparison(self.submission.pk, local_confirmation=token)
        with override_settings(CFM_COMPARISON_REMOTE_HEADERS='{"X-Key":"rotated"}'):
            with self.assertRaisesRegex(PDFComparisonError, "configuration changed"):
                create_pdf_comparison(self.submission.pk, local_confirmation=token)
        from submissions.services.formatting_history import capture_existing_formatting
        revision = capture_existing_formatting(self.submission.pk)
        with self.assertRaisesRegex(PDFComparisonError, "files/configuration changed"):
            create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}", local_confirmation=token)
        Path(self.submission.formatted_pdf_file.path).write_bytes(make_pdf(["Different PDF bytes"]))
        with self.assertRaisesRegex(PDFComparisonError, "files/configuration changed"):
            create_pdf_comparison(self.submission.pk, local_confirmation=token)

    def test_cross_submission_and_new_same_byte_revision_do_not_reuse_confirmation(self):
        token = self.choice()
        other = self.make_final_submission(final_submission_id="OTHER", paper_id_filled="P002")
        self.make_master_paper("P002")
        other.pdf_file.save("same-original.pdf", ContentFile(self.before))
        other.formatted_pdf_file.save("same-after.pdf", ContentFile(self.after))
        with self.assertRaisesRegex(PDFComparisonError, "files/configuration changed"):
            create_pdf_comparison(other.pk, local_confirmation=token)
        from submissions.services.formatting_history import capture_existing_formatting
        capture_existing_formatting(self.submission.pk)
        with self.assertRaisesRegex(PDFComparisonError, "files/configuration changed"):
            create_pdf_comparison(self.submission.pk, local_confirmation=token)

    def test_refusal_happens_before_native_inference(self):
        with patch("submissions.services.pdf_comparison.local_pdf_evidence") as infer:
            self.choice()
        infer.assert_not_called()

    def test_legacy_result_execution_is_unknown_not_inferred_from_current_settings(self):
        self.assertEqual(execution_display(None)["label"], "Worker not recorded")
        self.assertEqual(execution_display({"transport": "service", "worker": "remote", "backend": "cpu"})["label"], "Remote · CPU")
        self.assertEqual(execution_display({"transport": "service", "worker": {}, "backend": {}})["label"], "Worker not recorded")

    def test_service_strip_is_shared_outside_htmx_in_every_formatting_mode_only(self):
        for params in ({"filter": "all"}, {"mode": "focus", "submission": self.submission.pk},
                       {"mode": "single", "current": self.submission.pk, "filter": "all"}):
            with self.subTest(params=params):
                page = self.client.get(reverse("submissions:formatting"), params, follow=True)
                self.assertContains(page, "data-comparison-services ", count=1)
                html = page.content.decode()
                self.assertLess(html.index("data-comparison-services "), html.index('id="formatting-worklist"'))
        self.assertNotContains(self.client.get(self.url), "data-comparison-services ")
