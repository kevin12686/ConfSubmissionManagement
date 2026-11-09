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
from django.test import SimpleTestCase, override_settings
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
from submissions.tests.test_pdf_comparison import make_pdf, make_positioned_pdf


def ambiguous_outline_pdf_pair():
    """Return unmapped/known PDF bytes with two Unicode aliases for one outline."""
    from copy import deepcopy

    from fontTools.ttLib import TTFont

    from submissions.tests.test_pdf_glyph_evidence import font_bytes, pdf, replace_embedded_font

    with TTFont(io.BytesIO(font_bytes(features=False))) as known:
        known["glyf"]["B"] = deepcopy(known["glyf"]["A"])
        output = io.BytesIO()
        known.save(output)
        known_bytes = output.getvalue()
    with TTFont(io.BytesIO(known_bytes)) as unknown:
        del unknown["cmap"]
        output = io.BytesIO()
        unknown.save(output)
        unknown_bytes = output.getvalue()
    before = replace_embedded_font(pdf("A", font=known_bytes, missing=[2]), unknown_bytes)
    after = pdf("A", font=known_bytes)
    return before, after


class PDFComparisonOutlineRecoveryTests(SimpleTestCase):
    def test_unused_unicode_outline_alias_prevents_clean_recovery(self):
        before, after = ambiguous_outline_pdf_pair()
        self.assertNotEqual(before, after)
        result = compare_pdf_bytes(before, after)
        self.assertEqual(result["coverage_status"], "limited")
        self.assertTrue(result["coverage_reasons"])
        self.assertGreater(result["uncertain_count"], 0)


