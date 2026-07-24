from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from submissions.forms import EditorUploadForm
from submissions.models import FinalSubmission, InitialPaper
from submissions.services.paper_picker import (
    PAPER_PICKER_RESULT_LIMIT,
    search_master_papers,
)


class PaperPickerSearchTests(TestCase):
    def setUp(self):
        self.search_url = reverse("submissions:paper_picker_search")

    def test_empty_query_does_not_return_all_master_papers(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="First Paper",
            authors="Ada Lovelace",
        )

        response = self.client.get(self.search_url, {"context": "master", "q": ""})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_master_search_ranks_exact_id_first_and_caps_results(self):
        InitialPaper.objects.create(
            paper_id="P100",
            title="Exact Paper",
            authors="Ada Lovelace",
        )
        for index in range(PAPER_PICKER_RESULT_LIMIT + 5):
            InitialPaper.objects.create(
                paper_id=f"X{index:03d}",
                title=f"P100 related title {index}",
                authors="Grace Hopper",
            )

        results = search_master_papers("P100")

        self.assertEqual(results[0]["paper_id"], "P100")
        self.assertEqual(len(results), PAPER_PICKER_RESULT_LIMIT)

    def test_master_search_uses_title_and_authors_without_rendering_authors(self):
        paper = InitialPaper.objects.create(
            paper_id="R084",
            title="Reliable Publication Workflows",
            authors="Chih-Wei Hsu; Ada Lovelace",
        )

        title_response = self.client.get(
            self.search_url,
            {"context": "master", "q": "Publication Workflows"},
        )
        author_response = self.client.get(
            self.search_url,
            {"context": "master", "q": "Ada Lovelace"},
        )

        self.assertEqual(title_response.json()["results"][0]["pk"], paper.pk)
        self.assertEqual(author_response.json()["results"][0]["pk"], paper.pk)

    def test_selected_master_record_can_be_hydrated_by_pk_or_paper_id(self):
        paper = InitialPaper.objects.create(
            paper_id="R084",
            title="Reliable Publication Workflows",
        )

        by_pk = self.client.get(
            self.search_url,
            {"context": "master", "selected": paper.pk},
        )
        by_id = self.client.get(
            self.search_url,
            {
                "context": "master",
                "selected": paper.paper_id,
                "selected_field": "paper_id",
            },
        )

        self.assertEqual(by_pk.json()["results"][0]["paper_id"], "R084")
        self.assertEqual(by_id.json()["results"][0]["pk"], paper.pk)

    def test_process_search_returns_exact_focused_submission_url(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="Current Publication Paper",
        )
        submission = FinalSubmission.objects.create(
            final_submission_id="101",
            paper_id_filled="P001",
            final_submission_title="Current Publication Paper",
            active_version=True,
        )
        FinalSubmission.objects.create(
            final_submission_id="100",
            paper_id_filled="P001",
            final_submission_title="Inactive historical version",
            active_version=False,
        )
        FinalSubmission.objects.create(
            final_submission_id="102",
            paper_id_filled="P001",
            final_submission_title="Discarded version",
            active_version=True,
            discarded=True,
        )
        FinalSubmission.objects.create(
            final_submission_id="103",
            paper_id_filled="P001",
            final_submission_title="Not Publishing version",
            active_version=True,
            excluded_from_publication=True,
        )

        response = self.client.get(
            self.search_url,
            {"context": "process", "q": "P001"},
        )

        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["paper_id"], "P001")
        self.assertEqual(result["final_id"], "101")
        self.assertNotIn("title", result)
        self.assertEqual(
            result["url"],
            f"{reverse('submissions:process')}?submission={submission.pk}",
        )


class PaperPickerPageTests(TestCase):
    def test_picker_discards_unselected_options_between_remote_queries(self):
        asset = (
            Path(settings.BASE_DIR)
            / "submissions"
            / "static"
            / "submissions"
            / "paper_picker.js"
        ).read_text(encoding="utf-8")

        self.assertIn("function removeUnselectedOptions(picker)", asset)
        self.assertIn("onType: function ()", asset)
        self.assertGreaterEqual(asset.count("removeUnselectedOptions(this)"), 2)

    def test_editor_upload_uses_remote_picker_and_retains_model_validation(self):
        selected = InitialPaper.objects.create(
            paper_id="P001",
            title="Selected Master Title",
        )
        hidden = InitialPaper.objects.create(
            paper_id="P999",
            title="This title must not be rendered as an option",
        )

        response = self.client.get(
            reverse("submissions:editor_upload"),
            {"paper_id": selected.paper_id},
        )
        form = EditorUploadForm(data={"paper": hidden.pk + 10000})

        self.assertContains(response, 'data-cfm-paper-picker="true"')
        self.assertContains(response, "editor-upload-paper-summary")
        self.assertNotContains(response, hidden.title)
        self.assertFalse(form.is_valid())
        self.assertIn("paper", form.errors)
        self.assertNotIn("paper", form.cleaned_data)
        self.assertEqual(EditorUploadForm().fields["paper"].clean(selected.pk), selected)

    def test_verify_page_uses_remote_picker_without_master_option_list(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="Correct Master Title",
        )
        InitialPaper.objects.create(
            paper_id="P999",
            title="This title must not be repeated in every verification row",
        )
        FinalSubmission.objects.create(
            final_submission_id="101",
            paper_id_filled="UNKNOWN",
            final_submission_title="Different Final Title",
            active_version=True,
            verification_status="invalid_paper_id",
        )

        response = self.client.get(
            reverse("submissions:verify_paper_ids"),
            {"filter": "all"},
        )

        self.assertContains(response, 'data-picker-value-field="paper_id"')
        self.assertNotContains(
            response,
            "This title must not be repeated in every verification row",
        )

    def test_process_page_uses_find_paper_picker(self):
        response = self.client.get(reverse("submissions:process"))

        self.assertContains(response, "Find paper")
        self.assertContains(response, 'data-picker-context="process"')
        self.assertNotContains(response, "paper-preview-jump")
