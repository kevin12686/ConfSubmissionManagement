import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from submissions.controllers.settings import (
    _active_version_candidate_fingerprint,
)
from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.import_preview import (
    apply_import_preview,
    preview_final_import,
)
from submissions.services.pdf_processor import determine_active_versions


class ImportAndActiveRuleRegressionTests(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings_override = override_settings(
            BASE_DIR=self.root,
            MEDIA_ROOT=self.root / "media",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    @staticmethod
    def _csv(text):
        return SimpleUploadedFile(
            "final.csv",
            text.encode("utf-8-sig"),
            content_type="text/csv",
        )

    def _settings_form_data(self, settings_obj, active_version_rule):
        response = self.client.get(reverse("submissions:settings"))
        self.assertEqual(response.status_code, 200)
        return {
            "action": "save_settings",
            "evidence_token": response.context["settings_evidence_token"],
            "conference_name": settings_obj.conference_name,
            "page_minimum": settings_obj.page_minimum,
            "page_limit": settings_obj.page_limit,
            "author_paper_limit": settings_obj.author_paper_limit,
            "max_authors_per_paper": settings_obj.max_authors_per_paper,
            "title_words_for_filename": settings_obj.title_words_for_filename,
            "active_version_rule": active_version_rule,
            "time_zone": settings_obj.time_zone,
            "publication_pdf_debug_folder": (
                settings_obj.publication_pdf_debug_folder
            ),
            "reports_folder": settings_obj.reports_folder,
            "extraction_results_folder": (
                settings_obj.extraction_results_folder
            ),
            "plagiarism_reports_folder": (
                settings_obj.plagiarism_reports_folder
            ),
            "grobid_enabled": settings_obj.grobid_enabled,
            "grobid_api_url": settings_obj.grobid_api_url,
            "grobid_timeout_seconds": settings_obj.grobid_timeout_seconds,
            "plagiarism_percent_threshold": (
                settings_obj.plagiarism_percent_threshold
            ),
            "single_similarity_threshold": (
                settings_obj.single_similarity_threshold
            ),
        }

    def test_duplicate_start2_final_id_blocks_entire_preview_apply(self):
        InitialPaper.objects.create(
            paper_id="P001",
            acceptance_status="accepted",
            title="Canonical title",
            authors="Ada",
        )
        preview = preview_final_import(
            self._csv(
                "final_submission_id,author_entered_paper_id,"
                "final_submission_title,final_submission_authors,upload_date\n"
                "10,P001,First title,Ada,2026-07-18 09:00:00\n"
                "10,P001,Conflicting title,Ada,2026-07-19 09:00:00\n"
            )
        )

        self.assertEqual(len(preview["blocking_errors"]), 1)
        self.assertIn("Duplicate Final ID '10'", preview["blocking_errors"][0])
        with self.assertRaisesMessage(ValueError, "Preview has blocking errors"):
            apply_import_preview(preview["token"])
        self.assertFalse(FinalSubmission.objects.exists())

    def test_inactive_candidate_date_change_invalidates_rule_preview(self):
        InitialPaper.objects.create(
            paper_id="P001",
            acceptance_status="accepted",
            title="Versioned paper",
            authors="Ada",
        )
        first = FinalSubmission.objects.create(
            final_submission_id="1",
            paper_id_filled="P001",
            final_submission_title="Versioned paper",
            upload_date=timezone.now() - timezone.timedelta(days=2),
        )
        second = FinalSubmission.objects.create(
            final_submission_id="2",
            paper_id_filled="P001",
            final_submission_title="Versioned paper",
            upload_date=timezone.now() - timezone.timedelta(days=1),
        )
        determine_active_versions()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.active_version)
        self.assertTrue(second.active_version)

        settings_obj = AppSetting.load()
        response = self.client.post(
            reverse("submissions:settings"),
            self._settings_form_data(settings_obj, "upload_date"),
        )
        self.assertEqual(response.status_code, 302)
        preview = self.client.session["active_version_rule_preview"]
        original_fingerprint = preview["candidate_fingerprint"]

        first.upload_date = timezone.now()
        first.save(update_fields=["upload_date", "updated_at"])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.active_version)
        self.assertTrue(second.active_version)
        self.assertNotEqual(
            original_fingerprint,
            _active_version_candidate_fingerprint(),
        )

        response = self.client.post(
            reverse("submissions:settings"),
            {
                "action": "confirm_active_rule_change",
                "preview_token": preview["token"],
            },
            follow=True,
        )

        settings_obj.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(settings_obj.active_version_rule, "final_id")
        self.assertFalse(first.active_version)
        self.assertTrue(second.active_version)
        self.assertContains(
            response,
            "active-version candidate changed",
        )
        self.assertNotIn(
            "active_version_rule_preview",
            self.client.session,
        )

    def test_candidate_fingerprint_is_built_with_one_database_query(self):
        FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=str(index),
                    paper_id_filled=f"P{index:03d}",
                )
                for index in range(25)
            ]
        )

        with self.assertNumQueries(1):
            fingerprint = _active_version_candidate_fingerprint()

        self.assertEqual(len(fingerprint), 64)