class PDFComparisonWorkerBoundaryTests(SimpleTestCase):
    def _worker_documents(self):
        documents = []
        for _ in range(2):
            tokens = []
            for index, word in enumerate(("one", "two", "six")):
                box = [0.1 + index * 0.2, 0.1, 0.2 + index * 0.2, 0.2]
                tokens.append({
                    "text": word, "word": word, "word_start": 0, "word_end": len(word),
                    "page": 1, "box": box, "glyphs": [box] * len(word), "issues": [],
                    "text_identity_supported": True,
                })
            documents.append({
                "tokens": tokens, "words": tokens, "controls": [],
                "pages": [{"number": 1, "width": 595, "height": 842}],
            })
        documents[0]["alignment"] = {"opcodes": [["equal", 0, 3, 0, 3]], "limited": False}
        return documents

    def _compare_worker_documents(self, documents, *, identical_bytes=False):
        before = make_pdf(["Worker input before"])
        after = before if identical_bytes else make_pdf(["Worker input after"])
        worker_result = subprocess.CompletedProcess(
            "evidence-worker", 0, stdout=json.dumps(documents).encode(),
        )
        with patch("submissions.services.pdf_comparison.subprocess.run", return_value=worker_result):
            return compare_pdf_bytes(before, after)

    def test_worker_process_failures_never_return_a_comparison(self):
        before, after = make_pdf(["Before"]), make_pdf(["After"])
        for failure in (
            subprocess.TimeoutExpired("evidence-worker", 120),
            subprocess.CalledProcessError(1, "evidence-worker"),
            OSError("worker unavailable"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch("submissions.services.pdf_comparison.subprocess.run", side_effect=failure):
                    with self.assertRaises(PDFComparisonError):
                        compare_pdf_bytes(before, after)

    def test_malformed_or_incomplete_worker_response_never_means_no_changes(self):
        before, after = make_pdf(["Before"]), make_pdf(["After"])
        for output in (b"not JSON", b"null", b"{}", b"[]", b"[{}, {}]"):
            with self.subTest(output=output):
                result = subprocess.CompletedProcess("evidence-worker", 0, stdout=output)
                with patch("submissions.services.pdf_comparison.subprocess.run", return_value=result):
                    with self.assertRaises(PDFComparisonError):
                        compare_pdf_bytes(before, after)

    def test_worker_alignment_rejects_duplicate_omitted_and_invalid_ranges(self):
        invalid_ranges = {
            "omitted_all": [],
            "omitted_tail": [["equal", 0, 2, 0, 2]],
            "omitted_middle": [["equal", 0, 1, 0, 1], ["equal", 2, 3, 2, 3]],
            "duplicate": [["equal", 0, 1, 0, 1], ["equal", 0, 1, 0, 1], ["equal", 1, 3, 1, 3]],
            "overlap": [["equal", 0, 2, 0, 2], ["equal", 1, 3, 1, 3]],
            "out_of_bounds": [["equal", 0, 4, 0, 4]],
            "negative": [["equal", -1, 3, -1, 3]],
            "reversed": [["replace", 0, -1, 0, 3]],
            "unequal_lengths": [["equal", 0, 2, 0, 3]],
            "empty": [["equal", 0, 0, 0, 0], ["equal", 0, 3, 0, 3]],
            "boolean_index": [["equal", False, 3, 0, 3]],
            "floating_index": [["equal", 0, 3.0, 0, 3]],
            "unknown_operation": [["keep", 0, 3, 0, 3]],
            "insert_consumes_before": [["insert", 0, 3, 0, 3]],
            "delete_consumes_after": [["delete", 0, 3, 0, 3]],
        }
        for name, opcodes in invalid_ranges.items():
            with self.subTest(case=name):
                documents = self._worker_documents()
                documents[0]["alignment"]["opcodes"] = opcodes
                with self.assertRaises(PDFComparisonError):
                    self._compare_worker_documents(documents)

    def test_worker_alignment_requires_explicit_structure_and_boolean_limit(self):
        for alignment in (
            None, {}, [], {"opcodes": [], "limited": 0},
            {"opcodes": "equal", "limited": False},
            {"opcodes": [None], "limited": False},
            {"opcodes": [["equal", 0, 3]], "limited": False},
        ):
            with self.subTest(alignment=alignment):
                documents = self._worker_documents()
                documents[0]["alignment"] = alignment
                with self.assertRaises(PDFComparisonError):
                    self._compare_worker_documents(documents)
        documents = self._worker_documents()
        del documents[0]["alignment"]
        with self.assertRaises(PDFComparisonError):
            self._compare_worker_documents(documents)

    def test_worker_equal_ranges_cannot_hide_text_context_or_unsupported_identity(self):
        for change in ("text", "context", "before_issue", "after_issue"):
            with self.subTest(change=change):
                documents = self._worker_documents()
                before, after = (document["tokens"][0] for document in documents)
                if change == "text":
                    after.update(text="ten", word="ten")
                elif change == "context":
                    before["context_key"] = ["results", "control"]
                    after["context_key"] = ["results", "treatment"]
                else:
                    (before if change == "before_issue" else after).update(
                        issues=["Unresolved glyph"], text_identity_supported=False,
                    )
                with self.assertRaises(PDFComparisonError):
                    self._compare_worker_documents(documents)

    def test_valid_equal_alignment_is_consumed_without_parent_token_matching(self):
        with patch("submissions.services.pdf_comparison.SequenceMatcher", side_effect=AssertionError("Unexpected parent matching")):
            result = self._compare_worker_documents(self._worker_documents())
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["token_coverage"]["aligned"], 3)

    def test_oversized_character_refinement_keeps_changed_word_without_parent_matching(self):
        documents = self._worker_documents()
        documents[1]["tokens"][0].update(text="ten", word="ten")
        documents[0]["alignment"] = {
            "opcodes": [["replace", 0, 1, 0, 1], ["equal", 1, 3, 1, 3]], "limited": False,
        }
        with patch("submissions.services.pdf_comparison.MAX_PAIRING_STEPS", 4):
            with patch("submissions.services.pdf_comparison.SequenceMatcher", side_effect=AssertionError("Unbounded character refinement")):
                result = self._compare_worker_documents(documents)
        finding, = result["findings"]
        self.assertEqual(finding["before_text"], "one")
        self.assertEqual(finding["after_text"], "ten")
        self.assertTrue(finding["before_locations"])
        self.assertTrue(finding["after_locations"])

    def test_identical_bytes_allow_uncertain_equality_but_keep_coverage_limited(self):
        documents = self._worker_documents()
        for document in documents:
            document["tokens"][0].update(issues=["Unresolved glyph"], text_identity_supported=False)
            document["coverage_reasons"] = ["Unresolved glyph"]
        result = self._compare_worker_documents(documents, identical_bytes=True)
        self.assertTrue(result["identical_bytes"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["coverage_status"], "limited")
        self.assertIn("Unresolved glyph", result["coverage_reasons"])

    def test_supported_opaque_equality_retains_uncertainty_and_structure_changes(self):
        documents = self._worker_documents()
        for document, role in zip(documents, ("super", "sub")):
            token = document["tokens"][0]
            token.update(
                text="\ufffc", word="\ufffc", word_end=1, glyphs=[token["box"]],
                opaque_key="a" * 64, text_identity_supported=True,
                issues=["Unicode unresolved"], char_meta=[{"script_role": role}],
            )
            document["coverage_reasons"] = ["Unicode unresolved"]
        result = self._compare_worker_documents(documents)
        self.assertEqual(result["token_coverage"]["aligned"], 3)
        self.assertEqual(result["coverage_status"], "limited")
        self.assertIn("Unicode unresolved", result["coverage_reasons"])
        finding, = result["findings"]
        self.assertEqual(finding["category"], "structure")
        self.assertTrue(finding["before_locations"])
        self.assertTrue(finding["after_locations"])

    def _opaque_worker_documents(self):
        documents = self._worker_documents()
        for document in documents:
            token = document["tokens"][0]
            token.update(
                text="\ufffc", word="\ufffc", word_end=1, glyphs=[token["box"]],
                opaque_key="a" * 64, text_identity_supported=True,
                issues=["Unicode unresolved"],
            )
        return documents

    def test_worker_opaque_identity_requires_strict_key_atom_flag_and_warning(self):
        mutations = (
            ("text_identity_supported", None), ("text_identity_supported", 1),
            ("text_identity_supported", "true"), ("issues", []),
            ("opaque_key", ""), ("opaque_key", "a" * 63), ("opaque_key", "a" * 65),
            ("opaque_key", "A" * 64), ("opaque_key", "g" * 64), ("opaque_key", 123),
            ("text", "A"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                documents = self._opaque_worker_documents()
                documents[0]["tokens"][0][field] = value
                with self.assertRaises(PDFComparisonError):
                    self._compare_worker_documents(documents)
        for missing in ("text_identity_supported", "issues"):
            with self.subTest(missing=missing):
                documents = self._opaque_worker_documents()
                del documents[0]["tokens"][0][missing]
                with self.assertRaises(PDFComparisonError):
                    self._compare_worker_documents(documents)

    def test_worker_opaque_equality_rejects_unsupported_identity_and_changed_key_or_context(self):
        for field, value in (
            ("text_identity_supported", False), ("opaque_key", "b" * 64),
            ("context_key", ["results", "treatment"]),
        ):
            with self.subTest(field=field):
                documents = self._opaque_worker_documents()
                documents[1]["tokens"][0][field] = value
                with self.assertRaises(PDFComparisonError):
                    self._compare_worker_documents(documents)

    def test_opaque_alignment_cannot_report_complete_when_document_reasons_are_omitted(self):
        result = self._compare_worker_documents(self._opaque_worker_documents())
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["token_coverage"]["aligned"], 3)
        self.assertEqual(result["coverage_status"], "limited")
        self.assertIn("Unicode unresolved", result["coverage_reasons"])

    def test_worker_limited_alignment_never_becomes_complete_after_pairing(self):
        documents = self._worker_documents()
        documents[0]["alignment"] = {"opcodes": [["replace", 0, 3, 0, 3]], "limited": True}
        result = self._compare_worker_documents(documents)
        self.assertTrue(result["pairing_limited"])
        self.assertEqual(result["coverage_status"], "limited")
        self.assertTrue(result["coverage_reasons"])

    def test_structure_findings_respect_location_limit_and_report_limited_coverage(self):
        token = {
            "text": "xy", "word": "xy", "word_start": 0, "word_end": 2,
            "page": 1, "box": [0.1, 0.1, 0.3, 0.2], "issues": [],
            "glyphs": [[0.1, 0.1, 0.2, 0.2], [0.2, 0.1, 0.3, 0.2]],
        }
        documents = [{
            "tokens": [{**token, "char_meta": [{"script_role": role}] * 2}],
            "words": [token], "controls": [],
            "pages": [{"number": 1, "width": 595, "height": 842}],
        } for role in ("super", "sub")]
        documents[0]["alignment"] = {"opcodes": [["equal", 0, 1, 0, 1]], "limited": False}
        worker_result = subprocess.CompletedProcess(
            "evidence-worker", 0, stdout=json.dumps(documents).encode(),
        )
        with patch("submissions.services.pdf_comparison.subprocess.run", return_value=worker_result):
            with patch("submissions.services.pdf_comparison.MAX_BOXES", 1):
                result = compare_pdf_bytes(make_pdf(["Before"]), make_pdf(["After"]))
        finding, = result["findings"]
        self.assertEqual(finding["category"], "structure")
        self.assertLessEqual(len(finding["before_locations"]), 1)
        self.assertLessEqual(len(finding["after_locations"]), 1)
        self.assertTrue(finding["locations_truncated"])
        self.assertEqual(result["coverage_status"], "limited")


class PDFComparisonMoveContextTests(SimpleTestCase):
    def test_values_swapped_between_fixed_labels_remain_changes(self):
        templates = (
            "{label}: {value}.",
            "{label} = {value}.",
            "{label} reported {value}.",
            "{label} ({value}).",
            "{value} were observed for {label}.",
        )
        for template in templates:
            with self.subTest(template=template):
                before = "\n".join((
                    template.format(label="Control group", value="red green blue"),
                    template.format(label="Treatment group", value="orange white black"),
                ))
                after = "\n".join((
                    template.format(label="Control group", value="orange white black"),
                    template.format(label="Treatment group", value="red green blue"),
                ))
                result = compare_pdf_bytes(make_pdf([before]), make_pdf([after]))
                changes = [finding for finding in result["findings"] if finding["category"] == "text"]
                self.assertTrue(changes)
                changed_text = " ".join(finding["before_text"] + " " + finding["after_text"] for finding in changes)
                self.assertTrue(any(value in changed_text for value in ("red green blue", "orange white black")))
                context = " ".join(finding["before_context"] + " " + finding["after_context"] for finding in changes)
                for word in ("red", "green", "blue", "orange", "white", "black"):
                    self.assertIn(word, context)
                self.assertIn("Control group", context)
                self.assertIn("Treatment group", context)

    def test_neither_partial_value_can_be_consumed_by_secondary_pairing(self):
        from submissions.services.pdf_comparison import _sequence_pairs

        def tokens(values):
            result = []
            for label, value in zip(("Control", "Treatment"), values):
                words = [label, "group", ":", *value.split(), "."]
                for word in words:
                    index = len(result)
                    result.append({
                        "text": word, "page": 1, "box": [index, 0, index + 1, 1],
                        "raw_context": f"{label} group: {value}.",
                    })
            return result

        before = tokens(("red green blue", "orange white black"))
        after = tokens(("orange white black", "red green blue"))
        pairs, used_before, used_after, limited = _sequence_pairs(
            before, after, [("replace", 3, 6, 3, 6), ("replace", 10, 13, 10, 13)],
        )
        self.assertEqual((pairs, used_before, used_after), ([], set(), set()))
        self.assertFalse(limited)

    def test_next_line_values_cannot_move_independently_of_explicit_labels(self):
        for marker in (":", "="):
            for punctuation in ("", "."):
                with self.subTest(marker=marker, punctuation=punctuation):
                    before = make_pdf([
                        f"Control group{marker}\nred green blue{punctuation}\n"
                        f"Treatment group{marker}\norange white black{punctuation}"
                    ])
                    after = make_pdf([
                        f"Control group{marker}\norange white black{punctuation}\n"
                        f"Treatment group{marker}\nred green blue{punctuation}"
                    ])
                    result = compare_pdf_bytes(before, after)
                    self.assertEqual(result["token_coverage"]["paired"], 0)
                    changes = " ".join(finding["before_text"] + " " + finding["after_text"] for finding in result["findings"])
                    self.assertTrue(any(value in changes for value in ("red green blue", "orange white black")))

    def test_explicit_label_and_next_line_value_can_relocate_together(self):
        for marker in (":", "="):
            with self.subTest(marker=marker):
                control = f"Control group{marker}\nred green blue"
                treatment = f"Treatment group{marker}\norange white black"
                result = compare_pdf_bytes(
                    make_pdf([control + "\n" + treatment]),
                    make_pdf([treatment + "\n" + control]),
                )
                self.assertEqual(result["findings"], [])
                self.assertGreater(result["token_coverage"]["paired"], 0)

    def test_complete_unpunctuated_flow_line_relocation_is_not_a_change(self):
        moved = "Participants retained voluntary withdrawal rights"
        anchor = "The independent research protocol records all assessment methods and the final outcomes."
        before = make_positioned_pdf([[(80, moved), (180, anchor)]])
        after = make_positioned_pdf([[(80, anchor), (180, moved)]])
        result = compare_pdf_bytes(before, after)
        self.assertEqual(result["findings"], [])
        self.assertGreater(result["token_coverage"]["paired"], 0)

    def test_complete_sentence_still_pairs_beside_a_changed_score(self):
        sentence = "Participants could withdraw voluntarily."
        anchor = "The shared detailed protocol describes all independent assessment methods and research results."
        before = make_pdf([sentence + " Score: 12.\n" + anchor])
        after = make_pdf([anchor + "\n" + sentence + " Score: 21."])
        result = compare_pdf_bytes(before, after)
        self.assertGreater(result["token_coverage"]["paired"], 0)
        changed = " ".join(finding["before_text"] + " " + finding["after_text"] for finding in result["findings"])
        self.assertIn("12", changed)
        self.assertIn("21", changed)
        self.assertNotIn("withdraw voluntarily", changed)

    def test_complete_line_on_only_one_side_does_not_prove_fragment_relocation(self):
        anchor = "The independent assessment protocol records every study outcome and all research methods."
        before = make_pdf(["Control group reported red green blue.\n" + anchor])
        after = make_pdf([anchor + "\nTreatment group reported\nred green blue."])
        for left, right in ((before, after), (after, before)):
            with self.subTest(reverse=left is after):
                result = compare_pdf_bytes(left, right)
                changes = " ".join(finding["before_text"] + " " + finding["after_text"] for finding in result["findings"])
                self.assertIn("red green blue", changes)

    def test_ambiguous_semantic_owner_cannot_be_consumed_as_moved_text(self):
        from submissions.services.pdf_comparison import _sequence_pairs

        tokens = [{
            "text": word, "page": 1, "box": [index, 0, index + 1, 1],
            "raw_context": "red green blue", "context_key": "shared-table",
            "semantic_context": {"ambiguous": True},
        } for index, word in enumerate(("red", "green", "blue"))]
        pairs, used_before, used_after, limited = _sequence_pairs(
            tokens, tokens, [("replace", 0, 3, 0, 3)],
        )
        self.assertEqual((pairs, used_before, used_after), ([], set(), set()))
        self.assertFalse(limited)


class PDFComparisonSafetyRegressionTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
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

                def compare_then_change(*args):
                    result = compare_pdf_bytes(*args)
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

                def compare_then_upload(*args):
                    result = compare_pdf_bytes(*args)
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

        def compare_then_upload_source(*args):
            result = compare_pdf_bytes(*args)
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
                with patch("submissions.services.pdf_comparison.subprocess.run", **failure) as worker:
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
