"""Generated PDFs test evidence failures without private papers or font-name rules."""
import io
import copy
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase
from pypdf import PdfReader
from pypdf.generic import DecodedStreamObject

from submissions.services.pdf_comparison import _extract, _validate_evidence, compare_pdf_bytes
from submissions.services.pdf_text_alignment import align_tokens
from submissions.services.pdf_text_evidence import unicode_map
from submissions.tests.test_pdf_comparison import make_pdf, make_positioned_pdf


def coded_pdf(text, *, missing=(), replacements=None, malformed=False):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 80), text)
        font = page.get_fonts()[0][0]
        doc.xref_set_key(font, "Encoding", "null")
        mapping = {c: chr(c) for c in range(32, 127) if chr(c) not in missing}
        mapping.update(replacements or {})
        entries = "\n".join(f'<{code:02x}> <{value.encode("utf-16-be").hex()}>' for code, value in mapping.items())
        cmap = f"begincmap\n1 begincodespacerange\n<00> <ff>\nendcodespacerange\n{len(mapping)} beginbfchar\n{entries}\nendbfchar\nendcmap"
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, ("begincmap usecmap endcmap" if malformed else cmap).encode())
        doc.xref_set_key(font, "ToUnicode", f"{xref} 0 R")
        return doc.tobytes()


