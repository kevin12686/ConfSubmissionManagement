import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from submissions.application.pagination import paginate_worklist
from submissions.application.selectors import (
    _sort_final_submission_rows,
    _sort_paper_master_rows,
)
from submissions.models import FinalSubmission, InitialPaper, PaperAuthor
from submissions.services.editor_uploads import editor_conflict_details
from submissions.services.checks import publication_duplicate_groups
from submissions.services.file_inspection import FileInspectionContext
from submissions.services.final_submission_state import (
    bulk_sync_submission_state_records,
    bulk_update_submissions,
)
from submissions.services.organized_list import organized_list_rows
from submissions.services.workflow_evidence import paper_master_review_digest


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

    def test_scroll_anchor_defaults_to_simple_htmx_target(self):
        page = paginate_worklist(
            self.factory.get("/papers/?filter=attention"),
            self.items,
            hx_target="#paper-worklist",
        )

        self.assertEqual(page.scroll_anchor, "paper-worklist")

    def test_explicit_scroll_anchor_supports_full_page_worklists(self):
        page = paginate_worklist(
            self.factory.get("/papers/"),
            self.items,
            scroll_anchor="paper-master-worklist",
        )

        self.assertEqual(page.scroll_anchor, "paper-master-worklist")


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
                self.assertContains(
                    first,
                    'aria-label="Top worklist pagination"',
                    count=1,
                )
                self.assertContains(
                    first,
                    'aria-label="Bottom worklist pagination"',
                    count=1,
                )
                self.assertContains(
                    first,
                    'data-cfm-pagination-position="top"',
                    count=1,
                )
                self.assertContains(
                    first,
                    'data-cfm-pagination-position="bottom"',
                    count=1,
                )
                html = first.content.decode()
                top_pagination = html.split(
                    'aria-label="Top worklist pagination"',
                    1,
                )[1].split("</nav>", 1)[0]
                bottom_pagination = html.split(
                    'aria-label="Bottom worklist pagination"',
                    1,
                )[1].split("</nav>", 1)[0]
                self.assertIn('hx-swap="outerHTML"', top_pagination)
                self.assertNotIn("show:top", top_pagination)
                self.assertNotRegex(top_pagination, r'href="[^"]*#')
                self.assertIn(
                    'hx-swap="outerHTML show:top"',
                    bottom_pagination,
                )

    def test_full_page_pagination_keeps_scroll_anchor_fallback(self):
        response = self.client.get(reverse("submissions:initial_paper_list"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        top_pagination = html.split(
            'aria-label="Top worklist pagination"',
            1,
        )[1].split("</nav>", 1)[0]
        bottom_pagination = html.split(
            'aria-label="Bottom worklist pagination"',
            1,
        )[1].split("</nav>", 1)[0]
        self.assertIn("#paper-master-worklist", top_pagination)
        self.assertIn("#paper-master-worklist", bottom_pagination)
        self.assertNotIn("hx-get=", top_pagination)
        self.assertNotIn("hx-get=", bottom_pagination)

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

    def test_paper_id_master_evidence_digest_is_built_once_per_page(self):
        with patch(
            "submissions.controllers.reviews.paper_master_review_digest",
            wraps=paper_master_review_digest,
        ) as master_digest:
            response = self.client.get(
                reverse("submissions:verify_paper_ids"),
                {"filter": "all", "page_size": 50},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(master_digest.call_count, 1)

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
        self.assertIn("COUNT", captured[0]["sql"].upper())
        self.assertEqual(details[0]["paper_id"], "P0001")
        self.assertEqual(details[-1]["paper_id"], "P0205")
        self.assertEqual(len(details[0]["start2"]), 1)
        self.assertEqual(len(details[0]["editor"]), 1)


class PublicationFileHydrationPaginationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._media_directory = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(
            MEDIA_ROOT=Path(cls._media_directory.name),
        )
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls._media_override.disable()
            cls._media_directory.cleanup()

    @classmethod
    def setUpTestData(cls):
        media_root = Path(cls._media_directory.name)
        pdf_root = media_root / "final_submissions"
        source_root = media_root / "source_submissions"
        thumbnail_root = media_root / "pdf_thumbnails"
        pdf_root.mkdir(parents=True)
        source_root.mkdir(parents=True)
        thumbnail_root.mkdir(parents=True)

        InitialPaper.objects.bulk_create(
            [
                InitialPaper(
                    paper_id=f"P{index:04d}",
                    acceptance_status="Accept",
                    title=f"Publication Paper {index:04d}",
                )
                for index in range(1, 61)
            ]
        )
        submissions = []
        for index in range(1, 61):
            shared_suffix = 59 if index in {59, 60} else index
            pdf_bytes = f"pdf-{shared_suffix}".encode("ascii")
            source_bytes = f"source-{shared_suffix}".encode("ascii")
            pdf_name = f"P{index:04d}.pdf"
            source_name = f"P{index:04d}.docx"
            (pdf_root / pdf_name).write_bytes(pdf_bytes)
            (source_root / source_name).write_bytes(source_bytes)
            paper_thumbnail_root = thumbnail_root / f"P{index:04d}"
            paper_thumbnail_root.mkdir()
            (paper_thumbnail_root / "page-1.png").write_bytes(b"thumbnail")
            submissions.append(
                FinalSubmission(
                    final_submission_id=str(index),
                    start2_paper_id_raw=f"P{index:04d}",
                    paper_id_filled=f"P{index:04d}",
                    final_submission_title=f"Publication Paper {index:04d}",
                    final_submission_authors=f"Author {index:04d}",
                    extracted_title=f"Publication Paper {index:04d}",
                    extracted_authors=f"Author {index:04d}",
                    pdf_file=f"final_submissions/{pdf_name}",
                    source_file=f"source_submissions/{source_name}",
                    active_version=True,
                    paper_id_verified=True,
                    verification_status="verified",
                    title_author_review_status="review_ok",
                    title_author_verified=True,
                    extracted_title_verified=True,
                    format_status="review_ok",
                    page_count=8,
                    processing_status="processed",
                    pdf_hash=hashlib.sha256(pdf_bytes).hexdigest(),
                    source_hash=hashlib.sha256(source_bytes).hexdigest(),
                    thumbnail_folder=str(paper_thumbnail_root),
                    thumbnail_status="processed",
                    similarity_score=1,
                    single_similarity_score=1,
                )
            )
        created = FinalSubmission.objects.bulk_create(submissions)
        bulk_sync_submission_state_records(created)

    def _count_hashes_for_get(self, url, params):
        original_sha256 = FileInspectionContext.sha256
        with patch.object(
            FileInspectionContext,
            "sha256",
            autospec=True,
            side_effect=lambda inspection, path, **kwargs: original_sha256(
                inspection,
                path,
                **kwargs,
            ),
        ) as sha256:
            response = self.client.get(url, params)
        return response, sha256.call_count

    def test_organized_default_page_hashes_only_displayed_pdf_and_source_files(self):
        response, hash_count = self._count_hashes_for_get(
            reverse("submissions:organized_list"),
            {"sort": "paper_id_asc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 25)
        self.assertEqual(hash_count, 50)
        self.assertEqual(
            response.context["summary"]["publication_duplicates"],
            2,
        )
        self.assertEqual(
            response.context["pagination"].total_count,
            60,
        )

    def test_organized_duplicate_filter_finds_records_outside_first_page(self):
        response, hash_count = self._count_hashes_for_get(
            reverse("submissions:organized_list"),
            {
                "filter": "publication_duplicates",
                "sort": "paper_id_asc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(hash_count, 4)
        self.assertEqual(
            [
                row["submission"].paper_id_filled
                for row in response.context["rows"]
            ],
            ["P0059", "P0060"],
        )
        self.assertTrue(
            all(
                {"Duplicate PDF", "Duplicate source"}.issubset(
                    row["duplicate_badges"]
                )
                for row in response.context["rows"]
            )
        )

    def test_organized_all_mode_hydrates_all_requested_records(self):
        response, hash_count = self._count_hashes_for_get(
            reverse("submissions:organized_list"),
            {
                "sort": "paper_id_asc",
                "page_size": "all",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 60)
        self.assertEqual(hash_count, 120)
        self.assertTrue(response.context["pagination"].is_all)

    def test_organized_service_full_hydration_contract_remains_available(self):
        original_sha256 = FileInspectionContext.sha256
        with patch.object(
            FileInspectionContext,
            "sha256",
            autospec=True,
            side_effect=lambda inspection, path, **kwargs: original_sha256(
                inspection,
                path,
                **kwargs,
            ),
        ) as sha256:
            rows, _summary, _settings, _current_filter, _current_sort = (
                organized_list_rows(current_sort="paper_id_asc")
            )

        self.assertEqual(len(rows), 60)
        self.assertEqual(sha256.call_count, 120)
        self.assertTrue(rows[0]["publication_pdf"]["url"])
        self.assertTrue(rows[0]["publication_source"]["url"])

    def test_compact_view_shows_multiple_active_conflict_without_selecting_files(self):
        conflicting = FinalSubmission.objects.create(
            final_submission_id="30B",
            start2_paper_id_raw="P0030",
            paper_id_filled="P0030",
            final_submission_title="Conflicting Publication Paper",
            active_version=True,
        )
        bulk_sync_submission_state_records([conflicting])

        response = self.client.get(
            reverse("submissions:organized_list"),
            {
                "view": "compact",
                "q": "P0030",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["missing_final"], 0)
        self.assertEqual(response.context["summary"]["version_conflicts"], 1)
        self.assertEqual(
            response.context["summary"]["multiple_active_conflicts"],
            [
                {
                    "paper_id": "P0030",
                    "final_ids": ["30", "30B"],
                }
            ],
        )
        self.assertContains(response, "Version conflicts require resolution")
        self.assertContains(response, "30, 30B")
        self.assertContains(response, "No file selected")
        self.assertNotContains(response, "P0030.pdf")

    def test_unbound_hash_is_not_silently_trusted_by_duplicate_precheck(self):
        submission = FinalSubmission.objects.get(paper_id_filled="P0060")
        submission.pdf_hash = ""
        submission.save(update_fields=["pdf_hash", "updated_at"])

        response, hash_count = self._count_hashes_for_get(
            reverse("submissions:organized_list"),
            {
                "filter": "pdf_issues",
                "sort": "paper_id_asc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(hash_count, 1)
        self.assertEqual(
            [row["submission"].paper_id_filled for row in response.context["rows"]],
            ["P0060"],
        )
        self.assertTrue(response.context["rows"][0]["needs_processing_after_formatting"])
        strict_groups = publication_duplicate_groups(strict_hash=True)
        self.assertTrue(
            any(
                group["kind"] == "pdf"
                and {
                    item.paper_id_filled
                    for item in group["submissions"]
                }
                == {"P0059", "P0060"}
                for group in strict_groups
            )
        )

    def test_process_default_page_hashes_only_displayed_pdfs(self):
        response, hash_count = self._count_hashes_for_get(
            reverse("submissions:process"),
            {"filter": "all"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["processed_rows"]), 25)
        self.assertEqual(hash_count, 25)
        self.assertEqual(response.context["pagination"].total_count, 60)
        self.assertEqual(
            next(
                option["count"]
                for option in response.context["filter_options"]
                if option["value"] == "processed"
            ),
            60,
        )

    def test_process_all_mode_hydrates_all_requested_records(self):
        response, hash_count = self._count_hashes_for_get(
            reverse("submissions:process"),
            {
                "filter": "all",
                "page_size": "all",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["processed_rows"]), 60)
        self.assertEqual(hash_count, 60)
        self.assertTrue(response.context["pagination"].is_all)

    def test_formatting_list_defers_preview_rendering_and_has_constant_queries(self):
        self.client.get(
            reverse("submissions:formatting"),
            {"filter": "all"},
        )
        with (
            patch(
                "submissions.services.formatting._render_first_page_upper_half"
            ) as render_preview,
            CaptureQueriesContext(connection) as page_queries,
        ):
            page = self.client.get(
                reverse("submissions:formatting"),
                {"filter": "all"},
            )
        with (
            patch(
                "submissions.services.formatting._render_first_page_upper_half"
            ) as render_all_previews,
            CaptureQueriesContext(connection) as all_queries,
        ):
            all_rows = self.client.get(
                reverse("submissions:formatting"),
                {"filter": "all", "page_size": "all"},
            )

        self.assertEqual(page.status_code, 200)
        self.assertEqual(all_rows.status_code, 200)
        self.assertEqual(render_preview.call_count, 0)
        self.assertEqual(render_all_previews.call_count, 0)
        self.assertEqual(len(page.context["rows"]), 25)
        self.assertEqual(len(all_rows.context["rows"]), 60)
        self.assertLessEqual(
            abs(len(page_queries) - len(all_queries)),
            1,
        )
        self.assertLessEqual(len(page_queries), 6)
        self.assertLessEqual(len(all_queries), 6)
        self.assertNotIn("formatting_review_snapshots", self.client.session)
        self.assertContains(
            page,
            'data-format-preview-src="/reviews/formatting/',
            count=25,
        )

    def test_formatting_preview_is_generated_only_when_requested(self):
        submission = FinalSubmission.objects.get(paper_id_filled="P0001")

        def write_preview(_source, target):
            Path(target).write_bytes(b"preview")

        with patch(
            "submissions.services.formatting._render_first_page_upper_half",
            side_effect=write_preview,
        ) as render_preview:
            response = self.client.get(
                reverse(
                    "submissions:formatting_preview",
                    args=[submission.pk],
                )
            )
            body = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"preview")
        self.assertEqual(render_preview.call_count, 1)

    def test_current_page_detects_pdf_changed_outside_workflow(self):
        submission = FinalSubmission.objects.get(paper_id_filled="P0001")
        pdf_path = Path(submission.pdf_file.path)
        original_bytes = pdf_path.read_bytes()
        pdf_path.write_bytes(b"externally changed")
        try:
            organized = self.client.get(
                reverse("submissions:organized_list"),
                {"sort": "paper_id_asc"},
            )
            process = self.client.get(
                reverse("submissions:process"),
                {"filter": "all"},
            )
        finally:
            pdf_path.write_bytes(original_bytes)

        self.assertEqual(organized.status_code, 200)
        self.assertEqual(process.status_code, 200)
        organized_row = next(
            row
            for row in organized.context["rows"]
            if row["submission"].paper_id_filled == "P0001"
        )
        process_row = next(
            row
            for row in process.context["processed_rows"]
            if row["submission"].paper_id_filled == "P0001"
        )
        self.assertTrue(organized_row["needs_processing_after_formatting"])
        self.assertEqual(organized_row["page_label"], "Page OK")
        self.assertTrue(process_row["needs_processing"])
        self.assertFalse(process_row["is_processed"])

    def test_organized_and_process_query_counts_are_not_per_submission(self):
        # Warm request/session state so the comparison measures worklist scaling.
        self.client.get(
            reverse("submissions:organized_list"),
            {"sort": "paper_id_asc"},
        )
        self.client.get(
            reverse("submissions:process"),
            {"filter": "all"},
        )
        with CaptureQueriesContext(connection) as organized_queries:
            organized = self.client.get(
                reverse("submissions:organized_list"),
                {"sort": "paper_id_asc"},
            )
        with CaptureQueriesContext(connection) as organized_all_queries:
            organized_all = self.client.get(
                reverse("submissions:organized_list"),
                {
                    "sort": "paper_id_asc",
                    "page_size": "all",
                },
            )
        with CaptureQueriesContext(connection) as process_queries:
            process = self.client.get(
                reverse("submissions:process"),
                {"filter": "all"},
            )
        with CaptureQueriesContext(connection) as process_all_queries:
            process_all = self.client.get(
                reverse("submissions:process"),
                {
                    "filter": "all",
                    "page_size": "all",
                },
            )

        self.assertEqual(organized.status_code, 200)
        self.assertEqual(organized_all.status_code, 200)
        self.assertEqual(process.status_code, 200)
        self.assertEqual(process_all.status_code, 200)
        self.assertEqual(len(organized_queries), len(organized_all_queries))
        self.assertEqual(len(process_queries), len(process_all_queries))
        self.assertLessEqual(len(organized_queries), 12)
        self.assertLessEqual(len(process_queries), 6)
