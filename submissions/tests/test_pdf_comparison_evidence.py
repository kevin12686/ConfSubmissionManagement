"""Generated PDFs test evidence failures without private papers or font-name rules."""
import io
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase
from pypdf import PdfReader
from pypdf.generic import DecodedStreamObject

from submissions.services.pdf_comparison import _extract, compare_pdf_bytes
from submissions.services.pdf_text_evidence import document_font_evidence, unicode_map
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
    def test_partial_mapping_flags_only_used_codes(self):
        self.assertFalse(document_font_evidence(coded_pdf("A", missing="z"))["1"]["fonts"][0]["reason"])
        self.assertTrue(document_font_evidence(coded_pdf("z", missing="z"))["1"]["fonts"][0]["reason"])

    def test_partial_mapping_does_not_assume_the_extractors_fallback_character(self):
        record = document_font_evidence(coded_pdf("Q!", missing="!"))["1"]["fonts"][0]
        self.assertTrue(record["all"])
        self.assertTrue(all(token["issues"] for token in _extract(coded_pdf("Q!", missing="!"))["tokens"]))

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
        self.assertFalse(document_font_evidence(data)["1"]["fonts"][0]["reason"])

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
        evidence = {"tables": {}, "fonts": {"1": {"fonts": [], "error": "Inspection failed"}}}
        with patch("submissions.services.pdf_comparison._pdf_evidence", return_value=evidence):
            r = compare_pdf_bytes(make_pdf(["Same text"]), make_pdf(["Same text"]))
        self.assertGreater(r["uncertain_count"], 0)

    def test_nested_form_resources_are_inspected(self):
        source = coded_pdf("Q!", missing="!")
        with fitz.open(stream=source, filetype="pdf") as original, fitz.open() as doc:
            page = doc.new_page()
            page.show_pdf_page(page.rect, original, 0)
            nested = doc.tobytes()
        self.assertTrue(any(f["reason"] for f in document_font_evidence(nested)["1"]["fonts"]))

    def test_cmap_ranges_arrays_and_limits(self):
        reader = PdfReader(io.BytesIO(make_pdf(["Test"])))
        stream = DecodedStreamObject()
        stream.set_data(b"1 begincodespacerange <00> <ff> endcodespacerange 2 beginbfrange <41><42><0041> <51><52>[<0051><0052>] endbfrange")
        mapping, _ = unicode_map(stream, reader)
        self.assertEqual(mapping, {b"A": "A", b"B": "B", b"Q": "Q", b"R": "R"})
        stream.set_data(b"1 begincodespacerange <00000000><ffffffff> endcodespacerange 1 beginbfrange <00000000><ffffffff><0041> endbfrange")
        with self.assertRaises(ValueError):
            unicode_map(stream, reader)
