"""Generated publisher-independent fixtures: no private papers or file-name rules."""
import subprocess
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase

from submissions.services.pdf_comparison import PDFComparisonError, _extract, compare_pdf_bytes
from submissions.tests.test_pdf_comparison import make_pdf, make_positioned_pdf


def column_pdf(*, offset=0, reverse=False, changed=False):
    with fitz.open() as doc:
        p = doc.new_page()
        columns = [(45, "The first study measured twelve independent outcomes.\nEach participant completed all assigned tasks.\nThe findings support further careful investigation."),
                   (310, "The second study used a different assessment method.\nThe resulting confidence interval was narrow.\nAll reported values remain available for inspection.")]
        for x, text in columns[::(-1 if reverse else 1)]:
            if changed and x == 310:
                text = text.replace("narrow", "wide")
            p.insert_textbox((x, 80 + (offset if x == 310 else 0), x + 230, 500), text, fontsize=10)
        return doc.tobytes()


def table_pdf(*, swap=False, offset=0):
    with fitz.open() as doc:
        p = doc.new_page()
        for x in (70, 250, 430):
            p.draw_line((x, 70 + offset), (x, 160 + offset))
        for y in (70, 115, 160):
            p.draw_line((70, y + offset), (430, y + offset))
        p.insert_text((90, 95 + offset), "Condition A")
        p.insert_text((270, 95 + offset), "Condition B")
        # Identical paint order but different cells: content must not disappear.
        p.insert_text((270 if swap else 90, 140 + offset), "12")
        p.insert_text((90 if swap else 270, 140 + offset), "21")
        return doc.tobytes()


