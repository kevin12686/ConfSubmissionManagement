from unittest.mock import patch

from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from submissions.application.pagination import paginate_worklist
from submissions.application.selectors import (
    _sort_final_submission_rows,
    _sort_paper_master_rows,
)
from submissions.models import FinalSubmission, InitialPaper, PaperAuthor
from submissions.services.editor_uploads import editor_conflict_details
from submissions.services.final_submission_state import (
    bulk_sync_submission_state_records,
    bulk_update_submissions,
)


class WorklistPaginationTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.items = list(range(1, 251))

    def test_default_page_size_is_25(self):
        page = paginate_worklist(self.factory.get("/papers/"), self.items)

        self.assertEqual(list(page.items), list(range(1, 26)))
        self.assertEqual(page.total_count, 250)
        self.assertEqual(page.page_size, 25)
        self.assertEqual(
            [option["value"] for option in page.page_size_links],
            ["25", "50", "100", "200", "all"],
        )
        self.assertEqual(page.start_index, 1)
        self.assertEqual(page.end_index, 25)

    def test_all_returns_every_filtered_item(self):
        page = paginate_worklist(
            self.factory.get("/papers/?q=paper&page_size=all&page=3"),
            self.items,
        )

        self.assertEqual(page.items, self.items)
        self.assertTrue(page.is_all)
        self.assertEqual(page.start_index, 1)
        self.assertEqual(page.end_index, 250)
        self.assertIn("q=paper", page.page_size_links[0]["url"])

    def test_invalid_page_and_page_size_are_normalized(self):
        page = paginate_worklist(
            self.factory.get("/papers/?page=999&page_size=invalid"),
            self.items,
        )

        self.assertEqual(page.page_number, 10)
        self.assertEqual(page.page_size, 25)
        self.assertEqual(list(page.items), list(range(226, 251)))

    def test_page_size_links_reset_page_and_preserve_filters(self):
        page = paginate_worklist(
            self.factory.get("/papers/?filter=attention&page=3&page_size=50"),
            self.items,
        )

        all_link = next(
            link for link in page.page_size_links if link["value"] == "all"
        )
        self.assertIn("filter=attention", all_link["url"])
        self.assertIn("page=1", all_link["url"])
        self.assertIn("page_size=all", all_link["url"])


class WorklistPaginationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        InitialPaper.objects.bulk_create(
            [
                InitialPaper(
                    paper_id=f"P{index:04d}",
                    acceptance_status="Accept",
                    title=f"Paper {index:04d}",
                )
                for index in range(1, 206)
            ]
        )
        submissions = FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=str(index),
                    start2_paper_id_raw=f"P{index:04d}",
                    paper_id_filled=f"P{index:04d}",
                    final_submission_title=f"Paper {index:04d}",
                    final_submission_authors=f"Author {index:04d}",
                    extracted_title=f"Paper {index:04d}",
                    extracted_authors=f"Author {index:04d}",
                    active_version=True,
                    paper_id_verified=True,
                    verification_status="verified",
                    title_author_review_status="review_ok",
                    title_author_verified=True,
                    extracted_title_verified=True,
                    format_status="review_ok",
                    page_count=8,
                    processing_status="processed",
                    pdf_hash=f"{index:064x}",
                    similarity_score=1,
                    single_similarity_score=1,
                )
                for index in range(1, 206)
            ]
        )
        bulk_sync_submission_state_records(submissions)
        PaperAuthor.objects.bulk_create(
            [
                PaperAuthor(
                    final_submission=submission,
                    paper_id=submission.paper_id_filled,
                    author_name=f"Author {index:04d}",
                    normalized_author_name=f"author {index:04d}",
                    author_order=1,
                )
                for index, submission in enumerate(submissions, start=1)
            ]
        )

    def test_paper_master_default_and_all_have_the_same_ordered_scope(self):
        url = reverse("submissions:initial_paper_list")

        first_page = self.client.get(url)
        all_rows = self.client.get(url, {"page_size": "all"})

        self.assertEqual(len(first_page.context["papers"]), 25)
        self.assertEqual(len(all_rows.context["papers"]), 205)
        self.assertEqual(
            [paper.paper_id for paper in all_rows.context["papers"]],
            [f"P{index:04d}" for index in range(1, 206)],
        )
        self.assertContains(all_rows, "All")

    def test_natural_sort_queries_only_load_sort_keys_before_pagination(self):
        with CaptureQueriesContext(connection) as paper_queries:
            paper_ids = _sort_paper_master_rows(
                InitialPaper.objects.all(),
                "paper_id_asc",
            )
        with CaptureQueriesContext(connection) as final_queries:
            submission_ids = _sort_final_submission_rows(
                FinalSubmission.objects.all(),
                "final_id_asc",
            )

        self.assertEqual(len(paper_ids), 205)
        self.assertEqual(len(submission_ids), 205)
        self.assertEqual(len(paper_queries), 1)
        self.assertEqual(len(final_queries), 1)
        paper_sql = paper_queries[0]["sql"].lower()
        final_sql = final_queries[0]["sql"].lower()
        self.assertNotIn("authors", paper_sql)
        self.assertNotIn("notes", paper_sql)
        self.assertNotIn("extracted_authors", final_sql)
        self.assertNotIn("processing_message", final_sql)
        self.assertNotIn("plagiarism_report_path", final_sql)

    def test_second_page_starts_after_the_first_page_without_overlap(self):
        url = reverse("submissions:initial_paper_list")
        first_page = self.client.get(url)
        second_page = self.client.get(url, {"page": 2})

        first_ids = [paper.paper_id for paper in first_page.context["papers"]]
        second_ids = [paper.paper_id for paper in second_page.context["papers"]]
        self.assertEqual(second_ids[0], "P0026")
        self.assertFalse(set(first_ids) & set(second_ids))

    def test_publication_worklists_paginate_the_complete_ordered_scope(self):
        cases = [
            (
                "final submissions",
                reverse("submissions:final_submission_list"),
                {},
                "submissions",
                lambda item: item.paper_id_filled,
            ),
            (
                "organized list",
                reverse("submissions:organized_list"),
                {},
                "rows",
                lambda row: row["submission"].paper_id_filled,
            ),
            (
                "process PDFs",
                reverse("submissions:process"),
                {"filter": "all"},
                "processed_rows",
                lambda row: row["submission"].paper_id_filled,
            ),
            (
                "verify Paper IDs",
                reverse("submissions:verify_paper_ids"),
                {"filter": "all"},
                "rows",
                lambda row: row["submission"].paper_id_filled,
            ),
            (
                "title author",
                reverse("submissions:title_author_extraction"),
                {"filter": "all"},
                "rows",
                lambda row: row["submission"].paper_id_filled,
            ),
            (
                "formatting",
                reverse("submissions:formatting"),
                {"filter": "all"},
                "rows",
                lambda row: row["submission"].paper_id_filled,
            ),
        ]

        for label, url, params, context_key, identity in cases:
            with self.subTest(worklist=label):
                first = self.client.get(url, params)
                second = self.client.get(url, {**params, "page": 2})
                complete = self.client.get(url, {**params, "page_size": "all"})
                self.assertEqual(first.status_code, 200)
                self.assertEqual(second.status_code, 200)
                self.assertEqual(complete.status_code, 200)
                first_rows = list(first.context[context_key])
                second_rows = list(second.context[context_key])
                complete_rows = list(complete.context[context_key])
                self.assertEqual(first.context["pagination"].total_count, 205)
                self.assertEqual(len(first_rows), 25)
                self.assertEqual(len(second_rows), 25)
                self.assertEqual(len(complete_rows), 205)
                self.assertEqual(identity(first_rows[0]), "P0001")
                self.assertEqual(identity(second_rows[0]), "P0026")
                self.assertEqual(
                    [identity(row) for row in complete_rows],
                    [f"P{index:04d}" for index in range(1, 206)],
                )
                self.assertFalse(
                    {identity(row) for row in first_rows}
                    & {identity(row) for row in second_rows}
                )

    def test_error_author_and_exception_worklists_paginate_full_counts(self):
        error_response = self.client.get(
            reverse("submissions:error_report"),
            {"page": 2},
        )
        author_response = self.client.get(
            reverse("submissions:author_count"),
            {"page": 2},
        )
        submissions = list(FinalSubmission.objects.all())
        for submission in submissions:
            submission.page_count = 20
        bulk_update_submissions(submissions, ["page_count"])
        exception_response = self.client.get(
            reverse("submissions:exceptions_center"),
            {"page": 2},
        )

        self.assertGreater(error_response.context["pagination"].total_count, 205)
        self.assertEqual(len(error_response.context["rows"]), 25)
        self.assertEqual(author_response.context["pagination"].total_count, 205)
        self.assertEqual(len(author_response.context["rows"]), 25)
        self.assertEqual(exception_response.context["pagination"].total_count, 205)
        self.assertEqual(len(exception_response.context["rows"]), 25)

    def test_old_versions_paginate_without_affecting_current_candidates(self):
        old_submissions = FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=f"OLD{index:04d}",
                    start2_paper_id_raw=f"P{index:04d}",
                    paper_id_filled=f"P{index:04d}",
                    final_submission_title=f"Paper {index:04d}",
                    active_version=False,
                    duplicate_submission=True,
                )
                for index in range(1, 206)
            ]
        )
        bulk_sync_submission_state_records(old_submissions)

        response = self.client.get(
            reverse("submissions:old_versions"),
            {"page": 2},
        )

        self.assertEqual(response.context["pagination"].total_count, 205)
        self.assertEqual(len(response.context["rows"]), 25)
        self.assertTrue(
            all(not row["submission"].active_version for row in response.context["rows"])
        )

    def test_expensive_title_diffs_are_hydrated_only_for_displayed_page(self):
        with patch(
            "submissions.services.title_author_extraction.text_diff_html",
            return_value="diff",
        ) as title_diff:
            response = self.client.get(
                reverse("submissions:title_author_extraction"),
                {"filter": "all", "page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(title_diff.call_count, 50)

    def test_expensive_verification_diffs_are_hydrated_only_for_displayed_page(self):
        with (
            patch(
                "submissions.services.verification.title_diff_html",
                return_value="title diff",
            ) as title_diff,
            patch(
                "submissions.services.verification.text_diff_html",
                return_value="author diff",
            ) as author_diff,
        ):
            response = self.client.get(
                reverse("submissions:verify_paper_ids"),
                {"filter": "all", "page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(title_diff.call_count, 50)
        self.assertEqual(author_diff.call_count, 50)

    def test_organized_details_are_hydrated_only_for_displayed_page(self):
        with patch(
            "submissions.services.organized_list.text_diff_html",
            return_value="diff",
        ) as title_diff:
            response = self.client.get(
                reverse("submissions:organized_list"),
                {"page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(title_diff.call_count, 100)

    def test_process_thumbnails_are_hydrated_only_for_displayed_page(self):
        with patch(
            "submissions.services.pdf_processor.thumbnail_urls",
            return_value=[],
        ) as thumbnails:
            response = self.client.get(
                reverse("submissions:process"),
                {"filter": "all", "page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["processed_rows"]), 50)
        self.assertEqual(thumbnails.call_count, 50)

    def test_author_pdf_links_are_hydrated_only_for_displayed_page(self):
        with patch(
            "submissions.services.checks.publication_pdf_info",
            return_value={
                "url": "",
                "label": "No PDF",
                "source": "missing",
                "exists": False,
            },
        ) as publication_pdf:
            response = self.client.get(
                reverse("submissions:author_count"),
                {"page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(publication_pdf.call_count, 50)

    def test_exception_pdf_links_are_hydrated_only_for_displayed_page(self):
        submissions = list(FinalSubmission.objects.all())
        for submission in submissions:
            submission.page_count = 20
        bulk_update_submissions(submissions, ["page_count"])

        with patch(
            "submissions.services.exceptions.publication_pdf_info",
            return_value={
                "url": "",
                "label": "No PDF",
                "source": "missing",
                "exists": False,
            },
        ) as publication_pdf:
            response = self.client.get(
                reverse("submissions:exceptions_center"),
                {"page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(publication_pdf.call_count, 50)

    def test_old_version_pdf_links_are_hydrated_only_for_displayed_page(self):
        old_submissions = FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=f"OLD-PAGE-{index:04d}",
                    paper_id_filled=f"P{index:04d}",
                    active_version=False,
                    duplicate_submission=True,
                )
                for index in range(1, 206)
            ]
        )
        bulk_sync_submission_state_records(old_submissions)

        with patch(
            "submissions.services.version_history.publication_pdf_info",
            return_value={
                "url": "",
                "label": "No PDF",
                "source": "missing",
                "exists": False,
            },
        ) as publication_pdf:
            response = self.client.get(
                reverse("submissions:old_versions"),
                {"page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(publication_pdf.call_count, 50)

    def test_editor_conflict_snapshot_query_count_does_not_scale_per_paper(self):
        editor_submissions = FinalSubmission.objects.bulk_create(
            [
                FinalSubmission(
                    final_submission_id=f"EDITOR{index:04d}",
                    start2_paper_id_raw=f"P{index:04d}",
                    paper_id_filled=f"P{index:04d}",
                    final_submission_title=f"Paper {index:04d}",
                    submission_origin="editor_upload",
                    mapping_source="editor_upload",
                )
                for index in range(1, 206)
            ]
        )
        bulk_sync_submission_state_records(editor_submissions)

        with CaptureQueriesContext(connection) as captured:
            details = editor_conflict_details()

        self.assertEqual(len(details), 205)
        self.assertLessEqual(len(captured), 2)
        self.assertEqual(details[0]["paper_id"], "P0001")
        self.assertEqual(details[-1]["paper_id"], "P0205")
        self.assertEqual(len(details[0]["start2"]), 1)
        self.assertEqual(len(details[0]["editor"]), 1)
