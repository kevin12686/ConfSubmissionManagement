"""Real-worker precision regressions using only generated PDF/font bytes."""

import fitz
from django.test import SimpleTestCase

from submissions.services.pdf_comparison import PDFComparisonError, compare_pdf_bytes
from submissions.tests.test_pdf_glyph_evidence import font_bytes


PAGE_SIZE = (400, 300)
TABLE_X = (45, 89, 133, 177)
TABLE_TOP = 60
ROW_HEIGHT = 14
ROWS = (("ALPHA", "10", "31"), ("BRAVO", "40", "57"), ("DELTA", "60", "82"))


def _table_pdf(rows=ROWS, *, filled=True, font_size=10, body_y=230, operator="+", bars=True,
               vector_bar=False, cell_bar=False):
    with fitz.open() as document:
        page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        page.insert_text((45, 35), "Experiment settings", fontsize=11)
        grid = (("Group", "Step", "Score"), *rows)
        bottom = TABLE_TOP + ROW_HEIGHT * len(grid)
        for row in range(len(grid) + 1):
            y = TABLE_TOP + ROW_HEIGHT * row
            page.draw_line((TABLE_X[0], y), (TABLE_X[-1], y), width=.7)
        for x in TABLE_X:
            if not filled:
                page.draw_line((x, TABLE_TOP), (x, bottom), width=.7)
                continue
            for row in range(len(grid)):
                top = TABLE_TOP + ROW_HEIGHT * row
                end = top + ROW_HEIGHT
                # Exporters can encode a filled rule as several collinear edges.
                shape = page.new_shape()
                shape.draw_polyline([
                    (x - .35, top), (x + .35, top), (x + .35, (top + end) / 2),
                    (x + .35, end), (x - .35, end), (x - .35, (top + end) / 2),
                ])
                shape.finish(closePath=True, color=None, fill=(0, 0, 0))
                shape.commit()
        for row, values in enumerate(grid):
            for column, value in enumerate(values):
                if value:
                    page.insert_text((TABLE_X[column] + 5, TABLE_TOP + ROW_HEIGHT * row + 10),
                                     value, fontsize=font_size)
        if cell_bar:
            shape = page.new_shape()
            shape.draw_polyline([(154.825, 78), (155.175, 78), (155.175, 81.5),
                                 (155.175, 85), (154.825, 85), (154.825, 81.5)])
            shape.finish(closePath=True, color=None, fill=(0, 0, 0))
            shape.commit()
        page.insert_font(fontname="barfont", fontbuffer=font_bytes(features=False))
        page.insert_text((45, 170), "norm", fontsize=10)
        if bars:
            page.insert_text((73, 170), "|", fontname="barfont", fontsize=10)
            page.insert_text((85, 170), "|", fontname="barfont", fontsize=10)
        page.insert_text((77, 170), "v", fontsize=10)
        page.insert_text((95, 170), "= p", fontsize=10)
        if operator:
            page.insert_text((123, 170), operator, fontsize=10)
        page.insert_text((137, 170), "q", fontsize=10)
        page.insert_text((45, 195), "A", fontsize=10)
        page.insert_text((65, 195), "B", fontsize=10)
        if vector_bar:
            shape = page.new_shape()
            shape.draw_polyline([(55.725, 184), (56.275, 184), (56.275, 189.5),
                                 (56.275, 195), (55.725, 195), (55.725, 189.5)])
            shape.finish(closePath=True, color=None, fill=(0, 0, 0))
            shape.commit()
        page.insert_text((45, body_y), "Independent measurements were retained.", fontsize=10)
        return document.tobytes()


def _metadata_rewrite(data):
    with fitz.open(stream=data, filetype="pdf") as document:
        document.set_metadata({**document.metadata, "title": "Precision control", "subject": "Metadata only"})
        return document.tobytes(garbage=4, deflate=True)


