"""Comparison is advisory: publication inputs and saved review evidence stay intact."""

import csv
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.automatic_pdf_comparison import (
    get_upload_comparison,
    remember_upload_comparison,
)
from submissions.services.checks import publication_readiness_rows
from submissions.services.crosscheck import prepare_crosscheck_upload
from submissions.services.file_manager import publication_pdf_info, publication_source_info
from submissions.services.formatting import (
    create_formatting_review_snapshot,
    load_formatting_review_snapshot,
    save_formatting_review_result,
)
from submissions.services.formatting_history import capture_existing_formatting
from submissions.services.pdf_comparison import (
    PDFComparisonError,
    compare_pdf_bytes,
    comparison_page_png,
    comparison_pdf_bytes,
    comparison_root,
    create_pdf_comparison,
    load_pdf_comparison,
)
from submissions.services.reports import PublicationPackageBlocked, export_publication_package
from submissions.tests.factories import create_final_submission, create_master_paper
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase
from submissions.tests.test_pdf_comparison import make_pdf
from submissions.tests.ocr_fixtures import synthetic_ocr_worker


class PDFComparisonSafetyRegressionTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("submissions.services.pdf_comparison_execution.subprocess.run", side_effect=synthetic_ocr_worker))
        self.enterContext(override_settings(BASE_DIR=self.root.resolve()))
        self.master = create_master_paper()
        self.original = make_pdf(["Original result: 12 participants."] + [""] * 7)
        self.corrected = make_pdf(["Corrected result: 21 participants."] + [""] * 7)
        self.original_source = b"Original editable manuscript"
        self.corrected_source = b"Corrected editable manuscript"
        self.submission = create_final_submission(
            self.root,
            pdf_bytes=self.original,
            source_bytes=self.original_source,
            submission_origin="editor_upload",
        )
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(self.corrected))
        self.submission.formatted_source_file.save("corrected.docx", ContentFile(self.corrected_source))
        self.mark_submission_publication_ready(self.submission)
        self.revision = capture_existing_formatting(self.submission.pk)
        self.historical = create_final_submission(
            self.root,
            final_submission_id="99",
            active_version=False,
            discarded=True,
            pdf_bytes=make_pdf(["Superseded Start2 submission"]),
        )
        self.historical.formatted_pdf_file.save(
            "historical.pdf", ContentFile(make_pdf(["Historical corrected manuscript"]))
        )
        # Deliberately different, readable decoys must never become publication inputs.
        self.make_file("publication_debug/P001-Ready Paper.pdf", make_pdf(["Debug copy"]))
        Path(self.submission.current_file_path).write_bytes(make_pdf(["Legacy PDF copy"]))
        Path(self.submission.source_current_file_path).write_bytes(b"Legacy source copy")
        self.session = {}

    def _state(self):
        records = {
            model._meta.label: list(model.objects.order_by("pk").values())
            for model in apps.get_app_config("submissions").get_models()
        }
        protected = (self.media_root, *(self.root / name for name in (
            "files", "thumbnails", "publication_debug", "active", "old",
        )))
        files = {
            str(path.relative_to(self.root)): path.read_bytes()
            for directory in protected
            for path in directory.rglob("*") if path.is_file()
        }
        return records, files, publication_readiness_rows(strict_hash=True)

    def _publication_contents(self):
        with zipfile.ZipFile(export_publication_package()) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            manifests = [name for name in names if name.startswith("publication_manifest_")]
            self.assertEqual(len(manifests), 1)
            files = {name: archive.read(name) for name in names if name != manifests[0]}
            rows = list(csv.DictReader(io.StringIO(archive.read(manifests[0]).decode("utf-8-sig"))))
        return files, rows

    def _crosscheck_contents(self, token):
        result = prepare_crosscheck_upload(token)
        self.assertEqual(result["exported_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertIsNone(archive.testzip())
            manifest_name = Path(result["manifest_path"]).name
            self.assertCountEqual(archive.namelist(), [f"P001_{token}.pdf", manifest_name])
            manifest = archive.read(manifest_name)
            self.assertEqual(manifest, Path(result["manifest_path"]).read_bytes())
            rows = list(csv.DictReader(io.StringIO(manifest.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["exported_filename"], f"P001_{token}.pdf")
            pdf = archive.read(row["exported_filename"])
            self.assertEqual(row["publication_pdf_sha256"], hashlib.sha256(pdf).hexdigest())
            self.assertEqual(row["final_submission_id"], self.submission.final_submission_id)
            self.assertEqual(row["source_publication_pdf"], str(publication_pdf_info(self.submission)["path"]))
        # Each CrossCheck export must use a new token. Only that filename may differ.
        return pdf, {key: value for key, value in row.items() if key != "exported_filename"}

    def _save(self, *, pdf=None, source=None):
        self.submission.refresh_from_db()
        token = create_formatting_review_snapshot({}, self.submission)
        evidence = load_formatting_review_snapshot({}, token, submission_id=self.submission.pk)
        result = save_formatting_review_result(self.submission.pk, {
            "format_status": "pending", "format_notes": "Saved editorial note",
            "corrected_pdf": SimpleUploadedFile("upload.pdf", pdf) if pdf is not None else None,
            "corrected_source": SimpleUploadedFile("upload.docx", source) if source is not None else None,
        }, evidence)
        self.submission = result.submission
        return result, remember_upload_comparison(self.session, result)

    def _comparison_tokens(self):
        root = comparison_root()
        return {path.name for path in root.iterdir() if path.is_dir() and path.name != "requests"} if root.exists() else set()

    def test_manual_comparison_and_browsing_preserve_all_selection_combinations(self):
        corrected_pdf_name = self.submission.formatted_pdf_file.name
        corrected_source_name = self.submission.formatted_source_file.name
        for use_pdf, use_source in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(corrected_pdf=use_pdf, corrected_source=use_source):
                self.submission.formatted_pdf_file = corrected_pdf_name if use_pdf else ""
                self.submission.formatted_source_file = corrected_source_name if use_source else ""
                self.mark_submission_publication_ready(self.submission)
                self.assertEqual(publication_readiness_rows(strict_hash=True), [])
                expected_pdf = self.corrected if use_pdf else self.original
                expected_source = self.corrected_source if use_source else self.original_source
                package_before = self._publication_contents()
                crosscheck_before = self._crosscheck_contents(f"BEFORE{int(use_pdf)}{int(use_source)}")
                state = self._state()

                payload = create_pdf_comparison(self.submission.pk)
                self.assertEqual(payload["identity"]["submission_id"], self.submission.pk)
                self.assertEqual(payload["identity"]["origin"], "editor_upload")
                self.assertEqual(payload["target"]["label"], "Corrected" if use_pdf else "Original")
                for side, expected in (("before", self.original), ("after", expected_pdf)):
                    self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], side), expected)
                    response = self.client.get(reverse(
                        "submissions:formatting_comparison_pdf", args=[self.submission.pk, payload["token"], side],
                    ))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(b"".join(response.streaming_content), expected)
                    response.close()
                    self.assertTrue(comparison_page_png(self.submission.pk, payload["token"], side, 1).startswith(b"\x89PNG"))

                package_after = self._publication_contents()
                self.assertEqual(package_before, package_after)
                self.assertEqual(package_after[0], {
                    "PDF/P001-Ready Paper.pdf": expected_pdf,
                    "Source/P001-Ready Paper.docx": expected_source,
                })
                self.assertEqual([row["ID"] for row in package_after[1]], ["P001"])
                self.assertEqual(crosscheck_before, self._crosscheck_contents(f"AFTER{int(use_pdf)}{int(use_source)}"))
                self.assertEqual(crosscheck_before[0], expected_pdf)
                self.assertEqual(self._state(), state)

    def test_revision_baseline_does_not_select_revision_pdf_or_source_for_export(self):
        newer = make_pdf(["Latest publication findings"] + [""] * 7)
        self.submission.formatted_pdf_file.save("latest.pdf", ContentFile(newer))
        self.submission.formatted_source_file.save("latest.docx", ContentFile(b"Latest source"))
        self.mark_submission_publication_ready(self.submission)
        package_before = self._publication_contents()
        crosscheck_before = self._crosscheck_contents("REVISIONBEFORE")
        state = self._state()
        payload = create_pdf_comparison(self.submission.pk, f"revision-{self.revision.pk}")
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "before"), self.corrected)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "after"), newer)
        self.assertEqual(self._publication_contents(), package_before)
        self.assertEqual(self._crosscheck_contents("REVISIONAFTER"), crosscheck_before)
        self.assertEqual(self._state(), state)

    def test_missing_selected_corrected_pdf_blocks_comparison_and_both_exports(self):
        payload = create_pdf_comparison(self.submission.pk)
        Path(self.submission.formatted_pdf_file.path).unlink()
        self.assertTrue(Path(self.submission.pdf_file.path).exists())
        self.assertTrue(Path(self.revision.pdf_file.path).exists())
        self.assertEqual(publication_pdf_info(self.submission)["source"], "corrected_missing")
        state = self._state()
        tokens = self._comparison_tokens()
        for phase in ("BEFORE", "AFTER"):
            with self.assertRaises(PublicationPackageBlocked):
                export_publication_package()
            with self.assertRaisesRegex(ValueError, "No unambiguous publication PDFs"):
                prepare_crosscheck_upload(f"MISSING{phase}")
            if phase == "BEFORE":
                with self.assertRaises(PDFComparisonError):
                    create_pdf_comparison(self.submission.pk)
                with self.assertRaises(PDFComparisonError):
                    comparison_pdf_bytes(self.submission.pk, payload["token"], "after")
        self.assertEqual(self._comparison_tokens(), tokens)
        self.assertEqual(self._state(), state)
        self.assertFalse(list((self.root / "reports").glob("*.zip")))
        self.assertFalse(list(self.crosscheck_root.rglob("*.zip")))

    def test_missing_corrected_source_stays_blocked_but_pdf_only_crosscheck_is_unchanged(self):
        Path(self.submission.formatted_source_file.path).unlink()
        self.assertTrue(Path(self.submission.source_file.path).exists())
        self.assertTrue(Path(self.revision.source_file.path).exists())
        self.assertEqual(publication_source_info(self.submission)["source"], "corrected_missing")
        state = self._state()
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package()
        crosscheck_before = self._crosscheck_contents("MISSINGSOURCEBEFORE")
        create_pdf_comparison(self.submission.pk)
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package()
        self.assertEqual(self._crosscheck_contents("MISSINGSOURCEAFTER"), crosscheck_before)
        self.assertEqual(crosscheck_before[0], self.corrected)
        self.assertEqual(self._state(), state)

    def test_comparison_does_not_rebind_changed_source_or_clear_review_blockers(self):
        Path(self.submission.formatted_source_file.path).write_bytes(b"Unreviewed source edit")
        self.submission.title_author_review_status = "pending"
        self.submission.title_author_verified = False
        self.submission.save()
        state = self._state()
        self.assertTrue(state[2])
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package()
        create_pdf_comparison(self.submission.pk)
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package()
        self.assertEqual(self._state(), state)

    def test_same_bytes_do_not_override_final_paper_origin_or_file_identity(self):
        payload = create_pdf_comparison(self.submission.pk)
        create_master_paper("P002")
        alternate_original = self.make_file("media/final_submissions/alternate.pdf", self.original)
        alternate_corrected = self.make_file("media/formatted/alternate.pdf", self.corrected)
        mutations = {
            "final_submission_id": "101",
            "paper_id_filled": "P002",
            "submission_origin": "start2",
            "pdf_file": str(alternate_original.relative_to(self.media_root)),
            "formatted_pdf_file": str(alternate_corrected.relative_to(self.media_root)),
        }
        query = FinalSubmission.objects.filter(pk=self.submission.pk)
        original = query.values().get()
        for field, value in mutations.items():
            with self.subTest(field=field):
                query.update(**{field: value})
                state = self._state()
                for read in (
                    lambda: load_pdf_comparison(self.submission.pk, payload["token"]),
                    lambda: comparison_pdf_bytes(self.submission.pk, payload["token"], "after"),
                    lambda: comparison_page_png(self.submission.pk, payload["token"], "after", 1),
                ):
                    with self.assertRaises(PDFComparisonError):
                        read()
                self.assertEqual(self._state(), state)
                query.update(**{field: original[field]})

    def test_multiple_active_versions_fail_closed_without_running_worker(self):
        create_final_submission(self.root, final_submission_id="101", pdf_bytes=self.corrected)
        state = self._state()
        with patch("submissions.services.pdf_comparison.compare_pdf_bytes") as worker:
            with self.assertRaisesRegex(PDFComparisonError, "Multiple active"):
                create_pdf_comparison(self.submission.pk)
            worker.assert_not_called()
        for force in (False, True):
            with self.assertRaises(PublicationPackageBlocked):
                export_publication_package(force=force)
        with self.assertRaises(ValueError):
            prepare_crosscheck_upload("AMBIGUOUS")
        self.assertEqual(self._state(), state)

    def test_scope_change_during_comparison_is_preserved_and_no_result_is_retained(self):
        mutations = (
            (FinalSubmission, self.submission.pk, {"discarded": True}),
            (FinalSubmission, self.submission.pk, {"active_version": False}),
            (InitialPaper, self.master.pk, {"publication_decision_status": "decision_required"}),
        )
        for model, pk, change in mutations:
            with self.subTest(change=change):
                query = model.objects.filter(pk=pk)
                original = query.values(*change).get()
                expected = []

                def compare_then_change(*args, **kwargs):
                    result = compare_pdf_bytes(*args, **kwargs)
                    query.update(**change)
                    expected.append(self._state())
                    return result

                tokens = self._comparison_tokens()
                with patch("submissions.services.pdf_comparison.compare_pdf_bytes", side_effect=compare_then_change):
                    with self.assertRaises(PDFComparisonError):
                        create_pdf_comparison(self.submission.pk)
                self.assertEqual(len(expected), 1)
                self.assertEqual(self._state(), expected[0])
                self.assertEqual(self._comparison_tokens(), tokens)
                query.update(**original)

    def test_concurrent_pdf_upload_including_identical_bytes_never_reuses_old_receipt(self):
        for same_bytes in (True, False):
            with self.subTest(same_bytes=same_bytes):
                _, entry = self._save(pdf=self.corrected)
                newer = self.corrected if same_bytes else make_pdf(["Newer committed manuscript"] + [""] * 7)
                expected = []
                receipts = []
                tokens = self._comparison_tokens()

                def compare_then_upload(*args, **kwargs):
                    result = compare_pdf_bytes(*args, **kwargs)
                    receipts.append(self._save(pdf=newer)[1])
                    expected.append(self._state())
                    return result

                with patch("submissions.services.pdf_comparison.compare_pdf_bytes", side_effect=compare_then_upload):
                    with self.assertRaises(PDFComparisonError):
                        get_upload_comparison(self.submission.pk, entry["token"], build=True)
                self.assertEqual(len(expected), 1)
                self.assertEqual(self._state(), expected[0])
                self.assertEqual(self._comparison_tokens(), tokens)
                with self.assertRaises(PDFComparisonError):
                    get_upload_comparison(self.submission.pk, entry["token"])
                current = get_upload_comparison(self.submission.pk, receipts[0]["token"], build=True)
                self.assertEqual(comparison_pdf_bytes(self.submission.pk, current["token"], "before"), self.original)
                self.assertEqual(comparison_pdf_bytes(self.submission.pk, current["token"], "after"), newer)
                self.assertEqual(self._state(), expected[0])

    def test_concurrent_source_only_revision_keeps_pdf_binding_and_its_review_resets(self):
        _, entry = self._save(pdf=self.corrected)
        expected = []

        def compare_then_upload_source(*args, **kwargs):
            result = compare_pdf_bytes(*args, **kwargs)
            _, source_receipt = self._save(source=b"New source-only revision")
            self.assertIsNone(source_receipt)
            expected.append(self._state())
            return result

        with patch("submissions.services.pdf_comparison.compare_pdf_bytes", side_effect=compare_then_upload_source):
            payload = get_upload_comparison(self.submission.pk, entry["token"], build=True)
        self.assertEqual(len(expected), 1)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "after"), self.corrected)
        self.assertEqual(self._state(), expected[0])
        with self.assertRaises(PublicationPackageBlocked):
            export_publication_package()
        self.assertEqual(self._crosscheck_contents("SOURCEONLY")[0], self.corrected)
        self.assertEqual(self._state(), expected[0])

    def test_worker_failures_do_not_modify_uploads_export_bytes_or_existing_comparisons(self):
        _, entry = self._save(pdf=self.corrected)
        self.mark_submission_publication_ready(self.submission)
        existing = create_pdf_comparison(self.submission.pk)
        package_before = self._publication_contents()
        crosscheck_before = self._crosscheck_contents("WORKERBEFORE")
        state = self._state()
        tokens = self._comparison_tokens()
        endpoint = reverse("submissions:formatting_upload_comparison", args=[self.submission.pk])
        failures = (
            {"side_effect": subprocess.TimeoutExpired("evidence-worker", 30)},
            {"side_effect": subprocess.CalledProcessError(1, "evidence-worker", stderr=b"failed")},
            {"side_effect": OSError("worker unavailable")},
            {"return_value": subprocess.CompletedProcess("evidence-worker", 0, stdout=b"not JSON")},
            {"return_value": subprocess.CompletedProcess("evidence-worker", 0, stdout=b"[{}, {}]")},
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with patch("submissions.services.pdf_comparison_execution.subprocess.run", **failure) as worker:
                    response = self.client.post(endpoint, {"token": entry["token"], "retry": "1"})
                    self.assertTrue(worker.called)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["state"], "failed")
                self.assertTrue(response.json()["retryable"])
                self.assertNotIn("url", response.json())
                self.assertEqual(self._comparison_tokens(), tokens)
                self.assertEqual(comparison_pdf_bytes(self.submission.pk, existing["token"], "after"), self.corrected)
                self.assertEqual(self._state(), state)
        self.assertEqual(self._publication_contents(), package_before)
        self.assertEqual(self._crosscheck_contents("WORKERAFTER"), crosscheck_before)
        events = [json.loads(line) for line in (self.audit_root / "audit.log").read_text().splitlines()]
        failures_logged = [event for event in events if event["action"] == "formatting_pdf_compare" and event["status"] == "failed"]
        self.assertEqual(len(failures_logged), len(failures))
        response = self.client.post(endpoint, {"token": entry["token"], "retry": "1"})
        self.assertEqual(response.json()["state"], "complete")
        self.assertEqual(self._state(), state)

    def test_corrupt_comparison_snapshot_never_becomes_publication_input(self):
        payload = create_pdf_comparison(self.submission.pk)
        package_before = self._publication_contents()
        crosscheck_before = self._crosscheck_contents("SNAPSHOTBEFORE")
        state = self._state()
        (comparison_root() / payload["token"] / "after.pdf").write_bytes(make_pdf(["Wrong snapshot bytes"]))
        with self.assertRaises(PDFComparisonError):
            comparison_pdf_bytes(self.submission.pk, payload["token"], "after")
        self.assertEqual(self._publication_contents(), package_before)
        self.assertEqual(self._crosscheck_contents("SNAPSHOTAFTER"), crosscheck_before)
        self.assertEqual(self._state(), state)

    def test_warm_cpu_cache_preserves_exports_and_rechecks_live_binding(self):
        from submissions.services import pdf_comparison as service
        from submissions.services.rapidocr_comparison import runtime
        from submissions.services.rapidocr_comparison import cache as evidence_cache
        from submissions.tests.test_pdf_comparison_cache import document, in_process_worker
        package = self._publication_contents()
        crosscheck = self._crosscheck_contents("CACHEBEFORE")
        state = self._state()
        with (
            patch("submissions.services.pdf_comparison_execution.subprocess.run", side_effect=in_process_worker),
            patch.object(runtime, "build_recognizer", return_value=object()),
            patch.object(runtime, "extract_document", side_effect=lambda data, engine: document(data)) as extract,
            patch.object(evidence_cache, "runtime_fingerprint", return_value="a" * 64),
        ):
            cold = create_pdf_comparison(self.submission.pk)
            warm = create_pdf_comparison(self.submission.pk)
            self.assertEqual(cold["result"], warm["result"])
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(self._state(), state)
            self.assertEqual(self._publication_contents(), package)
            self.assertEqual(self._crosscheck_contents("CACHEAFTER"), crosscheck)
            original_compare = service.compare_pdf_bytes
            def concurrent_change(*args, **kwargs):
                result = original_compare(*args, **kwargs)
                FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=False)
                return result
            with patch.object(service, "compare_pdf_bytes", side_effect=concurrent_change):
                with self.assertRaises(PDFComparisonError):
                    create_pdf_comparison(self.submission.pk)
            self.assertEqual(extract.call_count, 2)
            FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=True)
            Path(self.submission.formatted_pdf_file.path).unlink()
            with self.assertRaisesRegex(PDFComparisonError, "missing"):
                create_pdf_comparison(self.submission.pk)
            self.assertEqual(extract.call_count, 2)

    def test_service_transport_preserves_exports_and_rejects_late_or_wrong_results(self):
        from submissions.services import comparison_client
        from submissions.services.comparison_protocol import contract_id, job_key, page_render_hashes
        from submissions.services.pdf_comparison_execution import local_pdf_evidence
        contract = contract_id()
        hashes = [hashlib.sha256(data).hexdigest() for data in (self.original, self.corrected)]
        result = local_pdf_evidence(self.original, self.corrected)
        evidence = {
            "protocol": 1, "contract": contract, "key": job_key(hashes, contract),
            "hashes": hashes, "state": "completed", "result": result,
            "render_hashes": {"before": page_render_hashes(self.original), "after": page_render_hashes(self.corrected)},
        }
        def reply(endpoint, path, **kwargs):
            if path == "/v1/health":
                return 200, {"protocol": 1, "contract": contract, "state": "ready"}
            return 200, evidence
        state = self._state()
        publication = self._publication_contents()
        crosscheck = self._crosscheck_contents("SERVICEBEFORE")
        with (
            override_settings(CFM_COMPARISON_TRANSPORT="service", CFM_COMPARISON_LOCAL_URL="http://worker:8091", CFM_COMPARISON_LOCAL_TOKEN="t" * 32),
            patch.object(comparison_client.Endpoint, "call", autospec=True, side_effect=reply) as transport,
        ):
            first = create_pdf_comparison(self.submission.pk)
            self.assertEqual(first["result"], result)
            self.assertEqual(self._state(), state)
            self.assertEqual(self._publication_contents(), publication)
            self.assertEqual(self._crosscheck_contents("SERVICEAFTER"), crosscheck)
            evidence["hashes"] = list(reversed(hashes))
            with self.assertRaisesRegex(PDFComparisonError, "does not match"):
                create_pdf_comparison(self.submission.pk)
            self.assertEqual(self._state(), state)
            evidence["hashes"] = hashes
            def late_reply(endpoint, path, **kwargs):
                response = reply(endpoint, path, **kwargs)
                if path.startswith("/v1/jobs/"):
                    FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=False)
                return response
            transport.side_effect = late_reply
            with self.assertRaisesRegex(PDFComparisonError, "no longer a current"):
                create_pdf_comparison(self.submission.pk)
            FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=True)
            Path(self.submission.formatted_pdf_file.path).unlink()
            with self.assertRaisesRegex(PDFComparisonError, "missing"):
                create_pdf_comparison(self.submission.pk)