class PDFEvidenceTests(SimpleTestCase):
    def test_exact_page_source_pairs_only_unique_identical_drawing_occurrences(self):
        left, right = make_pdf(["Alpha Beta"]), make_pdf(["Alpha Beta"])
        original = [_extract(left), _extract(right)]
        for mutation in (None, "resource", "appearance", "position", "duplicate_region", "duplicate_page", "missing"):
            evidence = copy.deepcopy(original)
            region = {"page": 1, "box": [.2, .2, .3, .3], "issues": ["Unknown vector Unicode"],
                      "appearance_key": "a" * 64, "right_token_index": 1}
            for document in evidence:
                document["pages"][0]["render_source_key"] = "c" * 64
                document["uncertain_regions"] = [copy.deepcopy(region)]
                document["coverage_reasons"] = ["Unknown vector Unicode"]
            if mutation == "resource":
                evidence[1]["pages"][0]["render_source_key"] = "d" * 64
            elif mutation == "appearance":
                evidence[1]["uncertain_regions"][0]["appearance_key"] = "b" * 64
            elif mutation == "position":
                evidence[1]["uncertain_regions"][0]["box"][1] = .1
            elif mutation == "duplicate_region":
                evidence[1]["uncertain_regions"].append(copy.deepcopy(region))
            elif mutation == "duplicate_page":
                evidence[1]["pages"].append({**evidence[1]["pages"][0], "number": 2})
            elif mutation == "missing":
                del evidence[1]["pages"][0]["render_source_key"]
            opcodes, limited = align_tokens(evidence[0]["tokens"], evidence[1]["tokens"])
            evidence[0]["alignment"] = {"opcodes": opcodes, "limited": limited}
            with self.subTest(mutation=mutation), patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
                result = compare_pdf_bytes(left, right)
                self.assertEqual(result["uncertain_count"], 0 if mutation is None else 3 if mutation == "duplicate_region" else 2)
                self.assertEqual(result["coverage_status"], "limited")
                self.assertIn("Unknown vector Unicode", result["coverage_reasons"])

    def test_page_render_source_key_is_validated_at_worker_boundary(self):
        original = _extract(make_pdf(["Source proof validation"]))
        for value in ("", "z" * 64, "a" * 63, "a" * 65, 1, True, [], {}):
            document = copy.deepcopy(original)
            document["pages"][0]["render_source_key"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "render source"):
                _validate_evidence(document)

    def test_vector_pair_requires_exact_appearance_anchors_and_attachment(self):
        for mutation in (None, "appearance", "geometry", "duplicate"):
            left, right = make_pdf(["Alpha Beta old"]), make_pdf(["Alpha Beta new"])
            evidence = [_extract(left), _extract(right)]
            region = {"page": 1, "box": [.2, .2, .3, .3], "issues": ["Unknown vector Unicode"],
                "left_token_index": 0, "right_token_index": 1, "appearance_key": "a" * 64,
                "anchor_geometry": {"left": ["1", "-1", "2", "0"], "right": ["-2", "-1", "-1", "0"]}}
            for document in evidence:
                document["uncertain_regions"] = [copy.deepcopy(region)]
                document["coverage_reasons"] = []
            if mutation == "appearance":
                evidence[1]["uncertain_regions"][0]["appearance_key"] = "b" * 64
            elif mutation == "geometry":
                evidence[1]["uncertain_regions"][0]["anchor_geometry"]["left"][1] = "1"
            elif mutation == "duplicate":
                evidence[0]["uncertain_regions"].append(copy.deepcopy(region))
            opcodes, limited = align_tokens(evidence[0]["tokens"], evidence[1]["tokens"])
            evidence[0]["alignment"] = {"opcodes": opcodes, "limited": limited}
            with self.subTest(mutation=mutation), patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
                result = compare_pdf_bytes(left, right)
                self.assertEqual(result["uncertain_count"], 0 if mutation is None else 3 if mutation == "duplicate" else 1)
                self.assertEqual(result["coverage_status"], "limited")
                if mutation in {"appearance", "geometry"}:
                    warning = next(f for f in result["findings"] if f["category"] == "uncertain")
                    self.assertTrue(warning["before_locations"])
                    self.assertTrue(warning["after_locations"])
                    self.assertIn("not established", " ".join(warning["issues"]))
                changes = " ".join(f["before_text"] + f["after_text"] for f in result["findings"] if f["category"] == "text")
                self.assertIn("old", changes)
                self.assertIn("new", changes)

    def test_vector_counterpart_stays_uncertain_and_neighboring_real_change_survives(self):
        for reverse in (False, True):
            left, right = make_pdf(["Alpha Beta old"]), make_pdf(["Alpha { Beta new"])
            evidence = [_extract(left), _extract(right)]
            evidence[0]["uncertain_regions"] = [{"page": 1, "box": [.2, .2, .3, .3],
                "issues": ["Vector glyph cannot be identified uniquely."],
                "left_token_index": 0, "right_token_index": 1, "candidate_values": ["{"]}]
            if reverse:
                left, right = right, left
                evidence.reverse()
            opcodes, limited = align_tokens(evidence[0]["tokens"], evidence[1]["tokens"])
            evidence[0]["alignment"] = {"opcodes": opcodes, "limited": limited}
            with self.subTest(reverse=reverse), patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
                result = compare_pdf_bytes(left, right)
                self.assertEqual(result["uncertain_count"], 1)
                warning = next(f for f in result["findings"] if f["category"] == "uncertain")
                self.assertTrue(warning["before_locations"])
                self.assertTrue(warning["after_locations"])
                edits = " ".join(f["before_text"] + f["after_text"] for f in result["findings"] if f["category"] == "text")
                self.assertIn("old", edits)
                self.assertIn("new", edits)
                self.assertEqual(result["coverage_status"], "limited")

    def test_many_vectors_cannot_be_collapsed_onto_one_unmatched_character(self):
        left, right = make_pdf(["Alpha Beta"]), make_pdf(["Alpha { Beta"])
        evidence = [_extract(left), _extract(right)]
        region = {"page": 1, "box": [.2, .2, .3, .3], "issues": ["Unresolved"],
            "left_token_index": 0, "right_token_index": 1, "candidate_values": ["{"]}
        evidence[0]["uncertain_regions"] = [region, copy.deepcopy(region)]
        opcodes, limited = align_tokens(evidence[0]["tokens"], evidence[1]["tokens"])
        evidence[0]["alignment"] = {"opcodes": opcodes, "limited": limited}
        with patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
            result = compare_pdf_bytes(left, right)
        self.assertEqual(result["uncertain_count"], 2)
        self.assertTrue(any(f["after_text"] == "{" and f["category"] == "text" for f in result["findings"]))

    def test_unresolved_vector_is_located_without_hiding_real_text_deletion(self):
        before, after = make_pdf(["Deleted content. Stable content"]), make_pdf(["Stable content"])
        evidence = [_extract(before), _extract(after)]
        evidence[0]["uncertain_regions"] = [{"page": 1, "box": [.2, .2, .3, .3],
            "issues": ["Vector glyph cannot be identified uniquely."]}]
        opcodes, limited = align_tokens(evidence[0]["tokens"], evidence[1]["tokens"])
        evidence[0]["alignment"] = {"opcodes": opcodes, "limited": limited}
        with patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
            result = compare_pdf_bytes(before, after)
        self.assertEqual(result["uncertain_count"], 1)
        self.assertEqual(result["coverage_status"], "limited")
        self.assertIn("Deleted content", " ".join(f["before_text"] for f in result["findings"] if f["category"] == "text"))
        region = next(f for f in result["findings"] if f["category"] == "uncertain")
        self.assertEqual(region["before_locations"], [{"page": 1, "box": [.2, .2, .3, .3]}])
        self.assertFalse(region["after_locations"])

    def test_worker_evidence_contract_rejects_incomplete_or_nonfinite_locations(self):
        token = {"text": "x", "page": 1, "box": [.1, .1, .2, .2],
                 "glyphs": [[.1, .1, .2, .2]], "word_start": 0, "word_end": 1, "issues": []}
        valid = {"words": [token], "tokens": [token], "controls": [],
                 "pages": [{"number": 1, "width": 600, "height": 800}]}
        _validate_evidence(valid)
        invalid = copy.deepcopy(valid)
        invalid["uncertain_regions"] = [{"page": 1, "box": [0, 0, float("nan"), 1], "issues": ["Unresolved"]}]
        with self.assertRaises(ValueError):
            _validate_evidence(invalid)
        for field, value in (("glyphs", []), ("glyphs", [[0, 0, float("nan"), 1]]),
                             ("page", 2), ("word_end", 2), ("box", [0, 0, 2, 1]),
                             ("char_meta", [None])):
            invalid = copy.deepcopy(valid)
            invalid["tokens"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _validate_evidence(invalid)

    def test_partial_mapping_flags_only_used_codes(self):
        self.assertFalse(_extract(coded_pdf("A", missing="z"))["tokens"][0]["issues"])
        self.assertTrue(_extract(coded_pdf("z", missing="z"))["tokens"][0]["issues"])

    def test_partial_mapping_localizes_uncertainty_without_guessing_fallback(self):
        tokens = _extract(coded_pdf("Q!", missing="!"))["tokens"]
        self.assertTrue(any(token["issues"] for token in tokens))
        self.assertTrue(any(token["text"] == "Q" and not token["issues"] for token in tokens))

    def test_regular_standard_font_without_tounicode_is_not_uncertain(self):
        self.assertEqual(compare_pdf_bytes(make_pdf(["A useful result"]), make_pdf(["A useful outcome"]))["uncertain_count"], 0)

    def test_empty_space_rounding_at_different_positions_is_not_drawn_ink(self):
        positions = (30, 65, 200, 400, 800)
        before = make_positioned_pdf([[(y, "Conference 2025") for y in positions]])
        after = make_positioned_pdf([[(y, "Conference 2026") for y in positions]])
        result = compare_pdf_bytes(before, after)
        self.assertEqual(result["uncertain_count"], 0)
        self.assertEqual(sum(f["before_text"].count("2025") for f in result["findings"]), len(positions))
        self.assertEqual(sum(f["after_text"].count("2026") for f in result["findings"]), len(positions))

    def test_simple_font_character_width_is_not_taken_from_cmap_codespace(self):
        with fitz.open(stream=coded_pdf("Actual text"), filetype="pdf") as doc:
            ref = int(doc.xref_get_key(doc[0].get_fonts()[0][0], "ToUnicode")[1].split()[0])
            doc.update_stream(ref, doc.xref_stream(ref).replace(b"<00> <ff>", b"<0000> <ffff>"))
            data = doc.tobytes()
        self.assertTrue(all(not token["issues"] for token in _extract(data)["tokens"]))

    def test_same_extracted_letters_with_incomplete_mapping_are_not_silently_equal(self):
        r = compare_pdf_bytes(coded_pdf("Q!", missing="!"), coded_pdf("Q!", missing="!"))
        self.assertGreater(r["uncertain_count"], 0)
        self.assertGreater(r["total_attention_count"], 0)

    def test_byte_identical_pdf_is_not_reported_as_changed(self):
        data = coded_pdf("Q!", missing="!")
        self.assertEqual(compare_pdf_bytes(data, data)["findings"], [])

    def test_unreliable_formula_and_real_removed_words_are_both_retained(self):
        r = compare_pdf_bytes(coded_pdf("Deleted words. Result Q!", missing="!"), coded_pdf("Result Q!", missing="!"))
        self.assertIn("Deleted words", " ".join(f["before_text"] for f in r["findings"]))
        self.assertGreater(r["uncertain_count"], 0)

    def test_formula_completely_removed_is_flagged_even_when_unreadable(self):
        r = compare_pdf_bytes(coded_pdf("Q! + 12", missing="!"), make_pdf([""]))
        self.assertGreater(r["uncertain_count"], 0)
        self.assertTrue(r["findings"][0]["before_locations"])
        self.assertEqual(r["findings"][0]["after_locations"], [])

    def test_valid_operator_and_numeric_changes_still_have_content_highlights(self):
        r = compare_pdf_bytes(coded_pdf("x + 12"), coded_pdf("x - 21"))
        self.assertEqual(r["uncertain_count"], 0)
        text = " ".join(f["before_text"] + f["after_text"] for f in r["findings"])
        for value in ("+", "-", "12", "21"):
            self.assertIn(value, text)

    def test_non_drawing_zwsp_is_recorded_but_not_a_deleted_word(self):
        r = compare_pdf_bytes(coded_pdf("v1 :", replacements={32: "\u200b"}), coded_pdf("v1:"))
        self.assertEqual(r["total_attention_count"], 0)
        self.assertEqual(r["normalized_controls"], {"before": 1, "after": 0})
        self.assertEqual(r["findings"][0]["category"], "formatting")
        self.assertIn("\u200b", r["findings"][0]["before_raw"])

    def test_zwsp_normalization_does_not_swallow_a_real_word_deletion(self):
        r = compare_pdf_bytes(coded_pdf("old :", replacements={32: "\u200b"}), coded_pdf(":"))
        self.assertIn("old", " ".join(f["before_text"] for f in r["findings"] if f["category"] == "text"))
        self.assertEqual(r["info_count"], 1)

    def test_glyph_mapped_to_zwsp_but_still_drawn_is_not_normalized(self):
        r = compare_pdf_bytes(coded_pdf("A", replacements={65: "\u200b"}), make_pdf([""]))
        self.assertEqual(r["normalized_controls"]["before"], 0)
        self.assertGreater(r["uncertain_count"], 0)

    def test_drawing_glyph_mapped_to_space_is_not_dropped(self):
        r = compare_pdf_bytes(coded_pdf("A", replacements={65: " "}), make_pdf([""]))
        self.assertGreater(r["uncertain_count"], 0)
        self.assertTrue(r["findings"][0]["before_locations"])

    def test_other_format_characters_are_not_blanket_deleted(self):
        for character in ("\u200c", "\u200d", "\u202e", "\ufe0f", "\u00ad"):
            r = compare_pdf_bytes(coded_pdf("v1 :", replacements={32: character}), coded_pdf("v1:"))
            self.assertEqual(r["normalized_controls"]["before"], 0)
            self.assertGreater(r["total_attention_count"], 0)

    def test_malformed_mapping_never_returns_a_clean_comparison(self):
        r = compare_pdf_bytes(coded_pdf("Q", malformed=True), coded_pdf("Q"))
        self.assertGreater(r["uncertain_count"], 0)

    def test_page_inspection_error_is_visible_and_does_not_use_equal_text_to_hide_it(self):
        # The isolated worker now returns occurrence-level pair evidence.
        evidence = [_extract(make_pdf(["Same text"])) for _ in range(2)]
        for document in evidence:
            for item in document["tokens"] + document["words"]:
                item["issues"] = ["Page inspection failed"]
                item["text_identity_supported"] = False
            document["coverage_reasons"] = ["Page inspection failed"]
        evidence[0]["alignment"] = {"opcodes": [["replace", 0, len(evidence[0]["tokens"]), 0, len(evidence[1]["tokens"])]], "limited": False}
        with patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
            r = compare_pdf_bytes(make_pdf(["Same text"]), make_pdf(["Same text"]))
        self.assertGreater(r["uncertain_count"], 0)
        self.assertEqual(r["coverage_status"], "limited")

    def test_nested_form_resources_are_inspected(self):
        source = coded_pdf("Q!", missing="!")
        with fitz.open(stream=source, filetype="pdf") as original, fitz.open() as doc:
            page = doc.new_page()
            page.show_pdf_page(page.rect, original, 0)
            nested = doc.tobytes()
        tokens = _extract(nested)["tokens"]
        self.assertTrue(any(token["issues"] for token in tokens))
        self.assertTrue(any(token["text"] == "Q" and not token["issues"] for token in tokens))

    def test_cmap_ranges_arrays_and_limits(self):
        reader = PdfReader(io.BytesIO(make_pdf(["Test"])))
        stream = DecodedStreamObject()
        stream.set_data(b"1 begincodespacerange <00> <ff> endcodespacerange 2 beginbfrange <41><42><0041> <51><52>[<0051><0052>] endbfrange")
        mapping, _ = unicode_map(stream, reader)
        self.assertEqual(mapping, {b"A": "A", b"B": "B", b"Q": "Q", b"R": "R"})
        stream.set_data(b"1 begincodespacerange <00000000><ffffffff> endcodespacerange 1 beginbfrange <00000000><ffffffff><0041> endbfrange")
        with self.assertRaises(ValueError):
            unicode_map(stream, reader)
