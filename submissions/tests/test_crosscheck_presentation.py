import hashlib
from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from submissions.application.selectors import crosscheck_batch_history
from submissions.models import CrossCheckServiceAttempt, CrossCheckServiceJob, CrossCheckServiceJobItem, CrossCheckServiceResult
from submissions.services.crosscheck_presentation import batch_counts, batch_summary
from submissions.services.crosscheck_service_history import batch_context, job_evidence, job_evidence_token
from submissions.services.publication_read import publication_database_signature
from submissions.tests import test_crosscheck_service as fixtures


class CrossCheckBatchPresentationTests(TestCase):
    setUp = fixtures.CrossCheckServiceTests.setUp
    preview = fixtures.CrossCheckServiceTests.preview
    archive = fixtures.CrossCheckServiceTests.archive
    ready = fixtures.CrossCheckServiceTests.ready
    second_submission = fixtures.CrossCheckServiceTests.second_submission

    def make_batch(self, statuses, *, state="partial", active=None, retained=None, latest=None):
        number = CrossCheckServiceJob.objects.count() + 1
        job = CrossCheckServiceJob.objects.create(
            token=f"QA-{number}", batch_day=date(2026, 1, 1), batch_number=number,
            conference_name="Example conference", service_url="http://qa-crosscheck:18765",
            folder="Example folder", scope="all", status=state,
        )
        run = CrossCheckServiceAttempt.objects.create(job=job, number=1, status=active or latest or "received")
        for n, status in enumerate(statuses):
            has_report = n in retained if retained is not None else status in {"downloaded", "applied", "stale"}
            digest = hashlib.sha256(f"report-{n}".encode()).hexdigest() if has_report else ""
            item = CrossCheckServiceJobItem.objects.create(
                job=job, paper_id=f"P{n:03d}", final_submission_id=str(n),
                pdf_sha256="a" * 64, source="corrected", status=status, report_sha256=digest,
            )
            if has_report:
                CrossCheckServiceResult.objects.create(attempt=run, item=item, status=status, report_sha256=digest)
        return job

    def summaries(self, job):
        detail = batch_context(job)
        row = crosscheck_batch_history().get(pk=job.pk)
        items = row.summary_items
        runs = list(row.attempts.all())
        active = next((run for run in runs if run.status in {"running", "pending_download", "submitting", "uncertain"}), None)
        summary = batch_summary(row, batch_counts(items, lambda item: item.has_report_result),
                                retained=sum(item.has_report_result for item in items),
                                active=active, latest=runs[-1] if runs else None)
        self.assertEqual(summary, detail["batch_summary"])
        return summary, detail

    def test_list_and_detail_share_the_complete_status_matrix(self):
        cases = [
            (["prepared"] * 5, {"state": "preview"}, "preview", "neutral"),
            (["prepared"] * 5, {"state": "cancelled"}, "cancelled", "neutral"),
            ([], {"state": "partial"}, "empty", "neutral"),
            (["applied"] * 5, {}, "completed", "success"),
            (["downloaded"] * 5, {}, "awaiting_apply", "info"),
            (["applied"] * 3 + ["downloaded"] * 2, {}, "awaiting_apply", "info"),
            (["downloaded"] * 3 + ["wait_timeout"] * 2, {}, "partial", "warning"),
            (["applied"] * 3 + ["wait_timeout"] * 2, {}, "partial", "warning"),
            (["stale"] * 5, {}, "partial", "warning"),
            (["downloaded"] * 5, {"retained": []}, "attention", "warning"),
            (["applied"] * 5, {"retained": []}, "partial", "warning"),
            (["wait_timeout"] * 5, {}, "missing", "warning"),
            (["download_failed"] * 5, {}, "missing", "warning"),
            (["report_error"] * 5, {}, "attention", "warning"),
            (["unconfirmed"] * 5, {"state": "failed", "latest": "failed"}, "failed", "danger"),
            (["prepared"] * 5, {"latest": "rejected"}, "failed", "danger"),
            (["prepared"] * 5, {"active": "running"}, "running", "info"),
            (["applied"] * 3 + ["prepared"] * 2, {"active": "pending_download"}, "running", "info"),
            (["prepared"] * 5, {"active": "uncertain"}, "unconfirmed", "warning"),
            (["prepared"] * 5, {"active": "submitting"}, "unconfirmed", "warning"),
        ]
        for statuses, options, key, level in cases:
            with self.subTest(key=key, statuses=statuses, options=options):
                summary, _ = self.summaries(self.make_batch(statuses, **options))
                self.assertEqual((summary["key"], summary["level"]), (key, level))
                self.assertEqual(summary["retained"] + summary["missing"], len(statuses))

    def test_partial_results_keep_missing_and_ready_counts_visible_in_both_pages(self):
        job = self.make_batch(["applied", "downloaded", "downloaded", "wait_timeout", "report_error"])
        for url in (reverse("submissions:integration") + "?tab=automatic",
                    reverse("submissions:crosscheck_service_job", args=[job.pk])):
            response = self.client.get(url)
            self.assertContains(response, 'data-batch-state="partial"')
            self.assertContains(response, "3 / 5")
            self.assertContains(response, "1 / 5")
            self.assertContains(response, "2 ready to apply")
            self.assertContains(response, "2 reports missing")
            self.assertContains(response, "1 need attention")
            self.assertNotContains(response, "All results applied")

    def test_retained_history_is_counted_once_and_only_for_matching_item_digest(self):
        job = self.make_batch(["downloaded", "downloaded"])
        item = job.items.first()
        run = CrossCheckServiceAttempt.objects.create(job=job, number=2, status="received", kind="retry_download")
        CrossCheckServiceResult.objects.create(attempt=run, item=item, status="retained", report_sha256=item.report_sha256)
        summary, _ = self.summaries(job)
        self.assertEqual(summary["retained"], 2)
        item.report_sha256 = "c" * 64
        item.save()
        summary, detail = self.summaries(job)
        self.assertEqual(summary["retained"], 1)
        self.assertEqual(detail["counts"], {"total": 2, "applied": 0, "ready": 1, "waiting": 0, "attention": 1})

    def test_retry_keeps_applied_results_visible_without_claiming_completion(self):
        job = self.make_batch(["applied", "wait_timeout"], active="running")
        job.attempts.update(kind="retry_download")
        summary, detail = self.summaries(job)
        self.assertEqual(summary["description"], "Downloading remaining reports.")
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["missing"], 1)
        self.assertFalse(detail["can_apply"])
        self.assertTrue(detail["poll_enabled"])

    def test_history_loading_is_bounded_without_per_item_result_queries(self):
        for _ in range(12):
            self.make_batch(["downloaded", "wait_timeout", "stale"])
        with self.assertNumQueries(3):
            rows = list(crosscheck_batch_history()[:10])
            for row in rows:
                self.assertEqual(len(row.summary_items), 3)
                self.assertEqual(sum(item.has_report_result for item in row.summary_items), 2)
                self.assertEqual(len(list(row.attempts.all())), 1)

    def test_page_counts_are_batch_scoped_and_not_limited_to_current_page(self):
        for _ in range(26):
            self.make_batch(["downloaded", "wait_timeout"])
        response = self.client.get(reverse("submissions:integration"), {"tab": "automatic", "page": 2, "page_size": 25})
        self.assertEqual(response.context["job_page"].total_count, 26)
        self.assertEqual(len(response.context["service_jobs"]), 1)
        self.assertEqual(response.context["service_jobs"][0].summary["total"], 2)

    def test_unknown_status_is_attention_not_success(self):
        summary, detail = self.summaries(self.make_batch(["future_status"]))
        self.assertEqual(summary["key"], "attention")
        self.assertFalse(detail["can_apply"])

    def test_missing_retained_evidence_never_labels_a_paper_ready_to_apply(self):
        job = self.make_batch(["downloaded"], retained=[])
        _, detail = self.summaries(job)
        self.assertFalse(detail["can_apply"])
        self.assertEqual(detail["items"][0].status_label, "Result evidence missing")
        response = self.client.get(reverse("submissions:crosscheck_service_job", args=[job.pk]))
        self.assertContains(response, "Result evidence missing")
        self.assertNotContains(response, "Ready to apply")
        self.assertNotContains(response, "Confirm and apply available results")

    def test_waiting_is_not_completion_and_empty_is_not_zero_of_zero_success(self):
        for statuses in ([], ["wait_timeout"], ["downloaded"]):
            summary, _ = self.summaries(self.make_batch(statuses))
            self.assertNotEqual(summary["level"], "success")

    def test_reading_summaries_does_not_contact_service_or_change_version_evidence(self):
        self.second_submission()
        job = self.preview()
        self.ready(job, outcomes={"P002": {"status": "wait_timeout"}})
        job.refresh_from_db()
        signature, evidence, token = publication_database_signature(), job_evidence(job), job_evidence_token(job)
        tables = (CrossCheckServiceJob, CrossCheckServiceJobItem, CrossCheckServiceAttempt, CrossCheckServiceResult)
        before = [list(model.objects.order_by("pk").values()) for model in tables]
        files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}
        with patch("submissions.services.crosscheck_service.CrossCheckClient") as network:
            for url in (reverse("submissions:integration") + "?tab=automatic",
                        reverse("submissions:crosscheck_service_job", args=[job.pk])):
                self.assertEqual(self.client.get(url).status_code, 200)
            network.assert_not_called()
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(job_evidence(job), evidence)
        self.assertEqual(job_evidence_token(job), token)
        self.assertEqual([list(model.objects.order_by("pk").values()) for model in tables], before)
        self.assertEqual({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}, files)