class ContentComparisonTests(SimpleTestCase):
    def test_two_columns_shift_without_interleaving_or_highlights(self):
        for reverse in (False, True):
            result = compare_pdf_bytes(column_pdf(), column_pdf(offset=7, reverse=reverse))
            self.assertEqual(result["findings"], [])

    def test_changed_word_survives_column_reordering(self):
        result = compare_pdf_bytes(column_pdf(), column_pdf(offset=7, reverse=True, changed=True))
        text = " ".join(f["before_text"] + " " + f["after_text"] for f in result["findings"])
        self.assertIn("narrow", text)
        self.assertIn("wide", text)

    def test_same_table_moved_is_not_a_content_change(self):
        self.assertEqual(compare_pdf_bytes(table_pdf(), table_pdf(offset=70))["findings"], [])

    def test_table_cell_value_swap_is_not_cancelled_by_global_word_counts(self):
        result = compare_pdf_bytes(table_pdf(), table_pdf(swap=True, offset=30))
        self.assertTrue(result["findings"])
        self.assertTrue(any("21" in f["before_text"] + f["after_text"] for f in result["findings"]))

    def test_table_detection_does_not_touch_parent_rendering_settings(self):
        current = fitz.TOOLS.set_small_glyph_heights()
        with patch.object(fitz.Page, "find_tables", side_effect=AssertionError("Must run outside web process")):
            result = compare_pdf_bytes(table_pdf(), table_pdf(offset=30))
        self.assertEqual(result["findings"], [])
        self.assertEqual(fitz.TOOLS.set_small_glyph_heights(), current)

    def test_table_detection_timeout_cannot_be_presented_as_no_changes(self):
        with patch("submissions.services.pdf_comparison.subprocess.run", side_effect=subprocess.TimeoutExpired("table", 30)):
            with self.assertRaisesMessage(PDFComparisonError, "No complete comparison"):
                compare_pdf_bytes(table_pdf(), table_pdf(swap=True))

    def test_rotated_table_preserves_cells_and_highlight_coordinates(self):
        def rotate(data):
            with fitz.open(stream=data, filetype="pdf") as document:
                document[0].set_rotation(90)
                return document.tobytes()
        self.assertEqual(compare_pdf_bytes(table_pdf(), rotate(table_pdf()))["findings"], [])
        result = compare_pdf_bytes(rotate(table_pdf()), rotate(table_pdf(swap=True)))
        self.assertTrue(result["findings"])
        for finding in result["findings"]:
            for location in finding["before_locations"] + finding["after_locations"]:
                x0, y0, x1, y1 = location["box"]
                self.assertTrue(0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1)

    def test_punctuation_only_insertion_highlights_only_the_punctuation(self):
        before, after = make_pdf(["Alice Smith and Bob Jones"]), make_pdf(["Alice Smith* and Bob Jones"])
        r = compare_pdf_bytes(before, after)
        f, = r["findings"]
        self.assertEqual(f["after_text"], "*")
        self.assertEqual(f["before_locations"], [])
        token = next(t for t in _extract(after)["tokens"] if t["text"] == "*")
        self.assertEqual(f["after_locations"][0]["box"], token["box"])
        self.assertLess(token["box"][2] - token["box"][0], token["word_box"][2] - token["word_box"][0])

    def test_partial_word_edit_does_not_highlight_unchanged_prefix(self):
        f, = compare_pdf_bytes(make_pdf(["Alice Smithson"]), make_pdf(["Alice Smith"]))["findings"]
        self.assertEqual(f["before_text"], "son")
        self.assertEqual(f["after_locations"], [])
        self.assertIn("Smithson", f["before_context"])

    def test_deleted_occurrence_and_inserted_negation_are_both_kept(self):
        before = make_pdf(["Every subject agreed. Every subject agreed. Participation was mandatory."])
        after = make_pdf(["Every subject agreed. Participation was not mandatory."])
        findings = compare_pdf_bytes(before, after)["findings"]
        self.assertIn("Every subject agreed.", " ".join(f["before_text"] for f in findings))
        self.assertIn("not", " ".join(f["after_text"] for f in findings))

    def test_inserted_page_does_not_mark_shifted_body_as_changed(self):
        body = "The original research includes detailed findings and reproducible independent measurements."
        r = compare_pdf_bytes(make_pdf([body]), make_pdf(["New introductory material.", body]))
        self.assertEqual(len(r["findings"]), 1)
        self.assertEqual(r["findings"][0]["after_text"], "New introductory material.")
        self.assertEqual(r["findings"][0]["before_locations"], [])

    def test_removed_cover_pages_remain_visible(self):
        cover = "Responses to reviewers. These pages describe the changes."
        body = "The retained paper presents the independent study methods and detailed results."
        r = compare_pdf_bytes(make_pdf([cover, body]), make_pdf([body]))
        self.assertEqual(r["findings"][0]["before_text"], cover)
        self.assertEqual(r["findings"][0]["before_pages"], [1])
        self.assertEqual(r["findings"][0]["after_locations"], [])

    def test_pattern_changes_are_not_suppressed_by_reflow(self):
        r = compare_pdf_bytes(column_pdf(), make_pdf(["Error! Bookmark not defined.", "??"]))
        self.assertTrue(any(f["category"] == "reference" for f in r["findings"]))
        self.assertTrue(any(f["category"] == "placeholder" for f in r["findings"]))

    def test_display_cap_reports_total_not_just_stored_count(self):
        before = make_pdf(["First value 12. Second value 34. Third value 56."])
        after = make_pdf(["First value 21. Second value 43. Third value 65."])
        with patch("submissions.services.pdf_comparison.MAX_FINDINGS", 1):
            r = compare_pdf_bytes(before, after)
        self.assertEqual(r["shown_findings"], 1)
        self.assertEqual(r["total_findings"], 3)
        self.assertTrue(r["truncated"])

    def test_location_cap_counts_glyph_regions_not_whole_words(self):
        with patch("submissions.services.pdf_comparison.MAX_BOXES", 1):
            f, = compare_pdf_bytes(make_pdf([""]), make_pdf(["Added!"]))["findings"]
        self.assertEqual(len(f["after_locations"]), 1)
        self.assertTrue(f["locations_truncated"])

    def test_font_and_line_spacing_do_not_create_changes(self):
        before = make_positioned_pdf([[(80, "The unchanged text has all its words."), (110, "More unchanged content follows here.")]])
        after = make_positioned_pdf([[(160, "The unchanged text has all its words."), (210, "More unchanged content follows here.")]])
        self.assertEqual(compare_pdf_bytes(before, after)["findings"], [])

    def test_all_text_disappearing_still_highlights_the_baseline(self):
        r = compare_pdf_bytes(make_pdf(["All original text disappeared."]), make_pdf([""]))
        f, = r["findings"]
        self.assertEqual(f["before_text"], "All original text disappeared.")
        self.assertEqual(f["kind"], "removed")
        self.assertTrue(f["before_locations"])
        self.assertEqual(f["after_locations"], [])
        # Could also be a rasterized PDF; do not claim complete text comparison.
        self.assertFalse(r["text_available"])
        self.assertEqual(r["token_coverage"]["unmatched_before"], r["token_coverage"]["before_total"])

    def test_identical_bytes_with_equation_symbols_have_no_changes(self):
        data = make_pdf(["Result (x + 2) / 3 = 12; error probability: 0.05"])
        r = compare_pdf_bytes(data, data)
        self.assertTrue(r["identical_bytes"])
        self.assertEqual(r["findings"], [])

    def test_superscript_punctuation_is_separate_from_name(self):
        def pdf(marker):
            with fitz.open() as d:
                p = d.new_page()
                p.insert_text((70, 80), "Alice Smith", fontsize=12)
                if marker:
                    p.insert_text((132, 76), "*", fontsize=7)
                return d.tobytes()
        f, = compare_pdf_bytes(pdf(False), pdf(True))["findings"]
        self.assertEqual(f["after_text"], "*")
        self.assertEqual(f["before_locations"], [])