def _blank_corner_cell(row, column):
    return (40 + 64 * column, 50 + 24 * row, 104 + 64 * column, 74 + 24 * row)


def _blank_corner_table_pdf(edits=None):
    grid = [["", "Train", "Test", "Score", "Count"]]
    grid.extend([[f"Data{row}", *(str(10 * row + column) for column in range(4))]
                 for row in range(1, 7)])
    for (row, column), value in (edits or {}).items():
        grid[row][column] = value
    with fitz.open() as document:
        page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        page.insert_text((40, 30), "Benchmark measurements", fontsize=11)
        for row in range(8):
            page.draw_line((40, 50 + 24 * row), (360, 50 + 24 * row), width=.7)
        for column in range(6):
            page.draw_line((40 + 64 * column, 50), (40 + 64 * column, 218), width=.7)
        for row, values in enumerate(grid):
            for column, value in enumerate(values):
                if value:
                    x, y, _, _ = _blank_corner_cell(row, column)
                    page.insert_text((x + 5, y + 16), value, fontsize=10)
        return document.tobytes()


def _script_pdf(*, base_size=9.8, script_y=102):
    with fitz.open() as document:
        page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        page.insert_font(fontname="formula", fontbuffer=font_bytes())
        page.insert_text((60, 100), "A", fontname="formula", fontsize=base_size)
        page.insert_text((65.2, script_y), "fiB", fontname="formula", fontsize=7)
        page.insert_text((90, 100), "= 23", fontsize=10)
        return document.tobytes()


def _actual_text_pdf(painted="dose 23", actual=None):
    with fitz.open() as document:
        page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        page.insert_text((60, 100), painted, fontsize=12)
        if actual is not None:
            xref, = page.get_contents()
            replacement = ("feff" + actual.encode("utf-16-be").hex()).encode("ascii")
            document.update_stream(xref, b"/Span << /ActualText <" + replacement + b"> >> BDC\n"
                                   + document.xref_stream(xref) + b"\nEMC")
        return document.tobytes()


def _duplicate_blend_pdf(blend, font):
    with fitz.open() as document:
        page = document.new_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
        page.insert_font(fontname="custom", fontbuffer=font)
        page.insert_text((40, 100), "B", fontname="custom", fontsize=16)
        page.insert_text((250, 200), "B|B", fontname="custom", fontsize=16)
        extra = document.get_new_xref()
        document.update_object(extra, "<<>>")
        document.update_stream(extra,
            b"0 1 1 rg 50 198 5 15 re f q /GS1 gs 1 0 0 rg "
            b"52 200 m 52.5 200 l 52.5 205 l 52.5 210 l 52 210 l 52 205 l h f Q")
        contents = " ".join(f"{index} 0 R" for index in page.get_contents())
        document.xref_set_key(page.xref, "Contents", f"[{contents} {extra} 0 R]")
        resource = int(document.xref_get_key(page.xref, "Resources")[1].split()[0])
        # Serialize raw objects so a writer cannot repair the deliberate duplicate key.
        data, offsets = b"%PDF-1.7\n", [0]
        for index in range(1, document.xref_length()):
            offsets.append(len(data))
            obj = document.xref_object(index).encode("ascii")
            if index == resource:
                obj = obj[:-2] + b"/ExtGState << /GS1 << /BM /Normal /BM /" + blend.encode("ascii") + b" >> >> >>"
            if document.xref_is_stream(index):
                obj += b"\nstream\n" + document.xref_stream_raw(index) + b"\nendstream"
            data += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
        start, count = len(data), len(offsets)
        data += f"xref\n0 {count}\n0000000000 65535 f \n".encode("ascii")
        data += b"".join(f"{offset:010} 00000 n \n".encode("ascii") for offset in offsets[1:])
        return data + (f"trailer\n<< /Size {count} /Root {document.pdf_catalog()} 0 R >>\n"
                       f"startxref\n{start}\n%%EOF\n").encode("ascii")


