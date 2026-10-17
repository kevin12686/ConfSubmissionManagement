import json
import random
import time
import zipfile
from collections import Counter
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fitz
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse

from submissions.models import FinalSubmission
from submissions.services.checks import publication_readiness_rows
from submissions.services.formatting_history import capture_existing_formatting
from submissions.services.pdf_comparison import (
    PDFComparisonError, _extract, _scanned_findings, compare_pdf_bytes, comparison_page_png,
    comparison_pdf_bytes, comparison_root, create_pdf_comparison, load_pdf_comparison,
)
from submissions.services.publication_read import publication_database_signature
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase


def make_pdf(pages, *, rotation=0, image_only=False):
    with fitz.open() as document:
        for text in pages:
            page = document.new_page(width=595, height=842)
            if text:
                page.insert_textbox((55, 65, 540, 760), text, fontsize=12)
            if image_only:
                page.draw_rect((80, 90, 250, 180), color=(0, 0, 0), fill=(0.7, 0.8, 0.9))
            page.set_rotation(rotation)
        return document.tobytes()


def make_positioned_pdf(pages):
    with fitz.open() as document:
        for lines in pages:
            page = document.new_page(width=595, height=842)
            for y, text in lines:
                page.insert_text((55, y), text, fontsize=10)
        return document.tobytes()


def reflow_pair(*, header="2 N. Silvis-Cividjian", extra_pages=0):
    first = "The initial study evaluated several independent approaches and recorded detailed outcomes for every participant."
    crossing = "Medical physicists involved in the accidents described the hero who"
    last = "discovered the failure mechanism and prevented further losses through careful independent technical investigation."
    before = make_positioned_pdf([
        [(100, "A study (Journal-First)"), (200, first)],
        [(65, "2 N. Silvis-Cividjian"), (130, crossing), (200, last)],
    ])
    after = make_positioned_pdf([
        [(100, "A study"), (200, first), (250, crossing)],
        [(65, header), (200, last)],
    ] + [[] for _ in range(extra_pages)])
    return before, after


class PDFComparisonTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(BASE_DIR=self.root.resolve()))
        self.make_master_paper()
        self.submission = self.make_final_submission()
        self.before = make_pdf(["The results are shown in Table 2.\nParticipation was voluntary. All students could withdraw."])
        self.after = make_pdf(["The results are shown in Error! Reference source not found.\nThe final results were promising."])
        self.submission.pdf_file.save("original.pdf", ContentFile(self.before))
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(self.after))
        self.url = reverse("submissions:formatting_pdf_comparison", args=[self.submission.pk])

    def test_build_is_audited_and_cannot_change_publication_or_review_state(self):
        self.mark_submission_publication_ready(self.submission)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        readiness = publication_readiness_rows(strict_hash=True)
        payload = create_pdf_comparison(self.submission.pk)
        self.assertTrue(any(item["category"] == "reference" for item in payload["result"]["findings"]))
        self.assertTrue(any(item["category"] == "text" for item in payload["result"]["findings"]))
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)
        self.assertEqual(Path(self.submission.pdf_file.path).read_bytes(), self.before)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), self.after)
        self.assertIn('"action": "formatting_pdf_compare"', (self.audit_root / "audit.log").read_text())

    def test_whitespace_only_changes_do_not_report_removed_content(self):
        result = compare_pdf_bytes(make_pdf(["One two three\nfour five six"]), make_pdf(["One  two three four\nfive six"]))
        self.assertEqual(result["findings"], [])
        self.assertTrue(result["text_available"])
        self.assertFalse(result["identical_bytes"])

    def assert_text_accounted_for(self, before, after, result):
        self.assertFalse(result["truncated"])
        a, b = _extract(before)["tokens"], _extract(after)["tokens"]
        removed, added, seen_a, seen_b = Counter(), Counter(), set(), set()
        for finding in result["findings"]:
            if finding["category"] not in {"text", "sequence"}:
                continue
            left, right = finding["before_token_range"], finding["after_token_range"]
            self.assertFalse(seen_a.intersection(range(*left)))
            self.assertFalse(seen_b.intersection(range(*right)))
            seen_a.update(range(*left))
            seen_b.update(range(*right))
            words_a = [item["text"] for item in a[slice(*left)]]
            words_b = [item["text"] for item in b[slice(*right)]]
            if finding["category"] == "sequence":
                self.assertEqual(words_a, words_b)
            else:
                removed.update(words_a)
                added.update(words_b)
        before_counts = Counter(item["text"] for item in a)
        after_counts = Counter(item["text"] for item in b)
        self.assertEqual(before_counts - after_counts, removed - added)
        self.assertEqual(after_counts - before_counts, added - removed)
        coverage = result["token_coverage"]
        for side in ("before", "after"):
            self.assertEqual(coverage["aligned"] + coverage["paired"] + coverage[f"unmatched_{side}"], coverage[f"{side}_total"])

    def test_repagination_pairs_unchanged_header_without_hiding_title_change(self):
        before, after = reflow_pair()
        result = compare_pdf_bytes(before, after)
        self.assertEqual(result["attention_count"], 1)
        self.assertEqual(result["sequence_count"], 1)
        change, same = result["findings"]
        self.assertEqual(change["before_text"], "(Journal-First)")
        self.assertEqual(change["after_pages"], [])
        self.assertIsNone(change["after_page"])
        self.assertEqual(change["after_reference_page"], 1)
        self.assertEqual(same["before_text"], "2 N. Silvis-Cividjian")
        self.assertEqual(same["after_text"], same["before_text"])
        self.assertEqual(same["before_pages"], [2])
        self.assertEqual(same["after_pages"], [2])
        self.assertEqual(same["before_locations"], same["after_locations"])
        self.assert_text_accounted_for(before, after, result)

    def test_changed_or_removed_header_is_not_discarded_as_layout_noise(self):
        for header in ("2 N. Changed-Author", ""):
            with self.subTest(header=header):
                before, after = reflow_pair(header=header)
                result = compare_pdf_bytes(before, after)
                changed = [row for row in result["findings"] if row["category"] == "text"]
                self.assertIn("Silvis-Cividjian", " ".join(row["before_text"] for row in changed))
                if header:
                    self.assertIn("Changed-Author", " ".join(row["after_text"] for row in changed))
                self.assert_text_accounted_for(before, after, result)

    def test_header_footer_and_table_values_remain_in_all_text_and_pattern_checks(self):
        before = make_positioned_pdf([[(30, "Conference 2025"), (200, "Table: 12 participants"), (800, "References are in section 3.")]])
        after = make_positioned_pdf([[(30, "Conference 2026"), (200, "Table: 21 participants"), (800, "Error! Reference source not found.")]])
        result = compare_pdf_bytes(before, after)
        changed = [row for row in result["findings"] if row["category"] == "text"]
        self.assertIn("2025", " ".join(row["before_text"] for row in changed))
        self.assertIn("2026", " ".join(row["after_text"] for row in changed))
        self.assertIn("21", " ".join(row["after_text"] for row in changed))
        self.assertTrue(any(row["category"] == "reference" and row["after_locations"][0]["box"][1] > .9 for row in result["findings"]))
        self.assert_text_accounted_for(before, after, result)

    def test_moved_unique_passage_is_retained_as_a_two_sided_finding(self):
        anchor = "The common study protocol describes all assessment methods and independently collected research outcomes."
        moved = "Participants could withdraw voluntarily."
        before, after = make_pdf([moved + "\n" + anchor]), make_pdf([anchor + "\n" + moved])
        result = compare_pdf_bytes(before, after)
        same = [row for row in result["findings"] if row["category"] == "sequence"]
        self.assertEqual(len(same), 1)
        self.assertEqual(same[0]["before_text"], moved)
        self.assertNotEqual(same[0]["before_locations"], same[0]["after_locations"])
        self.assert_text_accounted_for(before, after, result)

    def test_changed_words_beside_a_moved_passage_remain_content_findings(self):
        anchor = "The shared detailed protocol describes all independent assessment methods and research results."
        before = make_pdf(["Participants could withdraw voluntarily. Score: 12.\n" + anchor])
        after = make_pdf([anchor + "\nParticipants could withdraw voluntarily. Score: 21."])
        result = compare_pdf_bytes(before, after)
        content = [row for row in result["findings"] if row["category"] == "text"]
        self.assertIn("12", " ".join(row["before_text"] for row in content))
        self.assertIn("21", " ".join(row["after_text"] for row in content))
        self.assertTrue(result["sequence_count"])
        self.assert_text_accounted_for(before, after, result)

    def test_deleted_repeated_passage_cannot_be_cancelled_by_another_occurrence(self):
        phrase = "All participants completed the survey."
        anchor = "The independent study protocol describes assessment methods and presents detailed research outcomes."
        before = make_pdf([phrase + "\n" + anchor + "\n" + phrase])
        after = make_pdf([anchor + "\n" + phrase])
        result = compare_pdf_bytes(before, after)
        self.assertEqual(result["sequence_count"], 0)
        self.assertIn(phrase, " ".join(row["before_text"] for row in result["findings"] if row["category"] == "text"))
        self.assert_text_accounted_for(before, after, result)

    def test_word_prefixes_and_punctuation_changes_are_never_hidden_by_pairing(self):
        anchor = "The independent research protocol includes multiple assessments and records all final study outcomes."
        before = make_pdf(["Participants could withdraw voluntarily. Smithson: 12!\n" + anchor])
        after = make_pdf([anchor + "\nParticipants could withdraw voluntarily. Smith: 12?"])
        result = compare_pdf_bytes(before, after)
        self.assertTrue(result["attention_count"])
        self.assert_text_accounted_for(before, after, result)

    def test_all_passage_pages_are_kept_even_when_highlight_regions_are_capped(self):
        before = make_pdf(["removed alpha beta", "removed gamma delta", "retained final conclusion"])
        after = make_pdf(["retained final conclusion"])
        with patch("submissions.services.pdf_comparison.MAX_BOXES", 1):
            result = compare_pdf_bytes(before, after)
        finding = result["findings"][0]
        self.assertEqual(finding["before_pages"], [1, 2])
        self.assertEqual(finding["after_pages"], [])
        self.assertIsNone(finding["after_page"])
        self.assertIsNone(finding["after_reference_page"])
        self.assertTrue(finding["locations_truncated"])
        self.assert_text_accounted_for(before, after, result)

    def test_unmapped_insertions_do_not_invent_a_baseline_text_location(self):
        result = compare_pdf_bytes(make_pdf(["Completely unrelated old baseline"]), make_pdf(["Completely unrelated old baseline", "Newly appended extra passage"]))
        finding = next(row for row in result["findings"] if row["label"] == "Added text")
        self.assertEqual(finding["before_pages"], [])
        self.assertIsNone(finding["before_page"])
        self.assertIsNone(finding["before_reference_page"])
        self.assertEqual(finding["after_pages"], [2])

    def test_pairing_limit_preserves_all_unpaired_text_and_marks_the_limit(self):
        before, after = reflow_pair()
        with patch("submissions.services.pdf_comparison.MAX_PAIRING_STEPS", 0):
            result = compare_pdf_bytes(before, after)
        self.assertTrue(result["pairing_limited"])
        self.assertEqual(result["sequence_count"], 0)
        self.assertEqual(result["attention_count"], 3)
        self.assert_text_accounted_for(before, after, result)

    def test_randomized_reorders_deletions_edits_and_duplicates_preserve_text_accounting(self):
        rng = random.Random(20261016)
        original = [f"Research group {index} recorded independent results for cohort {index}." for index in range(9)]
        for case in range(40):
            lines = original.copy()
            rng.shuffle(lines)
            lines.pop(rng.randrange(len(lines)))
            index = rng.randrange(len(lines))
            lines[index] = lines[index].replace("recorded", "revised").replace("results", "outcomes!")
            if case % 2:
                lines.append(lines[0])
            before, after = make_pdf(["\n".join(original)]), make_pdf(["\n".join(lines)])
            result = compare_pdf_bytes(before, after)
            with self.subTest(case=case):
                self.assertTrue(result["attention_count"])
                self.assert_text_accounted_for(before, after, result)

    def test_patterns_are_still_scanned_when_baseline_has_no_extractable_text(self):
        result = compare_pdf_bytes(make_pdf([""]), make_pdf(["Error! Reference source not found."]))
        self.assertFalse(result["text_available"])
        self.assertTrue(any(row["category"] == "reference" for row in result["findings"]))
        self.assertFalse(any("New" in row["label"] for row in result["findings"]))

    def test_number_punctuation_and_case_changes_are_not_normalized_away(self):
        result = compare_pdf_bytes(make_pdf(["Result A: 12 students [3]."]), make_pdf(["Result a: 21 students [4]!"]))
        changes = [row for row in result["findings"] if row["category"] == "text"]
        self.assertTrue(changes)
        self.assertIn("12", " ".join(row["before_text"] for row in changes))
        self.assertIn("21", " ".join(row["after_text"] for row in changes))

    def test_existing_moved_reference_errors_are_not_called_new(self):
        error = "Error! Reference source not found."
        result = compare_pdf_bytes(make_pdf([error, "Study results"]), make_pdf(["Study results", error]))
        self.assertFalse(any(item["category"] == "reference" for item in result["findings"]))

    def test_replacement_character_and_placeholders_are_advisory_patterns(self):
        def document(words):
            return {"words": [{"text": text, "page": 1, "box": [0, 0, .2, .1]} for text in words]}
        findings = _scanned_findings(document(["confidence"]), document(["confi\ufffdence", "??", "[?]"]))
        self.assertEqual(dict(Counter(item["category"] for item in findings)), {"character": 1, "placeholder": 2})

    def test_page_mapping_follows_matching_content_not_page_number(self):
        first = "We evaluated students across several programming exercises and collected feedback after every completed task."
        second = "The final analysis included independent ratings from two instructors and all participants completed the study."
        result = compare_pdf_bytes(make_pdf([first, second]), make_pdf(["A new cover page", first, second]))
        self.assertEqual(result["page_map"]["1"], 2)
        self.assertEqual(result["page_map"]["2"], 3)
        self.assertEqual(result["inverse_map"]["2"], 1)

    def test_a_shared_header_cannot_create_a_confident_page_match(self):
        header = "Proceedings of the annual conference on software education teaching assessment and learning practice.\n"
        before = make_pdf([header + " ".join(f"alpha{index}" for index in range(100))])
        after = make_pdf([header + " ".join(f"beta{index}" for index in range(100))])
        result = compare_pdf_bytes(before, after)
        self.assertEqual(result["page_map"], {})

    def test_image_only_pdfs_have_no_false_text_completeness_verdict(self):
        result = compare_pdf_bytes(make_pdf([""], image_only=True), make_pdf([""]))
        self.assertFalse(result["text_available"])
        self.assertFalse(result["identical_bytes"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["page_map"], {})

    def test_missing_visual_content_can_have_identical_text_and_is_not_called_safe(self):
        with fitz.open(stream=make_pdf(["Table 2. Study outcomes are discussed below."]), filetype="pdf") as document:
            document[0].draw_rect((80, 240, 420, 350), color=(0, 0, 0))
            with_table = document.tobytes()
        result = compare_pdf_bytes(with_table, make_pdf(["Table 2. Study outcomes are discussed below."]))
        self.assertTrue(result["text_available"])
        self.assertEqual(result["findings"], [])
        self.assertFalse(result["identical_bytes"])

    def test_password_protected_pdf_is_not_decrypted_or_modified(self):
        with fitz.open(stream=self.after, filetype="pdf") as document:
            encrypted = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
        self.submission.formatted_pdf_file.save("encrypted.pdf", ContentFile(encrypted))
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        with self.assertRaisesRegex(PDFComparisonError, "Password-protected"):
            create_pdf_comparison(self.submission.pk)
        self.assertEqual(Path(self.submission.formatted_pdf_file.path).read_bytes(), encrypted)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

    def test_rotated_pdf_regions_are_inside_rendered_page(self):
        result = compare_pdf_bytes(make_pdf(["Before title"], rotation=90), make_pdf(["After title"], rotation=90))
        self.assertGreater(result["after_pages"][0]["width"], result["after_pages"][0]["height"])
        for finding in result["findings"]:
            for location in finding["before_locations"] + finding["after_locations"]:
                x0, y0, x1, y1 = location["box"]
                self.assertTrue(0 <= x0 <= x1 <= 1)
                self.assertTrue(0 <= y0 <= y1 <= 1)

    def test_current_corrected_missing_never_falls_back_to_original_or_history(self):
        capture_existing_formatting(self.submission.pk)
        Path(self.submission.formatted_pdf_file.path).unlink()
        with self.assertRaisesRegex(PDFComparisonError, "missing"):
            create_pdf_comparison(self.submission.pk)
        self.assertFalse(comparison_root().exists())

    def test_missing_original_requires_an_explicit_revision_baseline(self):
        revision = capture_existing_formatting(self.submission.pk)
        Path(self.submission.pdf_file.path).unlink()
        with self.assertRaisesRegex(PDFComparisonError, "missing"):
            create_pdf_comparison(self.submission.pk)
        payload = create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}")
        self.assertTrue(payload["result"]["identical_bytes"])

    def test_decision_required_or_inactive_scope_is_never_silently_widened(self):
        from submissions.models import InitialPaper
        InitialPaper.objects.filter(paper_id="P001").update(publication_decision_status="decision_required")
        with self.assertRaisesRegex(PDFComparisonError, "no longer"):
            create_pdf_comparison(self.submission.pk)
        InitialPaper.objects.filter(paper_id="P001").update(publication_decision_status="publishing")
        FinalSubmission.objects.filter(pk=self.submission.pk).update(active_version=False)
        with self.assertRaisesRegex(PDFComparisonError, "no longer"):
            create_pdf_comparison(self.submission.pk)

    def test_ambiguous_active_versions_cannot_produce_a_publication_comparison(self):
        self.make_final_submission(final_submission_id="101")
        with self.assertRaisesRegex(PDFComparisonError, "Multiple active"):
            create_pdf_comparison(self.submission.pk)

    def test_revision_baseline_is_scoped_and_integrity_verified(self):
        revision = capture_existing_formatting(self.submission.pk)
        self.submission.formatted_pdf_file.save("next.pdf", ContentFile(make_pdf(["Latest corrected version"])))
        payload = create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}")
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "before"), self.after)
        self.assertNotEqual(payload["before_sha256"], payload["after_sha256"])
        other = self.make_final_submission(final_submission_id="OTHER", active_version=False)
        other.formatted_pdf_file.save("other.pdf", ContentFile(self.after))
        other_revision = capture_existing_formatting(other.pk)
        with self.assertRaisesRegex(PDFComparisonError, "does not belong"):
            create_pdf_comparison(self.submission.pk, f"revision-{other_revision.pk}")
        Path(revision.pdf_file.path).write_bytes(b"corrupt")
        with self.assertRaisesRegex(PDFComparisonError, "integrity"):
            create_pdf_comparison(self.submission.pk, f"revision-{revision.pk}")

    def test_stale_scope_hash_and_expiry_are_rejected_for_page_and_pdf(self):
        payload = create_pdf_comparison(self.submission.pk)
        self.assertTrue(comparison_page_png(self.submission.pk, payload["token"], "after", 1).startswith(b"\x89PNG"))
        Path(self.submission.formatted_pdf_file.path).write_bytes(make_pdf(["Changed outside upload workflow"]))
        with self.assertRaisesRegex(PDFComparisonError, "changed after"):
            load_pdf_comparison(self.submission.pk, payload["token"])
        self.submission.formatted_pdf_file.save("restored.pdf", ContentFile(self.after))
        payload = create_pdf_comparison(self.submission.pk)
        FinalSubmission.objects.filter(pk=self.submission.pk).update(discarded=True, active_version=False)
        with self.assertRaisesRegex(PDFComparisonError, "no longer"):
            comparison_page_png(self.submission.pk, payload["token"], "after", 1)
        FinalSubmission.objects.filter(pk=self.submission.pk).update(discarded=False, active_version=True)
        with patch("django.core.signing.time.time", return_value=time.time() + timedelta(hours=3).total_seconds()):
            with self.assertRaisesRegex(PDFComparisonError, "expired"):
                load_pdf_comparison(self.submission.pk, payload["token"])

    def test_snapshot_corruption_and_token_traversal_are_rejected(self):
        payload = create_pdf_comparison(self.submission.pk)
        directory = comparison_root() / payload["token"]
        (directory / "after.pdf").write_bytes(b"bad")
        with self.assertRaisesRegex(PDFComparisonError, "integrity"):
            comparison_page_png(self.submission.pk, payload["token"], "after", 1)
        for token in ("../outside", "x" * 32, ""):
            with self.subTest(token=token), self.assertRaises(PDFComparisonError):
                load_pdf_comparison(self.submission.pk, token)
        wrapper = json.loads((directory / "payload.json").read_text())
        wrapper["signed"] += "corrupt"
        (directory / "payload.json").write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(PDFComparisonError, "unavailable"):
            load_pdf_comparison(self.submission.pk, payload["token"])

    def test_old_method_requires_explicit_rebuild_without_rewriting_saved_comparison(self):
        from django.core import signing
        payload = create_pdf_comparison(self.submission.pk)
        path = comparison_root() / payload["token"] / "payload.json"
        wrapper = json.loads(path.read_text())
        payload["version"] = 1
        wrapper["signed"] = signing.dumps(payload, salt="submissions.pdf-comparison.v1", compress=True)
        path.write_text(json.dumps(wrapper))
        old_bytes = path.read_bytes()
        response = self.client.get(self.url, {"comparison": payload["token"], "baseline": "original"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "older comparison method", status_code=409)
        self.assertContains(response, "Build comparison", status_code=409)
        self.assertEqual(path.read_bytes(), old_bytes)

    def test_scope_and_current_bytes_are_bound_but_notes_and_review_are_not(self):
        payload = create_pdf_comparison(self.submission.pk)
        self.submission.format_notes = "An unrelated editorial note"
        self.submission.format_status = "needs_edit"
        self.submission.save()
        self.assertEqual(load_pdf_comparison(self.submission.pk, payload["token"])["token"], payload["token"])
        with self.assertRaisesRegex(PDFComparisonError, "does not belong"):
            load_pdf_comparison(self.submission.pk + 1, payload["token"])

    def test_new_comparison_purges_only_expired_staging(self):
        from django.utils import timezone
        old = create_pdf_comparison(self.submission.pk)
        directory = comparison_root() / old["token"]
        old_time = timezone.now() - timedelta(hours=3)
        wrapper = json.loads((directory / "payload.json").read_text())
        wrapper["created_at"] = old_time.isoformat()
        (directory / "payload.json").write_text(json.dumps(wrapper))
        recent = create_pdf_comparison(self.submission.pk)
        self.assertFalse(directory.exists())
        self.assertTrue((comparison_root() / recent["token"]).exists())

    def test_selected_pdf_paths_cannot_be_overridden_by_post_fields(self):
        response = self.client.post(self.url, {"baseline": "original", "pdf_path": "/etc/passwd", "target": "original"})
        page = self.client.get(response.url)
        target = self.client.get(page.context["comparison_data"]["pdf_urls"]["after"])
        self.assertEqual(b"".join(target.streaming_content), self.after)
        target.close()

    def test_compare_detects_concurrent_file_change_without_publishing_stale_result(self):
        from submissions.services.pdf_comparison import compare_pdf_bytes as compare
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        def compare_then_replace(*args):
            result = compare(*args)
            Path(self.submission.formatted_pdf_file.path).write_bytes(make_pdf(["Concurrent bytes"]))
            return result
        with patch("submissions.services.pdf_comparison.compare_pdf_bytes", side_effect=compare_then_replace):
            with self.assertRaisesRegex(PDFComparisonError, "changed during"):
                create_pdf_comparison(self.submission.pk)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertFalse(comparison_root().exists())

    def test_failed_audit_and_invalid_pdf_leave_no_partial_comparison(self):
        with patch("submissions.services.pdf_comparison.audit_success", side_effect=OSError("audit unavailable")):
            with self.assertRaisesRegex(PDFComparisonError, "audit unavailable"):
                create_pdf_comparison(self.submission.pk)
        self.assertEqual(list(comparison_root().iterdir()), [])
        self.submission.formatted_pdf_file.save("invalid.pdf", ContentFile(b"not a PDF"))
        with self.assertRaises(PDFComparisonError):
            create_pdf_comparison(self.submission.pk)
        self.assertEqual(list(comparison_root().iterdir()), [])

    def test_failed_staging_creation_never_deletes_an_existing_comparison(self):
        from types import SimpleNamespace
        payload = create_pdf_comparison(self.submission.pk)
        with patch("submissions.services.pdf_comparison.uuid.uuid4", return_value=SimpleNamespace(hex=payload["token"])):
            with self.assertRaises(PDFComparisonError):
                create_pdf_comparison(self.submission.pk)
        self.assertEqual(comparison_pdf_bytes(self.submission.pk, payload["token"], "after"), self.after)

    def test_limits_are_explicit_not_silently_truncated_as_success(self):
        with patch("submissions.services.pdf_comparison.MAX_TOKENS", 1):
            with self.assertRaisesRegex(PDFComparisonError, "token"):
                create_pdf_comparison(self.submission.pk)
        with patch("submissions.services.pdf_comparison.MAX_FINDINGS", 1):
            result = compare_pdf_bytes(self.before, self.after)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["findings"]), 1)

    def test_views_are_read_only_and_sources_open_exact_snapshot_bytes(self):
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        response = self.client.get(self.url)
        self.assertContains(response, "Build comparison")
        self.assertFalse(comparison_root().exists())
        response = self.client.post(self.url, {"baseline": "original", "next": "https://example.com/"})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.url)
        self.assertContains(response, "Needs a closer look")
        self.assertContains(response, "Browse all pages")
        self.assertNotIn("example.com", response.context["back_url"])
        payload = response.context["comparison_data"]
        original = self.client.get(payload["pdf_urls"]["before"])
        self.assertEqual(b"".join(original.streaming_content), self.before)
        original.close()
        page = self.client.get(payload["page_urls"]["after"].replace("/0.png", "/1.png"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "image/png")
        self.assertEqual(page["Cache-Control"], "no-store")
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

    def test_comparison_entry_is_shared_by_list_single_and_focus_modes(self):
        for params in ({"filter": "all"}, {"mode": "single", "filter": "all"}, {"mode": "focus", "submission": self.submission.pk}):
            with self.subTest(params=params):
                page = self.client.get(reverse("submissions:formatting"), params, follow=True)
                self.assertContains(page, f'href="{self.url}?next=')
                self.assertContains(page, 'target="_blank" rel="noopener">Compare PDF</a>')

    def test_warning_info_presentation_preserves_comparison_results_and_review_state(self):
        before, after = reflow_pair()
        self.submission.pdf_file.save("before-reflow.pdf", ContentFile(before))
        self.submission.formatted_pdf_file.save("after-reflow.pdf", ContentFile(after))
        state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        payload = create_pdf_comparison(self.submission.pk)
        response = self.client.get(self.url, {"comparison": payload["token"]})
        self.assertContains(response, 'data-comparison-level="warning"')
        self.assertContains(response, 'data-comparison-level="info"')
        self.assertContains(response, '<span class="cfm-comparison-level">Warning</span>')
        self.assertContains(response, '<span class="cfm-comparison-level">Info</span>')
        self.assertContains(response, 'class="badge text-bg-warning" data-finding-group-count="attention">1</span>')
        self.assertContains(response, 'class="badge text-bg-info" data-finding-group-count="sequence">1</span>')
        self.assertEqual(response.context["comparison_data"]["findings"], payload["result"]["findings"])
        self.assertEqual(response.context["comparison_data"]["page_map"], payload["result"]["page_map"])
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), state)

    def test_final_and_crosscheck_remain_byte_identical_after_comparison(self):
        from submissions.services.crosscheck import prepare_crosscheck_upload
        from submissions.services.reports import export_publication_package
        original, publication_after = reflow_pair(extra_pages=6)
        self.submission.pdf_file.save("reflow-original.pdf", ContentFile(original))
        self.submission.formatted_pdf_file.save("ready-eight-pages.pdf", ContentFile(publication_after))
        self.mark_submission_publication_ready(self.submission)
        before_state = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        readiness = publication_readiness_rows(strict_hash=True)
        result = create_pdf_comparison(self.submission.pk)
        self.assertEqual(result["result"]["sequence_count"], 1)
        with zipfile.ZipFile(export_publication_package()) as archive:
            name = next(name for name in archive.namelist() if name.startswith("PDF/"))
            self.assertEqual(archive.read(name), publication_after)
        result = prepare_crosscheck_upload("PDFCOMPARE")
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertEqual(archive.read("P001_PDFCOMPARE.pdf"), publication_after)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before_state)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)

    def test_backup_excludes_temporary_comparison_and_keeps_review_artifacts(self):
        from submissions.services.system_state import export_system_state
        payload = create_pdf_comparison(self.submission.pk)
        snapshot = export_system_state()
        with zipfile.ZipFile(snapshot["path"]) as archive:
            self.assertFalse(any("formatting_comparison_previews" in name for name in archive.namelist()))
        self.assertTrue((comparison_root() / payload["token"] / "after.pdf").exists())

    def test_cleanup_protects_open_comparison_and_clear_database_removes_it(self):
        from submissions.models import AppSetting
        from submissions.services.storage_inventory import build_storage_inventory
        payload = create_pdf_comparison(self.submission.pk)
        setting = AppSetting.load()
        setting.reports_folder = str(self.root / "data")
        setting.save()
        inventory = build_storage_inventory()
        self.assertFalse(any("formatting_comparison_previews" in str(row["path"]) for row in inventory["report_export_cleanup_candidates"]))
        setting.reports_folder = str(self.root / "reports")
        setting.save()
        self.client.post(reverse("submissions:clear_database"), {"confirmation": "CLEAR DATABASE"})
        self.assertFalse((comparison_root() / payload["token"]).exists())
