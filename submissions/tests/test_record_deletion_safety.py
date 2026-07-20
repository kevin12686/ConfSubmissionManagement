from django.contrib import admin
from django.urls import reverse

from submissions.models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    InitialPaper,
    PaperAuthor,
)
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase


class RecordDeletionSafetyTests(EditorialAcceptanceTestCase):
    def test_django_admin_cannot_bypass_publication_workflows(self):
        for model in (
            AppSetting,
            AuthorLimitWaiver,
            FinalSubmission,
            InitialPaper,
            PaperAuthor,
        ):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_add_permission(None))
                self.assertFalse(model_admin.has_change_permission(None))
                self.assertFalse(model_admin.has_delete_permission(None))

    def test_paper_master_delete_succeeds_only_without_mapped_finals(self):
        paper = self.make_master_paper(
            "DELETE-EMPTY",
            "No mapped final",
            "Ada",
        )
        page = self.client.get(
            reverse(
                "submissions:initial_paper_delete",
                args=[paper.pk],
            )
        )

        response = self.client.post(
            reverse(
                "submissions:initial_paper_delete",
                args=[paper.pk],
            ),
            {"evidence_token": page.context["evidence_token"]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            InitialPaper.objects.filter(pk=paper.pk).exists()
        )

    def test_paper_master_delete_is_blocked_when_final_is_mapped(self):
        paper = self.make_master_paper(
            "DELETE-MAPPED",
            "Mapped final",
            "Ada",
        )
        submission = self.make_final_submission(
            final_submission_id="DELETE-MAPPED-FINAL",
            paper_id_filled=paper.paper_id,
            final_submission_title=paper.title,
            extracted_title=paper.title,
        )
        page = self.client.get(
            reverse(
                "submissions:initial_paper_delete",
                args=[paper.pk],
            )
        )

        self.assertFalse(page.context["can_delete"])
        self.assertNotContains(page, ">Delete</button>")
        response = self.client.post(
            reverse(
                "submissions:initial_paper_delete",
                args=[paper.pk],
            ),
            {"evidence_token": page.context["evidence_token"]},
            follow=True,
        )

        self.assertContains(
            response,
            "cannot be deleted while Final Submissions are mapped",
        )
        self.assertTrue(
            InitialPaper.objects.filter(pk=paper.pk).exists()
        )
        self.assertTrue(
            FinalSubmission.objects.filter(pk=submission.pk).exists()
        )

    def test_paper_master_delete_rejects_stale_empty_mapping_evidence(self):
        paper = self.make_master_paper(
            "DELETE-RACE",
            "Concurrent mapping",
            "Ada",
        )
        page = self.client.get(
            reverse(
                "submissions:initial_paper_delete",
                args=[paper.pk],
            )
        )
        submission = self.make_final_submission(
            final_submission_id="DELETE-RACE-FINAL",
            paper_id_filled=paper.paper_id,
            final_submission_title=paper.title,
            extracted_title=paper.title,
        )

        response = self.client.post(
            reverse(
                "submissions:initial_paper_delete",
                args=[paper.pk],
            ),
            {"evidence_token": page.context["evidence_token"]},
            follow=True,
        )

        self.assertContains(
            response,
            "record changed after this page was loaded",
        )
        self.assertTrue(
            InitialPaper.objects.filter(pk=paper.pk).exists()
        )
        self.assertTrue(
            FinalSubmission.objects.filter(pk=submission.pk).exists()
        )

    def test_final_submission_hard_delete_is_disabled(self):
        self.make_master_paper("KEEP-FINAL", "Keep Final", "Ada")
        submission = self.make_final_submission(
            final_submission_id="KEEP-FINAL-1",
            paper_id_filled="KEEP-FINAL",
            final_submission_title="Keep Final",
            extracted_title="Keep Final",
        )

        listing = self.client.get(
            reverse("submissions:final_submission_list")
        )
        page = self.client.get(
            reverse(
                "submissions:final_submission_delete",
                args=[submission.pk],
            )
        )
        response = self.client.post(
            reverse(
                "submissions:final_submission_delete",
                args=[submission.pk],
            ),
            follow=True,
        )

        self.assertNotContains(
            listing,
            reverse(
                "submissions:final_submission_delete",
                args=[submission.pk],
            ),
        )
        self.assertFalse(page.context["can_delete"])
        self.assertContains(page, "hard delete is disabled")
        self.assertContains(response, "hard delete is disabled")
        self.assertTrue(
            FinalSubmission.objects.filter(pk=submission.pk).exists()
        )