def _cell(row, column):
    top = TABLE_TOP + ROW_HEIGHT * (row + 1)
    return (TABLE_X[column], top, TABLE_X[column + 1], top + ROW_HEIGHT)


class PDFComparisonPrecisionTests(SimpleTestCase):
    def assert_accounted(self, result):
        coverage = result["token_coverage"]
        for side in ("before", "after"):
            self.assertEqual(coverage["aligned"] + coverage["paired"] + coverage[f"unmatched_{side}"],
                             coverage[f"{side}_total"])
        self.assertFalse(result["truncated"])
        self.assertFalse(result["pairing_limited"])

    def assert_unchanged(self, before, after):
        self.assertNotEqual(before, after)
        result = compare_pdf_bytes(before, after)
        self.assert_accounted(result)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["token_coverage"]["unmatched_before"], 0)
        self.assertEqual(result["token_coverage"]["unmatched_after"], 0)
        self.assertGreater(result["token_coverage"]["before_total"], 0)
        return result

    def assert_marked(self, result, side, text, rectangle, *, category=None):
        matches = [finding for finding in result["findings"]
                   if text in finding[f"{side}_text"] and (category is None or finding["category"] == category)]
        self.assertTrue(matches, (side, text, result["findings"]))
        x0, y0, x1, y1 = rectangle
        marks = [location for finding in matches for location in finding[f"{side}_locations"]]
        self.assertTrue(marks, (side, text))
        for location in marks:
            self.assertEqual(location["page"], 1)
            a, b, c, d = location["box"]
            self.assertTrue(0 <= a < c <= 1 and 0 <= b < d <= 1)
        self.assertTrue(any(x0 < (mark["box"][0] + mark["box"][2]) * PAGE_SIZE[0] / 2 < x1
                            and y0 < (mark["box"][1] + mark["box"][3]) * PAGE_SIZE[1] / 2 < y1
                            for mark in marks), (side, text, rectangle, marks))

    def test_filled_table_borders_survive_metadata_only_rewrite(self):
        before = _table_pdf()
        self.assert_unchanged(before, _metadata_rewrite(before))

    def test_filled_and_stroked_table_borders_do_not_invent_operators(self):
        self.assert_unchanged(_table_pdf(filled=True), _table_pdf(filled=False))

    def test_filled_table_borders_survive_small_text_font_change(self):
        self.assert_unchanged(_table_pdf(font_size=10), _table_pdf(font_size=10.2))

    def test_duplicate_first_column_table_survives_metadata_only_rewrite(self):
        before = _table_pdf((("ALPHA", "10", "31"), ("ALPHA", "40", "57"), ("DELTA", "60", "82")), filled=False)
        self.assert_unchanged(before, _metadata_rewrite(before))

    def test_blank_corner_table_survives_metadata_only_rewrite(self):
        before = _blank_corner_table_pdf()
        after = _metadata_rewrite(before)
        with fitz.open(stream=before, filetype="pdf") as a, fitz.open(stream=after, filetype="pdf") as b:
            table, = a[0].find_tables().tables
            self.assertEqual((table.row_count, table.col_count), (7, 5))
            cells = table.extract()
            self.assertEqual(cells[0], ["", "Train", "Test", "Score", "Count"])
            self.assertEqual([row[0] for row in cells[1:]], [f"Data{row}" for row in range(1, 7)])
            self.assertEqual(a[0].get_pixmap().samples, b[0].get_pixmap().samples)
        self.assert_unchanged(before, after)

    def test_blank_corner_table_numeric_edit_keeps_both_cell_marks(self):
        result = compare_pdf_bytes(_blank_corner_table_pdf(), _blank_corner_table_pdf({(1, 1): "20"}))
        self.assert_accounted(result)
        self.assert_marked(result, "before", "10", _blank_corner_cell(1, 1), category="text")
        self.assert_marked(result, "after", "20", _blank_corner_cell(1, 1), category="text")

    def test_blank_corner_table_added_and_deleted_value_keeps_its_cell(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _blank_corner_table_pdf({(3, 2): ""}), _blank_corner_table_pdf()
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                side, absent = ("before", "after") if reverse else ("after", "before")
                self.assert_marked(result, side, "31", _blank_corner_cell(3, 2), category="text")
                self.assertFalse(any("31" in finding[f"{absent}_text"] for finding in result["findings"]))

    def test_blank_corner_table_cannot_hide_values_swapped_between_rows(self):
        result = compare_pdf_bytes(_blank_corner_table_pdf(),
                                   _blank_corner_table_pdf({(1, 1): "20", (2, 1): "10"}))
        self.assert_accounted(result)
        for side, values in (("before", ("10", "20")), ("after", ("20", "10"))):
            for row, value in enumerate(values, 1):
                self.assert_marked(result, side, value, _blank_corner_cell(row, 1), category="text")

    def test_blank_corner_placeholder_does_not_hide_added_or_deleted_corner_text(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _blank_corner_table_pdf(), _blank_corner_table_pdf({(0, 0): "Dataset"})
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                self.assert_marked(result, "before" if reverse else "after", "Dataset", _blank_corner_cell(0, 0))

    def test_blank_corner_does_not_hide_other_header_or_row_label_edits(self):
        for cell, original, replacement in (((0, 1), "Train", ""), ((0, 1), "Train", "Learn"),
                                             ((3, 0), "Data3", ""), ((3, 0), "Data3", "Data9")):
            with self.subTest(cell=cell, replacement=replacement):
                result = compare_pdf_bytes(_blank_corner_table_pdf(), _blank_corner_table_pdf({cell: replacement}))
                self.assert_accounted(result)
                self.assert_marked(result, "before", original, _blank_corner_cell(*cell))
                if replacement:
                    self.assert_marked(result, "after", replacement, _blank_corner_cell(*cell))
                self.assertFalse(any(original in finding["after_text"] for finding in result["findings"]))

    def test_identical_body_moving_beside_filled_grid_is_not_changed_text(self):
        self.assert_unchanged(_table_pdf(body_y=230), _table_pdf(body_y=260))

    def test_numeric_cell_change_keeps_both_values_and_their_cell_marks(self):
        after_rows = (("ALPHA", "20", "31"), *ROWS[1:])
        result = compare_pdf_bytes(_table_pdf(), _table_pdf(after_rows))
        self.assert_accounted(result)
        self.assert_marked(result, "before", "10", _cell(0, 1))
        self.assert_marked(result, "after", "20", _cell(0, 1))

    def test_added_and_deleted_cell_value_keeps_its_own_location(self):
        empty = (("ALPHA", "10", ""), *ROWS[1:])
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _table_pdf(empty), _table_pdf()
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                side = "before" if reverse else "after"
                self.assert_marked(result, side, "31", _cell(0, 2))
                self.assertFalse(any("31" in f[f"{'after' if reverse else 'before'}_text"]
                                     for f in result["findings"]))

    def test_duplicate_row_labels_cannot_hide_value_swaps(self):
        before_rows = (("ALPHA", "10", "31"), ("ALPHA", "40", "57"), ROWS[2])
        after_rows = (("ALPHA", "10", "57"), ("ALPHA", "40", "31"), ROWS[2])
        result = compare_pdf_bytes(_table_pdf(before_rows), _table_pdf(after_rows))
        self.assert_accounted(result)
        for side, values in (("before", ("31", "57")), ("after", ("57", "31"))):
            for row, value in enumerate(values):
                self.assert_marked(result, side, value, _cell(row, 2))

    def test_plus_to_minus_beside_grid_remains_an_operator_change(self):
        result = compare_pdf_bytes(_table_pdf(operator="+"), _table_pdf(operator="-"))
        self.assert_accounted(result)
        self.assert_marked(result, "before", "+", (121, 159, 133, 174))
        self.assert_marked(result, "after", "-", (121, 159, 133, 174))

    def test_added_and_deleted_operator_beside_grid_remains_visible(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _table_pdf(operator=""), _table_pdf(operator="+")
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                self.assert_marked(result, "before" if reverse else "after", "+", (121, 159, 133, 174))

    def test_added_and_deleted_vertical_bars_are_not_table_borders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _table_pdf(bars=False), _table_pdf(bars=True)
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                side = "before" if reverse else "after"
                for x in (73, 85):
                    self.assert_marked(result, side, "|", (x - 1, 162, x + 2, 171))

    def test_added_and_deleted_vector_bar_outside_grid_keeps_its_region(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _table_pdf(), _table_pdf(vector_bar=True)
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                self.assertEqual(result["coverage_status"], "limited")
                self.assert_marked(result, "before" if reverse else "after", "", (54, 183, 58, 196), category="uncertain")

    def test_short_vector_bar_inside_cell_is_not_a_grid_edge(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                before, after = _table_pdf(), _table_pdf(cell_bar=True)
                if reverse:
                    before, after = after, before
                result = compare_pdf_bytes(before, after)
                self.assert_accounted(result)
                self.assert_marked(result, "before" if reverse else "after", "", (153, 77, 157, 86), category="uncertain")

    def test_multicharacter_subscript_survives_base_font_perturbation(self):
        self.assert_unchanged(_script_pdf(base_size=9.8), _script_pdf(base_size=10.2))

    def test_genuine_multicharacter_attachment_change_marks_the_script(self):
        for after_y in (98, 100):
            with self.subTest(after_y=after_y):
                result = compare_pdf_bytes(_script_pdf(), _script_pdf(base_size=10.2, script_y=after_y))
                self.assert_accounted(result)
                for side in ("before", "after"):
                    self.assert_marked(result, side, "fiB", (64, 90, 83, 108), category="structure")

    def test_actualtext_only_change_does_not_invent_painted_text_edit(self):
        result = self.assert_unchanged(_actual_text_pdf(), _actual_text_pdf(actual="dose 99"))
        self.assertEqual(result["evidence_summary"]["before"]["actual_text_spans"], 0)
        self.assertEqual(result["evidence_summary"]["after"]["actual_text_spans"], 1)

    def test_matching_actualtext_survives_metadata_only_rewrite(self):
        before = _actual_text_pdf(actual="dose 23")
        self.assert_unchanged(before, _metadata_rewrite(before))

    def test_actualtext_cannot_hide_a_changed_painted_number(self):
        result = compare_pdf_bytes(_actual_text_pdf(), _actual_text_pdf(painted="dose 99", actual="dose 23"))
        self.assert_accounted(result)
        self.assert_marked(result, "before", "23", (60, 85, 110, 104))
        self.assert_marked(result, "after", "99", (60, 85, 110, 104))

    def test_duplicate_resource_keys_cannot_prove_unchanged_vector(self):
        font = font_bytes(features=False)
        before, after = _duplicate_blend_pdf("Normal", font), _duplicate_blend_pdf("Multiply", font)
        with fitz.open(stream=before, filetype="pdf") as a, fitz.open(stream=after, filetype="pdf") as b:
            self.assertNotEqual(a[0].get_pixmap().samples, b[0].get_pixmap().samples)
        try:
            result = compare_pdf_bytes(before, after)
        except PDFComparisonError:
            return  # Refusing parser-ambiguous input is also fail-closed.
        self.assert_accounted(result)
        for side in ("before", "after"):
            self.assertEqual(result["evidence_summary"][side].get("exact_render_source_region_pairs", 0), 0)
            self.assert_marked(result, side, "", (50, 89, 54, 101), category="uncertain")
