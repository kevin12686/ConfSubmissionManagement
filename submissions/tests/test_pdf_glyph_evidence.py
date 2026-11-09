"""Synthetic evidence tests: no private documents, installed fonts or Django IO."""

import io
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import fitz
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, TTCollection
from pypdf import PdfReader

from submissions.services.pdf_glyph_evidence import GlyphEvidenceError, inspect_pdf_pair


def font_bytes(*, cmap=True, features=True, variant=0, family="EvidenceFont"):
    names = [".notdef", "space", "A", "B", "f", "i", "fi", "ff", "A.script", "arrow", "bar"]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({32: "space", 65: "A", 66: "B", 102: "f", 105: "i",
                               0xE000: "fi", 0xE001: "ff", 0xE002: "A.script", 0x2192: "arrow", 124: "bar"})
    glyphs = {}
    for index, name in enumerate(names):
        pen = TTGlyphPen(None)
        if name == "bar":
            pen.moveTo((0, 0))
            pen.lineTo((30, 0))
            pen.lineTo((30, 600))
            pen.lineTo((0, 600))
            pen.closePath()
        elif name != "space":
            width = 200 + 25 * index + variant
            pen.moveTo((0, 0))
            pen.lineTo((width, 0))
            pen.lineTo((width + 100, 300 + 10 * index))
            pen.lineTo((50, 600))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (700, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": family, "styleName": "Regular", "uniqueFontIdentifier": family,
                            "fullName": family, "psName": family})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    if features:
        addOpenTypeFeaturesFromString(builder.font,
            "feature ssty { sub A by A.script; } ssty; feature liga { sub f i by fi; sub f f by ff; } liga;")
    if not cmap:
        del builder.font["cmap"]
    buffer = io.BytesIO()
    builder.font.save(buffer)
    return buffer.getvalue()


def _mapping(document, xref, mapping, width=1, *, malformed=False, padded=False):
    data = "1 begincodespacerange <" + "00" * width + "><" + "ff" * width + "> endcodespacerange\n"
    data += f"{len(mapping)} beginbfchar\n"
    for code, value in mapping.items():
        data += f"<{code:0{(2 if padded else width) * 2}x}><{value.encode('utf-16-be').hex()}>\n"
    data += "endbfchar"
    reference = document.get_new_xref()
    document.update_object(reference, "<<>>")
    document.update_stream(reference, b"begincmap usecmap endcmap" if malformed else data.encode())
    document.xref_set_key(xref, "ToUnicode", f"{reference} 0 R")


def pdf(text="AB", *, font=None, mappings=None, missing=(), malformed=False, padded=False,
        render_mode=0, overprints=1, rotation=0, positions=None, drawing=None):
    with fitz.open() as document:
        page = document.new_page(width=400, height=300)
        fontname = "custom" if font else "helv"
        if font:
            page.insert_font(fontname=fontname, fontbuffer=font)
        for _ in range(overprints):
            for x, y, value, size in positions or [(40, 70, text, 16)]:
                page.insert_text((x, y), value, fontname=fontname, fontsize=size, render_mode=render_mode)
        if mappings is not None or missing or malformed or padded:
            if font:
                mapping = {1: " ", 2: "A", 3: "B", 4: "f", 5: "i", 6: "fi", 7: "ff", 8: "A", 9: "\u2192", 10: "|"}
            else:
                mapping = {c: chr(c) for c in range(32, 127)}
            mapping.update(mappings or {})
            for code in missing:
                mapping.pop(code, None)
            _mapping(document, page.get_fonts()[0][0], mapping, 2 if font else 1, malformed=malformed, padded=padded)
        if drawing:
            drawing(page)
        page.set_rotation(rotation)
        return document.tobytes()


def inspect(data, other=None):
    return inspect_pdf_pair(data, data if other is None else other)[0]


def replace_embedded_font(data, payload):
    reader = PdfReader(io.BytesIO(data))
    font = next(iter(reader.pages[0]["/Resources"]["/Font"].values())).get_object()
    descriptor = font["/DescendantFonts"][0].get_object()["/FontDescriptor"]
    reference = descriptor.raw_get("/FontFile2").idnum
    with fitz.open(stream=data, filetype="pdf") as document:
        document.update_stream(reference, payload)
        return document.tobytes()


def actual_text(data, value):
    with fitz.open(stream=data, filetype="pdf") as document:
        encoded = (b"\xfe\xff" + value.encode("utf-16-be")).hex().encode("ascii")
        for page in document:
            for xref in page.get_contents():
                document.update_stream(xref, b"/Span << /ActualText <" + encoded + b"> >> BDC\n" +
                                       document.xref_stream(xref) + b"\nEMC")
        return document.tobytes()


class GlyphEvidenceTests(TestCase):
    def assert_accounted(self, result):
        counts = result["coverage"]
        self.assertEqual(counts["glyphs"], counts["accounted_glyphs"])
        self.assertEqual(counts["glyphs"], counts["token_glyphs"] + counts["whitespace_glyphs"] + counts["normalized_controls"])
        for token in result["tokens"]:
            self.assertEqual(len(token["text"]), len(token["glyphs"]))
            self.assertEqual(len(token["text"]), len(token["char_meta"]))
        json.dumps(result, allow_nan=False)

    def test_standard_text_is_clean_and_compact(self):
        result = inspect(pdf("A useful result"))
        self.assertEqual([t["text"] for t in result["tokens"]], ["A", "useful", "result"])
        self.assertFalse(result["coverage_reasons"])
        self.assertNotIn("raw", result["tokens"][0]["char_meta"][0])
        self.assert_accounted(result)

    def test_partial_map_warning_is_local_even_for_identical_bytes(self):
        result = inspect(pdf("A ! B", missing=[33]))
        self.assertFalse(result["tokens"][0]["issues"])
        self.assertTrue(result["tokens"][1]["issues"])
        self.assertFalse(result["tokens"][2]["issues"])
        self.assert_accounted(result)

    def test_unused_missing_code_does_not_poison_text(self):
        result = inspect(pdf("AB", missing=[33]))
        self.assertFalse(result["coverage_reasons"])

    def test_zero_padded_simple_font_cmap_keys(self):
        self.assertFalse(inspect(pdf("AB", padded=True))["coverage_reasons"])

    def test_malformed_map_is_uncertain_not_input_failure(self):
        result = inspect(pdf("AB", malformed=True))
        self.assertTrue(result["tokens"][0]["issues"])
        self.assertIn("malformed", " ".join(result["coverage_reasons"]))

    def test_malformed_map_is_not_cleared_by_embedded_font(self):
        self.assertTrue(inspect(pdf("AB", font=font_bytes(), malformed=True))["tokens"][0]["issues"])

    def test_visible_whitespace_and_controls_never_disappear(self):
        for mapped in (" ", "\t", "\u200b", "\u200c", "\ufffd"):
            with self.subTest(mapped=repr(mapped)):
                result = inspect(pdf("A", mappings={65: mapped}))
                self.assertTrue(result["tokens"])
                self.assertTrue(result["tokens"][0]["issues"])
                self.assertEqual(len(result["glyphs"]), 1)
                self.assertFalse(result["controls"])
                self.assert_accounted(result)

    def test_visible_space_not_repaired_by_embedded_cmap(self):
        result = inspect(pdf("A", font=font_bytes(), mappings={2: " "}))
        self.assertTrue(result["tokens"][0]["issues"])

    def test_only_empty_zwsp_is_normalized_with_inventory(self):
        result = inspect(pdf("A B", mappings={32: "\u200b"}))
        self.assertEqual(len(result["controls"]), 1)
        self.assertEqual("".join(t["text"] for t in result["tokens"]), "AB")
        self.assert_accounted(result)

    def test_embedded_cmap_recovers_missing_used_code(self):
        result = inspect(pdf("AB", font=font_bytes(), missing=[2]))
        self.assertEqual(result["tokens"][0]["text"], "AB")
        self.assertFalse(result["coverage_reasons"])

    def test_ssty_recovers_script_glyph_not_a_font_name_guess(self):
        result = inspect(pdf("\ue002", font=font_bytes(family="UnrelatedName"), missing=[8]))
        self.assertEqual(result["tokens"][0]["text"], "A")
        self.assertFalse(result["tokens"][0]["issues"])

    def test_multi_codepoint_ligature_is_one_occurrence_with_shared_boxes(self):
        result = inspect(pdf("\ue000", font=font_bytes(), mappings={6: "fi"}))
        self.assertEqual(len(result["glyphs"]), 1)
        self.assertEqual(result["glyphs"][0]["native_items"], 2)
        token = result["tokens"][0]
        self.assertEqual(token["text"], "fi")
        self.assertEqual(token["glyphs"][0], token["glyphs"][1])
        self.assertFalse(token["issues"])
        self.assert_accounted(result)

    def test_repeated_ligature_requires_independent_support(self):
        good = inspect(pdf("\ue001", font=font_bytes(), mappings={7: "ff"}))
        bad = inspect(pdf("\ue001", font=font_bytes(features=False), mappings={7: "ff"}))
        self.assertFalse(good["tokens"][0]["issues"])
        self.assertTrue(bad["tokens"][0]["issues"])

    def test_corrupt_repeated_mapping_recovers_only_with_outline_proof(self):
        result = inspect(pdf("A", font=font_bytes(), mappings={2: "AA"}))
        self.assertEqual(result["tokens"][0]["text"], "A")
        self.assertEqual(result["glyphs"][0]["raw"], "AA")

    def test_valid_conflicting_declaration_is_not_overwritten(self):
        result = inspect(pdf("A", font=font_bytes(), mappings={2: "B"}))
        self.assertEqual(result["tokens"][0]["text"], "B")
        self.assertTrue(result["tokens"][0]["issues"])

    def test_same_name_different_fonts_are_not_outline_equal(self):
        a, b = inspect_pdf_pair(pdf("A", font=font_bytes()), pdf("A", font=font_bytes(variant=10)))
        self.assertNotEqual(a["glyphs"][0]["outline_key"], b["glyphs"][0]["outline_key"])
        self.assertNotIn("pair_outline_key", a["glyphs"][0])

    def test_fill_stroke_one_retained_object_is_one_occurrence(self):
        result = inspect(pdf("AB", render_mode=2))
        self.assertEqual(len(result["glyphs"]), 2)
        self.assertEqual(result["glyphs"][0]["paint_modes"], ["fill", "stroke"])
        self.assertEqual(result["evidence_summary"]["fill_stroke_pairs"], 2)

    def test_separate_overprints_remain_separate_occurrences(self):
        for mode in (0, 2):
            result = inspect(pdf("AB", render_mode=mode, overprints=2))
            self.assertEqual(len(result["glyphs"]), 4)
            self.assertEqual("".join(t["text"] for t in result["tokens"]), "ABAB")
            self.assert_accounted(result)

    def test_nested_forms_use_actual_font_resources(self):
        data = pdf("A ! B", missing=[33])
        for _ in range(3):
            with fitz.open(stream=data, filetype="pdf") as source, fitz.open() as target:
                page = target.new_page(width=400, height=300)
                page.show_pdf_page(page.rect, source, 0)
                data = target.tobytes()
        result = inspect(data)
        self.assertFalse(result["tokens"][0]["issues"])
        self.assertTrue(result["tokens"][1]["issues"])
        self.assertEqual(len(result["glyphs"]), 5)

    def test_rotation_keeps_normalized_boxes_and_native_direction(self):
        for rotation in (0, 90, 180, 270):
            result = inspect(pdf("AB", rotation=rotation))
            self.assertEqual(result["pages"][0]["rotation"], rotation)
            self.assertEqual(result["glyphs"][0]["direction"], [1., 0.])
            self.assertTrue(all(0 <= v <= 1 for t in result["tokens"] for v in t["box"]))
            self.assert_accounted(result)

    def test_synthetic_space_boundaries_from_mupdf(self):
        data = pdf(positions=[(40, 70, "the", 16), (65, 70, "study", 16)])
        with fitz.open(stream=data, filetype="pdf") as document:
            expected = document[0].get_text().split()
        self.assertEqual([t["text"] for t in inspect(data)["tokens"]], expected)

    def test_unclassified_inline_vector_does_not_split_native_words(self):
        def drawing(page):
            page.draw_rect((49, 61, 52, 68), color=None, fill=(0, 0, 0))
        result = inspect(pdf("related observation", drawing=drawing))
        self.assertEqual([t["text"] for t in result["tokens"]], ["related", "observation"])
        self.assertTrue(result["inventory"])
        self.assertTrue(result["coverage_reasons"])

    def test_invisible_text_is_retained_with_warning(self):
        result = inspect(pdf("AB", render_mode=3))
        self.assertEqual(len(result["glyphs"]), 2)
        self.assertTrue(result["tokens"][0]["issues"])

    def test_known_input_failures(self):
        with self.assertRaisesRegex(GlyphEvidenceError, "could not be opened"):
            inspect(b"not a pdf")
        with fitz.open(stream=pdf(), filetype="pdf") as document:
            data = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
        with self.assertRaisesRegex(GlyphEvidenceError, "Password-protected"):
            inspect(data)

    def test_no_file_or_database_access_in_service(self):
        data = pdf()
        with patch("builtins.open", side_effect=AssertionError("filesystem access")):
            result = inspect(data)
        self.assert_accounted(result)

    def test_nonidentity_encoding_binds_code_to_cid_before_tounicode(self):
        data = pdf("A", font=font_bytes())
        with fitz.open(stream=data, filetype="pdf") as document:
            page = document[0]
            font = page.get_fonts()[0][0]
            encoding = document.get_new_xref()
            document.update_object(encoding, "<< /Type /CMap /CMapName /Synthetic /WMode 0 >>")
            document.update_stream(encoding, b"begincmap /CMapName /Synthetic def /CMapType 1 def "
                b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def "
                b"1 begincodespacerange <0000><ffff> endcodespacerange "
                b"1 begincidchar <0041> 2 endcidchar endcmap")
            document.xref_set_key(font, "Encoding", f"{encoding} 0 R")
            _mapping(document, font, {65: "A"}, width=2)
            for xref in page.get_contents():
                document.update_stream(xref, document.xref_stream(xref).replace(b"<0002>", b"<0041>"))
            data = document.tobytes()
        result = inspect(data)
        self.assertEqual(result["glyphs"][0]["cid"], 2)
        self.assertEqual(result["tokens"][0]["text"], "A")
        self.assertFalse(result["tokens"][0]["issues"])

    def test_collection_other_face_is_independent_evidence(self):
        collection = TTCollection()
        collection.fonts = [TTFont(io.BytesIO(font_bytes(cmap=False, features=False))),
                            TTFont(io.BytesIO(font_bytes()))]
        output = io.BytesIO()
        collection.save(output)
        data = replace_embedded_font(pdf("\ue002", font=font_bytes(), missing=[8]), output.getvalue())
        result = inspect(data)
        self.assertEqual(result["tokens"][0]["text"], "A")
        self.assertFalse(result["tokens"][0]["issues"])

    def test_missing_cmap_does_not_prevent_outline_inspection(self):
        data = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2]), font_bytes(cmap=False, features=False))
        known = pdf("A", font=font_bytes())
        result = inspect(data, known)
        self.assertIsNotNone(result["glyphs"][0]["outline_key"])
        self.assertEqual(result["tokens"][0]["text"], "A")
        self.assertFalse(result["tokens"][0]["issues"])

    def test_unknown_exact_outlines_do_not_invent_unicode(self):
        data = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2]), font_bytes(cmap=False, features=False))
        result = inspect(data)
        self.assertTrue(result["tokens"][0]["issues"])
        self.assertEqual(result["glyphs"][0]["text"], "\ufffc")
        self.assertEqual(result["tokens"][0]["raw"], result["glyphs"][0]["raw"])
        self.assertIsNotNone(result["tokens"][0]["char_meta"][0]["outline_key"])

    def test_same_font_bytes_with_conflicting_resources_do_not_share_declaration(self):
        left = pdf("A", font=font_bytes(), mappings={2: "A"})
        right = pdf("A", font=font_bytes(), mappings={2: "B"})
        with fitz.open() as document, fitz.open(stream=left, filetype="pdf") as a, fitz.open(stream=right, filetype="pdf") as b:
            page = document.new_page(width=800, height=300)
            page.show_pdf_page(fitz.Rect(0, 0, 400, 300), a, 0)
            page.show_pdf_page(fitz.Rect(400, 0, 800, 300), b, 0)
            result = inspect(document.tobytes())
        self.assertEqual([g["raw"] for g in result["glyphs"]], ["A", "B"])
        self.assertTrue(result["glyphs"][1]["issues"])
        self.assertNotEqual(result["glyphs"][0]["resource_xrefs"], result["glyphs"][1]["resource_xrefs"])

    def test_script_role_changes_preserve_raw_geometry(self):
        sub = inspect(pdf(positions=[(40, 70, "AB", 20), (68, 76, "i", 10)]))
        sup = inspect(pdf(positions=[(40, 70, "AB", 20), (68, 64, "i", 10)]))
        self.assertEqual(sub["glyphs"][-1]["script_role"], "sub")
        self.assertEqual(sup["glyphs"][-1]["script_role"], "super")
        self.assertNotEqual(sub["glyphs"][-1]["origin"], sup["glyphs"][-1]["origin"])

    def test_nearly_equal_outline_is_not_accepted(self):
        missing = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2]), font_bytes(cmap=False, features=False))
        result = inspect(missing, pdf("A", font=font_bytes(variant=1)))
        self.assertTrue(result["tokens"][0]["issues"])
        self.assertNotIn("pair_outline_key", result["glyphs"][0])

    def test_thin_rules_are_not_uncertain_glyph_regions(self):
        def rule(page):
            page.draw_rect((40, 69, 160, 69.001), color=None, fill=(0, 0, 0))
        result = inspect(pdf("Table Test", drawing=rule))
        self.assertFalse(result["uncertain_regions"])
        self.assertEqual([t["text"] for t in result["tokens"]], ["Table", "Test"])

    def test_repeated_outlines_are_cached(self):
        from submissions.services.pdf_glyph_evidence import _Face
        drawn = []
        original = _Face.glyph
        def count(face, gid):
            if gid not in face.outlines:
                drawn.append((face.key, gid))
            return original(face, gid)
        with patch.object(_Face, "glyph", count):
            inspect(pdf("AB" * 100, font=font_bytes()))
        self.assertEqual(len(drawn), len(set(drawn)))
        self.assertLess(len(drawn), 10)

    def test_mutated_retained_text_object_is_not_deduplicated(self):
        from submissions.services.pdf_glyph_evidence import _Capture, _M
        font = fitz.Font("helv")
        text = _M.FzText()
        matrix = _M.FzMatrix(12, 0, 0, 12, 40, 70)
        text.fz_show_glyph(font.this, matrix, font.has_glyph(65), 65, 0, 0, 0, 0)
        device = _Capture(SimpleNamespace(get=lambda font: ([], None)))
        try:
            device._collect("fill", text.m_internal, _M.FzMatrix().internal(), 1., ("black",))
            item = _M.FzTextSpan(text.m_internal.head).items(0)
            item.ucs = 66
            item.gid = font.has_glyph(66)
            device._collect("stroke", text.m_internal, _M.FzMatrix().internal(), 1., ("black",))
            self.assertEqual(len(device.glyphs), 2)
            self.assertEqual(device.paired, 0)
        finally:
            device.release()
            device.fz_close_device()

    def test_different_fill_stroke_paint_is_not_deduplicated(self):
        from submissions.services.pdf_glyph_evidence import _Capture, _M
        font = fitz.Font("helv")
        text = _M.FzText()
        text.fz_show_glyph(font.this, _M.FzMatrix(12, 0, 0, 12, 40, 70), font.has_glyph(65), 65, 0, 0, 0, 0)
        device = _Capture(SimpleNamespace(get=lambda font: ([], None)))
        try:
            device._collect("fill", text.m_internal, _M.FzMatrix().internal(), 1., ("black",))
            device._collect("stroke", text.m_internal, _M.FzMatrix().internal(), 1., ("red",))
            self.assertEqual(len(device.glyphs), 2)
        finally:
            device.release()
            device.fz_close_device()

    def test_unmapped_resource_sharing_pointer_is_not_ignored(self):
        from submissions.services.pdf_glyph_evidence import _meaning
        records = [{"mapping": {65: {"A"}}, "standard": False, "error": None},
                   {"mapping": None, "standard": False, "error": None}]
        value, issues, _ = _meaning(records, {}, 65, "A", True, 1)
        self.assertEqual(value, "A")
        self.assertIn("shared native font", " ".join(issues))

    def test_failed_embedded_font_inspection_remains_visible(self):
        from submissions.services.pdf_glyph_evidence import _Face
        face = _Face(b"invalid embedded font", 0)
        self.assertIn("error", face.glyph(2))

    def test_exact_vector_outline_recovers_without_approximation(self):
        font = font_bytes()
        before = pdf("B\u2192B", font=font, positions=[(40, 100, "B\u2192B", 62.5)])
        def arrow(page):
            shape = page.new_shape()
            shape.draw_polyline([(83.75, 100), (110.3125, 100), (116.5625, 75.625), (86.875, 62.5)])
            shape.finish(closePath=True, color=None, fill=(0, 0, 0))
            shape.commit()
        after = pdf(font=font, positions=[(40, 100, "B", 62.5), (127.5, 100, "B", 62.5)], drawing=arrow)
        _, result = inspect_pdf_pair(before, after)
        recovered = [g for g in result["glyphs"] if g["source"] == "vector_outline"]
        self.assertEqual([g["text"] for g in recovered], ["\u2192"])
        self.assert_accounted(result)
        with fitz.open(stream=after, filetype="pdf") as document:
            self.assertGreater(len(set(document[0].get_pixmap().samples)), 1)

    def _table(self, *, offset=0, duplicate=False, ambiguous=False, values=None):
        with fitz.open() as document:
            page = document.new_page(width=400, height=500)
            for y in ([40 + offset, 220 + offset] if duplicate else [40 + offset]):
                for xx in (40, 140, 240):
                    page.draw_line((xx, y), (xx, y + 120))
                for yy in (y, y + 40, y + 80, y + 120):
                    page.draw_line((40, yy), (240, yy))
                rows = values or (["Name", "Score"], ["One", "10"], ["One" if ambiguous else "Two", "20"])
                for row, cells in enumerate(rows):
                    for column, value in enumerate(cells):
                        page.insert_text((50 + column * 100, y + row * 40 + 25), value, fontsize=12)
            return document.tobytes()

    def test_table_context_is_semantic_and_survives_translation(self):
        a, b = inspect_pdf_pair(self._table(), self._table(offset=15))
        left = {t["text"]: t for t in a["tokens"]}
        right = {t["text"]: t for t in b["tokens"]}
        self.assertIsNotNone(left["10"]["context_key"])
        self.assertEqual(left["10"]["context_key"], right["10"]["context_key"])
        self.assertNotEqual(left["10"]["context_key"], left["20"]["context_key"])
        self.assertNotEqual(left["Name"]["context_key"], left["Score"]["context_key"])
        self.assertNotIn("page", left["10"]["context_key"])

    def test_duplicate_table_identity_remains_uncertain(self):
        result = inspect(self._table(duplicate=True))
        for token in result["tokens"]:
            self.assertIsNone(token["context_key"])
            self.assertTrue(token["issues"])
            self.assertFalse(token["text_identity_supported"])

    def test_repeated_row_labels_use_unique_ordered_table_context(self):
        result, moved = inspect_pdf_pair(self._table(ambiguous=True), self._table(ambiguous=True, offset=20))
        keys = []
        for token in result["tokens"]:
            if token["text"] in {"10", "20"}:
                keys.append(token["context_key"])
                self.assertEqual(json.loads(token["context_key"])[0], "table_ordered_cell")
                self.assertFalse(token["issues"])
                self.assertTrue(token["text_identity_supported"])
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual([t["context_key"] for t in result["tokens"]], [t["context_key"] for t in moved["tokens"]])

    def test_repeated_row_value_edits_and_swaps_keep_distinct_cell_identity(self):
        before = self._table(ambiguous=True)
        for rows in ((["Name", "Score"], ["One", "11"], ["One", "20"]),
                     (["Name", "Score"], ["One", "20"], ["One", "10"])):
            a, b = inspect_pdf_pair(before, self._table(values=rows))
            left = [t for t in a["tokens"] if t["semantic_context"]["column"] == 1 and not t["semantic_context"]["header"]]
            right = [t for t in b["tokens"] if t["semantic_context"]["column"] == 1 and not t["semantic_context"]["header"]]
            self.assertEqual([t["context_key"] for t in left], [t["context_key"] for t in right])
            self.assertNotEqual([(t["text"], t["context_key"]) for t in left],
                                [(t["text"], t["context_key"]) for t in right])
            self.assertTrue(all(t["text_identity_supported"] for t in left + right))

    def test_changed_row_labels_do_not_gain_ordinal_correspondence(self):
        a, _ = inspect_pdf_pair(self._table(ambiguous=True),
                               self._table(values=(["Name", "Score"], ["Changed", "10"], ["One", "20"])))
        for token in a["tokens"]:
            if token["text"] in {"10", "20"}:
                self.assertIsNone(token["context_key"])
                self.assertFalse(token["text_identity_supported"])

    def test_duplicate_repeated_label_tables_remain_ambiguous(self):
        result = inspect(self._table(ambiguous=True, duplicate=True))
        for token in result["tokens"]:
            self.assertIsNone(token["context_key"])
            self.assertFalse(token["text_identity_supported"])

    def test_native_item_limit_aborts_without_partial_evidence(self):
        data = pdf("ABC")
        with patch("submissions.services.pdf_glyph_evidence.MAX_NATIVE_ITEMS", 2):
            with self.assertRaisesRegex(GlyphEvidenceError, "native glyph inspection limit"):
                inspect(data)

    def test_unused_renumbered_unicode_glyph_supplies_exact_evidence(self):
        known = TTFont(io.BytesIO(font_bytes()))
        order = known.getGlyphOrder()
        order[2], order[3] = order[3], order[2]
        known.setGlyphOrder(order)
        buffer = io.BytesIO()
        known.save(buffer)
        missing = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2]), font_bytes(cmap=False, features=False))
        after = pdf("B", font=buffer.getvalue())
        result = inspect(missing, after)
        self.assertEqual(result["tokens"][0]["text"], "A")
        self.assertFalse(result["tokens"][0]["issues"])
        with patch("submissions.services.pdf_glyph_evidence.MAX_CANDIDATE_OUTLINES", 0):
            limited = inspect(missing, after)
        self.assertTrue(limited["tokens"][0]["issues"])
        self.assertIn("work limit", " ".join(limited["coverage_reasons"]))

    def test_opaque_repeated_mapping_emits_one_atom_with_original_raw(self):
        data = replace_embedded_font(pdf("A\ue001B", font=font_bytes(), mappings={7: "ff"}),
                                     font_bytes(cmap=False, features=False))
        before, after = inspect_pdf_pair(data, data)
        for result in (before, after):
            self.assertEqual([t["text"] for t in result["tokens"]], ["A", "\ufffc", "B"])
            token = result["tokens"][1]
            self.assertRegex(token["opaque_key"], r"^[0-9a-f]{64}$")
            self.assertEqual(token["raw"], "ff")
            self.assertEqual(token["raw_context"], "AffB")
            self.assertEqual(len(token["glyphs"]), 1)
            self.assertEqual(len(token["char_meta"]), 1)
            self.assertTrue(token["text_identity_supported"])
            self.assertTrue(token["issues"])
            self.assertFalse(result["coverage"]["complete"])
            self.assert_accounted(result)
        self.assertEqual(before["tokens"][1]["opaque_key"], after["tokens"][1]["opaque_key"])

    def test_different_corrupt_mappings_can_share_only_opaque_identity(self):
        def source(value):
            return replace_embedded_font(pdf("A", font=font_bytes(), mappings={2: value}),
                                         font_bytes(cmap=False, features=False))
        before, after = inspect_pdf_pair(source("AA"), source("\ufffd"))
        left, right = before["tokens"][0], after["tokens"][0]
        self.assertEqual(left["opaque_key"], right["opaque_key"])
        self.assertEqual((left["raw"], right["raw"]), ("AA", "\ufffd"))
        self.assertEqual((left["text"], right["text"]), ("\ufffc", "\ufffc"))
        self.assertTrue(left["issues"] and right["issues"])

    def test_near_or_different_outlines_cannot_be_opaque_pairs(self):
        def source(variant):
            return replace_embedded_font(pdf("A", font=font_bytes(variant=variant), missing=[2]),
                                         font_bytes(cmap=False, features=False, variant=variant))
        for variant in (1, 100):
            with self.subTest(variant=variant):
                pair = inspect_pdf_pair(source(0), source(variant))
                for result in pair:
                    self.assertFalse(result["tokens"][0]["text_identity_supported"])
                    self.assertNotIn("opaque_key", result["tokens"][0])

    def test_opaque_does_not_deduplicate_independent_overprints(self):
        data = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2], overprints=2),
                                     font_bytes(cmap=False, features=False))
        result = inspect(data)
        self.assertEqual([t["text"] for t in result["tokens"]], ["\ufffc", "\ufffc"])
        self.assertNotEqual(result["tokens"][0]["char_meta"][0]["glyph_id"], result["tokens"][1]["char_meta"][0]["glyph_id"])
        self.assert_accounted(result)

    def test_unsafe_mapping_and_clipping_do_not_gain_opaque_identity(self):
        for options in ({"malformed": True}, {"mappings": {2: " "}}, {"mappings": {2: "\u200b"}},
                        {"missing": [2], "render_mode": 3}, {"missing": [2], "render_mode": 7}):
            with self.subTest(options=options):
                data = replace_embedded_font(pdf("A", font=font_bytes(), **options), font_bytes(cmap=False, features=False))
                result = inspect(data)
                self.assertTrue(result["tokens"])
                for token in result["tokens"]:
                    self.assertFalse(token["text_identity_supported"])
                    self.assertNotIn("opaque_key", token)

    def test_valid_unicode_conflicts_cannot_seed_an_opaque_identity(self):
        def source(mapping, missing=()):
            return replace_embedded_font(pdf("A", font=font_bytes(), mappings=mapping, missing=missing),
                                         font_bytes(cmap=False, features=False))
        unknown, a, b = source({}, [2]), source({2: "A"}), source({2: "B"})
        with fitz.open(stream=unknown, filetype="pdf") as document, fitz.open(stream=a, filetype="pdf") as known:
            document.insert_pdf(known)
            before, _ = inspect_pdf_pair(document.tobytes(), b)
        token = before["tokens"][0]
        self.assertFalse(token["text_identity_supported"])
        self.assertNotIn("opaque_key", token)

    def test_resource_ambiguity_does_not_gain_opaque_identity(self):
        from submissions.services.pdf_glyph_evidence import _Face, _meaning, _pair_evidence
        face = _Face(font_bytes(cmap=False, features=False), 0)
        try:
            shape = face.glyph(2)
            for records in (
                [{"mapping": {2: {"A", "B"}}, "standard": False, "error": None}],
                [{"mapping": {2: {"A"}}, "standard": False, "error": None},
                 {"mapping": None, "standard": False, "error": None}],
                [{"mapping": {2: {"A"}}, "standard": False, "error": None},
                 {"mapping": {}, "standard": False, "error": None}],
            ):
                value, issues, source = _meaning(records, shape, 2, "A", True, 2)
                glyph = {"text": value, "issues": issues, "source": source, "_shape": shape,
                         "outline_key": shape["outline_key"], "has_ink": True}
                pair = [{"glyphs": [dict(glyph)]}, {"glyphs": [dict(glyph)]}]
                _pair_evidence(pair)
                self.assertNotIn("opaque_key", pair[0]["glyphs"][0])
        finally:
            face.close()

    def test_known_identity_survives_role_only_warning(self):
        result = inspect(pdf("\u2192", font=font_bytes()))
        token = result["tokens"][0]
        self.assertEqual(token["text"], "\u2192")
        self.assertTrue(token["text_identity_supported"])
        self.assertIn("Mathematical baseline attachment is unresolved.", token["issues"])
        self.assertFalse(result["coverage"]["complete"])

    def test_opaque_identity_survives_role_only_warning(self):
        data = replace_embedded_font(pdf("A", font=font_bytes(), mappings={2: "\U0001d461\U0001d461"}),
                                     font_bytes(cmap=False, features=False))
        token = inspect(data)["tokens"][0]
        self.assertEqual(token["text"], "\ufffc")
        self.assertTrue(token["text_identity_supported"])
        self.assertIn("Mathematical baseline attachment is unresolved.", token["issues"])

    def test_boundary_ambiguity_disables_identity_alignment(self):
        def ambiguous(page, glyphs):
            for glyph in glyphs:
                glyph["layout_issue"] = "Native word/line boundary binding is ambiguous or unavailable."
        data = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2]), font_bytes(cmap=False, features=False))
        with patch("submissions.services.pdf_glyph_evidence._native_boundaries", ambiguous):
            for source in (data, pdf("A")):
                token = inspect(source)["tokens"][0]
                self.assertFalse(token["text_identity_supported"])
                self.assertTrue(token["issues"])
                self.assertNotIn("opaque_key", token)

    def test_collection_face_limit_fails_explicitly(self):
        collection = TTCollection()
        collection.fonts = [TTFont(io.BytesIO(font_bytes())), TTFont(io.BytesIO(font_bytes()))]
        buffer = io.BytesIO()
        collection.save(buffer)
        data = replace_embedded_font(pdf("A", font=font_bytes()), buffer.getvalue())
        with patch("submissions.services.pdf_glyph_evidence.MAX_COLLECTION_FACES", 1):
            with self.assertRaisesRegex(GlyphEvidenceError, "face inspection limit"):
                inspect(data)

    def test_table_header_reading_order_uses_baseline_not_ink_top(self):
        def table(marker_size):
            with fitz.open() as document:
                page = document.new_page(width=400, height=300)
                for x in (40, 150, 300):
                    page.draw_line((x, 40), (x, 200))
                for y in (40, 100, 150, 200):
                    page.draw_line((40, y), (300, y))
                for point, value in (((50, 80), "Name"), ((160, 80), "Score"), ((50, 130), "One"),
                                     ((160, 130), "10"), ((50, 180), "Two"), ((160, 180), "20")):
                    page.insert_text(point, value, fontsize=12)
                page.insert_text((210, 80), "X", fontname="tiro", fontsize=marker_size)
                return document.tobytes()
        before, after = inspect_pdf_pair(table(24), table(12))
        for result in (before, after):
            headers = [t["text"] for t in result["tokens"] if t["semantic_context"]["header"]]
            self.assertEqual(headers, ["Name", "Score", "X"])
            self.assertEqual(result["tokens"][0]["semantic_context"]["headers"], ["Name", "Score X"])
        self.assertEqual([t["context_key"] for t in before["tokens"]], [t["context_key"] for t in after["tokens"]])

    def test_vector_appearance_key_requires_exact_opaque_fill_paint(self):
        from submissions.services.pdf_glyph_evidence import _drawing_appearance_key
        shape = "a" * 64
        drawing = {"type": "f", "fill": (0., 0., 0.), "fill_opacity": 1., "even_odd": False}
        key = _drawing_appearance_key(drawing, shape)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(key, _drawing_appearance_key(dict(drawing), shape))
        self.assertNotEqual(key, _drawing_appearance_key({**drawing, "fill": (1., 0., 0.)}, shape))
        self.assertNotEqual(key, _drawing_appearance_key({**drawing, "even_odd": True}, shape))
        self.assertNotEqual(key, _drawing_appearance_key(drawing, "b" * 64))
        self.assertIsNone(_drawing_appearance_key({**drawing, "type": "fs"}, shape))
        self.assertIsNone(_drawing_appearance_key({**drawing, "fill_opacity": .5}, shape))
        self.assertIsNone(_drawing_appearance_key(drawing, None))

    def test_first_outline_hint_does_not_hide_unused_unicode_conflict(self):
        import copy
        known = TTFont(io.BytesIO(font_bytes()))
        known["glyf"]["B"] = copy.deepcopy(known["glyf"]["A"])
        buffer = io.BytesIO()
        known.save(buffer)
        missing = replace_embedded_font(pdf("A", font=font_bytes(), missing=[2]), font_bytes(cmap=False, features=False))
        before, after = inspect_pdf_pair(missing, pdf("A", font=buffer.getvalue()))
        self.assertFalse(before["tokens"][0]["text_identity_supported"])
        self.assertTrue(before["tokens"][0]["issues"])
        self.assertNotIn("opaque_key", before["tokens"][0])
        self.assertFalse(before["coverage"]["complete"])
        self.assertEqual(after["tokens"][0]["text"], "A")

    def test_vector_anchor_geometry_is_exact_and_requires_both_anchors(self):
        from submissions.services.pdf_glyph_evidence import _anchor_geometry
        left, right = {"origin": [0, 20], "size": 10}, {"origin": [20, 20], "size": 10}
        geometry = _anchor_geometry([10, 10, 12, 15], left, right)
        self.assertEqual(geometry["left"], ["1", "-1", "6/5", "-1/2"])
        self.assertEqual(geometry, _anchor_geometry([120, 220, 124, 230],
            {"origin": [100, 240], "size": 20}, {"origin": [140, 240], "size": 20}))
        self.assertNotEqual(geometry, _anchor_geometry([10, 10, 12, 15], left, {**right, "origin": [20, 21]}))
        self.assertNotEqual(geometry, _anchor_geometry([10, 10, 12.000001, 15], left, right))
        self.assertIsNone(_anchor_geometry([10, 10, 12, 15], left, None))
        self.assertIsNone(_anchor_geometry([10, 10, 12, 15], None, right))

    def test_actualtext_is_retained_separately_from_recovered_native_boundaries(self):
        source = pdf("A B", font=font_bytes(), mappings={2: "AA"})
        marked = actual_text(source, "replacement metadata")
        before, after = inspect_pdf_pair(source, marked)
        self.assertEqual([t["text"] for t in before["tokens"]], ["A", "B"])
        self.assertEqual([t["text"] for t in after["tokens"]], ["A", "B"])
        self.assertTrue(all(t["text_identity_supported"] for t in after["tokens"]))
        spans = [e for e in after["inventory"] if e["kind"] == "actual_text"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["text"], "replacement metadata")
        self.assertEqual(spans[0]["raw"], "AA B")
        self.assertEqual(spans[0]["glyph_ids"], [g["id"] for g in after["glyphs"]])
        self.assert_accounted(after)

    def test_actualtext_cannot_hide_a_real_painted_change(self):
        before, after = inspect_pdf_pair(actual_text(pdf("dose 23"), "same metadata"),
                                         actual_text(pdf("dose 24"), "same metadata"))
        self.assertEqual([t["text"] for t in before["tokens"]], ["dose", "23"])
        self.assertEqual([t["text"] for t in after["tokens"]], ["dose", "24"])

    def test_native_boundary_binding_does_not_guess_between_lines(self):
        from submissions.services.pdf_glyph_evidence import _native_boundaries
        char = {"origin": (10., 20.), "c": "A"}
        page = SimpleNamespace(get_text=lambda *args, **kwargs: {"blocks": [
            {"lines": [{"spans": [{"chars": [char]}]}, {"spans": [{"chars": [char]}]}]}]})
        glyph = {"origin": [10., 20.], "raw": "A"}
        _native_boundaries(page, [glyph])
        self.assertIn("layout_issue", glyph)

    def test_script_run_attachment_survives_small_font_perturbations(self):
        def source(perturbation, y=76, text="curr"):
            positions = [(40, 70, "AB", 20)]
            x = 68
            for index, char in enumerate(text):
                size = 8 + (perturbation if index % 2 else 0)
                positions.append((x, y, char, size))
                x += fitz.get_text_length(char, fontname="helv", fontsize=size)
            return pdf(positions=positions)
        a, b = inspect_pdf_pair(source(0), source(.03))
        for result in (a, b):
            self.assertEqual([g["script_role"] for g in result["glyphs"][-4:]], ["sub"] * 4)
        sup = inspect(source(.03, y=64))
        self.assertEqual([g["script_role"] for g in sup["glyphs"][-4:]], ["super"] * 4)
        changed = inspect(source(.03, text="curB"))
        self.assertEqual("".join(g["text"] for g in changed["glyphs"][-4:]), "curB")
        self.assertEqual([g["script_role"] for g in changed["glyphs"][-4:]], ["sub"] * 4)

    def test_real_baseline_to_subscript_change_remains_supported(self):
        a, b = inspect_pdf_pair(pdf(positions=[(40, 70, "x2", 16)]),
                               pdf(positions=[(40, 70, "x", 16), (48, 75, "2", 10)]))
        self.assertEqual(a["glyphs"][-1]["script_role"], "baseline")
        self.assertEqual(b["glyphs"][-1]["script_role"], "sub")

    def test_smaller_ungrounded_run_is_not_baseline_from_its_own_peers(self):
        from submissions.services.pdf_glyph_evidence import _layout
        result = inspect(pdf(positions=[(40, 70, "A", 20), (85, 75, "ii", 8)]))
        glyphs = result["glyphs"]
        for glyph in glyphs:
            glyph["_layout_line"] = (0, 0)
            glyph["script_role"] = None
            glyph["issues"] = []
        with fitz.open(stream=pdf(), filetype="pdf") as document:
            _layout(glyphs, document[0])
        for glyph in glyphs[-2:]:
            self.assertIsNone(glyph["script_role"])
            self.assertIn("Mathematical baseline attachment is unresolved.", glyph["issues"])

    def _segmented_grid(self, *, polyline=False, offset=0, bar=True, join_gap=0):
        with fitz.open() as document:
            page = document.new_page(width=400, height=300)
            xs, ys = (40, 120, 200), tuple(40 + offset + 12 * i for i in range(4))
            for y in ys:
                page.draw_line((40, y), (200, y), width=.5)
            for x in xs:
                for y0, y1 in zip(ys, ys[1:]):
                    y0, y1 = y0 + join_gap, y1 - join_gap
                    if polyline:
                        shape = page.new_shape()
                        shape.draw_polyline([(x-.25, y0), (x+.25, y0), (x+.25, (y0+y1)/2),
                                             (x+.25, y1), (x-.25, y1), (x-.25, (y0+y1)/2)])
                        shape.finish(closePath=True, color=None, fill=(0, 0, 0))
                        shape.commit()
                    else:
                        page.draw_rect((x-.25, y0, x+.25, y1), color=None, fill=(0, 0, 0))
            for row, values in enumerate((("Name", "Score"), ("One", "10"), ("Two", "20"))):
                for column, value in enumerate(values):
                    page.insert_text((xs[column]+5, ys[row]+9), value, fontsize=9)
            page.insert_font(fontname="custom", fontbuffer=font_bytes())
            page.insert_text((40, 140), "B|B", fontname="custom", fontsize=16)
            page.insert_text((40, 190), "B", fontname="custom", fontsize=16)
            page.insert_text((64, 190), "B", fontname="custom", fontsize=16)
            if bar:
                page.draw_rect((52, 180, 52.5, 190), color=None, fill=(0, 0, 0))
                page.draw_rect((155, ys[1]+2, 155.5, ys[1]+10), color=None, fill=(0, 0, 0))
            page.draw_rect((40, 220, 55, 220.5), color=None, fill=(0, 0, 0))
            return document.tobytes()

    def test_segmented_table_borders_are_owned_but_math_bars_are_not(self):
        for polyline in (False, True):
            with self.subTest(polyline=polyline):
                result = inspect(self._segmented_grid(polyline=polyline))
                grids = [e for e in result["inventory"] if e.get("status") == "table_grid"]
                vertical = [e for e in grids if e["pdf_box"][2]-e["pdf_box"][0] < 1]
                self.assertEqual(len(vertical), 9)
                self.assertTrue(all(e["table_cells"] for e in grids))
                bars = [e for e in result["inventory"] if e.get("pdf_box", [0])[0] in (52., 155.)]
                self.assertEqual(len(bars), 2)
                self.assertTrue(all(e["status"] != "table_grid" for e in bars))
                self.assertTrue(any(e.get("candidate_glyph") for e in bars))
                self.assertFalse(any(e.get("candidate_glyph") for e in grids))
                self.assertIn("|", "".join(t["text"] for t in result["tokens"]))

    def test_grid_budget_retains_uninspected_candidates_and_explicit_coverage(self):
        with patch("submissions.services.pdf_glyph_evidence.MAX_TABLE_OWNERSHIP_WORK", 0):
            result = inspect(self._segmented_grid(polyline=True))
        self.assertIn("Table ruling ownership inspection reached its work limit.", result["coverage_reasons"])
        self.assertTrue(result["uncertain_regions"])
        self.assertFalse(any(e.get("status") == "table_grid" for e in result["inventory"]))

    def test_segmented_grid_junction_gaps_are_bounded_by_ruling_width(self):
        small = inspect(self._segmented_grid(join_gap=.251, bar=False))
        large = inspect(self._segmented_grid(join_gap=2, bar=False))
        self.assertEqual(sum(e.get("status") == "table_grid" and e["pdf_box"][2]-e["pdf_box"][0] < 1
                             for e in small["inventory"]), 9)
        self.assertFalse(any(e.get("status") == "table_grid" and e["pdf_box"][2]-e["pdf_box"][0] < 1
                             for e in large["inventory"]))

    def test_retraced_and_diagonal_paths_are_not_rectangular_rules(self):
        from submissions.services.pdf_glyph_evidence import _axis_rule
        for points in (((0, 0), (1, 0), (1, 10), (1, 0), (0, 0)),
                       ((0, 0), (1, 10), (0, 10))):
            drawing = {"items": [("l", fitz.Point(a), fitz.Point(b)) for a, b in zip(points, points[1:])],
                       "closePath": True, "rect": fitz.Rect(0, 0, 1, 10)}
            self.assertIsNone(_axis_rule(drawing))

    def test_proven_blank_corner_has_structural_not_text_identity(self):
        rows = (["", "Score"], ["One", "10"], ["Two", "20"])
        before, after = inspect_pdf_pair(self._table(values=rows),
            self._table(values=(["", "Score"], ["One", "11"], ["Two", "20"])))
        self.assertTrue(all(t["text_identity_supported"] for t in before["tokens"] + after["tokens"]))
        a = next(t for t in before["tokens"] if t["text"] == "10")
        b = next(t for t in after["tokens"] if t["text"] == "11")
        self.assertEqual(a["context_key"], b["context_key"])
        self.assertEqual(json.loads(a["context_key"])[1][0], ["empty_corner"])
        unsupported = inspect(self._table(values=(["Name", ""], ["One", "10"], ["Two", "20"])))
        self.assertTrue(all(not t["text_identity_supported"] for t in unsupported["tokens"]))

    def test_grid_junction_caps_do_not_hide_squares_or_cell_backgrounds(self):
        data = self._table(values=(["", "Score"], ["One", "10"], ["Two", "20"]))
        for interior in (None, (90, 60, 90.5, 60.5), (40, 40, 140, 80)):
            with self.subTest(interior=interior), fitz.open(stream=data, filetype="pdf") as document:
                page = document[0]
                for x in (40, 140, 240):
                    for y in (40, 80, 120, 160):
                        page.draw_rect((x-.25, y-.25, x+.25, y+.25), color=None, fill=(0, 0, 0))
                if interior:
                    page.draw_rect(interior, color=None, fill=(0, 0, 0))
                result = inspect(document.tobytes())
                caps = [e for e in result["inventory"] if e["kind"] == "vector" and
                        abs(e["pdf_box"][2] - e["pdf_box"][0] - .5) < 1e-8 and
                        e["pdf_box"][0] in (39.75, 139.75, 239.75)]
                self.assertEqual(len(caps), 12)
                self.assertTrue(all(e["status"] == "table_grid" for e in caps))
                if interior:
                    item = next(e for e in result["inventory"] if e.get("pdf_box") == list(interior))
                    self.assertNotEqual(item["status"], "table_grid")
                    self.assertTrue(any(not t["text_identity_supported"] for t in result["tokens"]))
                else:
                    self.assertTrue(all(t["text_identity_supported"] for t in result["tokens"]))

    def test_render_source_key_ignores_document_metadata_but_not_paint_or_resources(self):
        data = pdf("AB", font=font_bytes())
        with fitz.open(stream=data, filetype="pdf") as document:
            document.set_metadata({"title": "Metadata only"})
            rewritten = document.tobytes()
        a, b = inspect_pdf_pair(data, rewritten)
        key = a["pages"][0]["render_source_key"]
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(key, b["pages"][0]["render_source_key"])
        for changed in (pdf("AA", font=font_bytes()), pdf("AB", font=font_bytes(variant=1)),
                        pdf("AB", font=font_bytes(), rotation=90)):
            self.assertNotEqual(key, inspect(changed)["pages"][0]["render_source_key"])

    def test_render_source_key_rejects_annotations_external_streams_and_limits(self):
        from submissions.services.pdf_glyph_evidence import _RenderSource
        data = pdf()
        for mode in ("annotation", "external", "cycle"):
            with self.subTest(mode=mode), fitz.open(stream=data, filetype="pdf") as document:
                page = document[0]
                if mode == "annotation":
                    page.add_text_annot((40, 40), "annotation")
                elif mode == "external":
                    document.xref_set_key(page.get_contents()[0], "F", "(outside.dat)")
                else:
                    resources = int(document.xref_get_key(page.xref, "Resources")[1].split()[0])
                    document.xref_set_key(resources, "Loop", f"{resources} 0 R")
                reader = PdfReader(io.BytesIO(document.tobytes()))
                with self.assertRaises(ValueError):
                    _RenderSource(reader).page_key(0)
        with patch("submissions.services.pdf_glyph_evidence.MAX_RENDER_SOURCE_NODES", 0):
            result = inspect(data)
        self.assertIsNone(result["pages"][0]["render_source_key"])
        self.assertIn("Exact page render-source identity could not be established.", result["coverage_reasons"])
        with patch("submissions.services.pdf_glyph_evidence.MAX_RENDER_SOURCE_BYTES", 0):
            self.assertIsNone(inspect(data)["pages"][0]["render_source_key"])

    def test_render_source_numbers_are_exact_rationals_not_rounded(self):
        from submissions.services.pdf_glyph_evidence import _RenderSource
        from pypdf.generic import FloatObject, NumberObject
        source = _RenderSource(None)
        self.assertEqual(source.digest(NumberObject(0)), source.digest(FloatObject(0.0)))
        self.assertEqual(source.digest(NumberObject(612)), source.digest(FloatObject(612.0)))
        self.assertNotEqual(source.digest(FloatObject(612.000001)), source.digest(NumberObject(612)))
        self.assertNotEqual(source.digest(FloatObject(.1)), source.digest(FloatObject(.1000000001)))

    def test_static_links_keep_exact_metadata_proof_and_all_appearance_inputs(self):
        from submissions.services.pdf_glyph_evidence import _RenderSource
        for kind in (fitz.LINK_URI, fitz.LINK_GOTO):
            with self.subTest(kind=kind), fitz.open(stream=pdf(), filetype="pdf") as document:
                document.new_page()
                link = {"kind": kind, "from": fitz.Rect(30, 30, 80, 45)}
                link.update({"uri": "https://example.test/"} if kind == fitz.LINK_URI else {"page": 1})
                document[0].insert_link(link)
                original = document.tobytes()
                document.set_metadata({"title": "Metadata only"})
                changed = document.tobytes()
            key = _RenderSource(PdfReader(io.BytesIO(original))).page_key(0)
            self.assertEqual(key, _RenderSource(PdfReader(io.BytesIO(changed))).page_key(0))
            for property_name, value in (("C", "[1 0 0]"), ("F", "2"), ("Border", "[0 0 5]"),
                                         ("Rect", "[40 40 90 55]")):
                with self.subTest(property_name=property_name), fitz.open(stream=original, filetype="pdf") as document:
                    xref = document[0].get_links()[0]["xref"]
                    document.xref_set_key(xref, property_name, value)
                    updated = _RenderSource(PdfReader(io.BytesIO(document.tobytes()))).page_key(0)
                    self.assertNotEqual(key, updated)
            with fitz.open(stream=original, filetype="pdf") as document:
                xref = document[0].get_links()[0]["xref"]
                appearance = document.get_new_xref()
                document.update_object(appearance, "<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] >>")
                document.update_stream(appearance, b"q 1 0 0 rg 0 0 10 10 re f Q")
                document.xref_set_key(xref, "AP", f"<< /N {appearance} 0 R >>")
                with_appearance = _RenderSource(PdfReader(io.BytesIO(document.tobytes()))).page_key(0)
                self.assertNotEqual(key, with_appearance)
                document.update_stream(appearance, b"q 0 0 1 rg 0 0 10 10 re f Q")
                self.assertNotEqual(with_appearance,
                    _RenderSource(PdfReader(io.BytesIO(document.tobytes()))).page_key(0))

    def test_link_source_proof_rejects_unproved_backlinks_actions_and_cycles(self):
        from submissions.services.pdf_glyph_evidence import _RenderSource
        with fitz.open(stream=pdf(), filetype="pdf") as document:
            document.new_page()
            document[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(30, 30, 80, 45),
                                    "uri": "https://example.test/"})
            original = document.tobytes()
        for mode in ("foreign_page", "javascript", "additional_action", "next_action", "cycle"):
            with self.subTest(mode=mode), fitz.open(stream=original, filetype="pdf") as document:
                xref = document[0].get_links()[0]["xref"]
                if mode == "foreign_page":
                    document.xref_set_key(xref, "P", f"{document[1].xref} 0 R")
                elif mode == "javascript":
                    document.xref_set_key(xref, "A", "<< /S /JavaScript /JS (change appearance) >>")
                elif mode == "additional_action":
                    document.xref_set_key(xref, "AA", "<< >>")
                elif mode == "next_action":
                    document.xref_set_key(xref, "A", "<< /S /URI /URI (https://example.test/) /Next << >> >>")
                else:
                    document.xref_set_key(xref, "AP", f"<< /N {xref} 0 R >>")
                with self.assertRaises(ValueError):
                    _RenderSource(PdfReader(io.BytesIO(document.tobytes()))).page_key(0)

    def test_render_source_resolves_inherited_geometry_and_resources(self):
        data = pdf()
        with fitz.open(stream=data, filetype="pdf") as document:
            page = document[0]
            parent = int(document.xref_get_key(page.xref, "Parent")[1].split()[0])
            for key in ("MediaBox", "Resources"):
                _, value = document.xref_get_key(page.xref, key)
                document.xref_set_key(parent, key, value)
                document.xref_set_key(page.xref, key, "null")
            inherited = document.tobytes()
        a, b = inspect_pdf_pair(data, inherited)
        self.assertIsNotNone(a["pages"][0]["render_source_key"])
        self.assertEqual(a["pages"][0]["render_source_key"], b["pages"][0]["render_source_key"])
        for change in ("font", "rotation"):
            with fitz.open(stream=inherited, filetype="pdf") as document:
                page = document[0]
                if change == "font":
                    document.xref_set_key(page.get_fonts()[0][0], "BaseFont", "/Courier")
                else:
                    parent = int(document.xref_get_key(page.xref, "Parent")[1].split()[0])
                    document.xref_set_key(page.xref, "Rotate", "null")
                    document.xref_set_key(parent, "Rotate", "90")
                changed = inspect(document.tobytes())
            self.assertIsNotNone(changed["pages"][0]["render_source_key"])
            self.assertNotEqual(a["pages"][0]["render_source_key"], changed["pages"][0]["render_source_key"])

    def test_tolerantly_repaired_dictionary_cannot_seed_source_proof(self):
        with fitz.open(stream=pdf(), filetype="pdf") as document:
            page = document[0]
            resources = int(document.xref_get_key(page.xref, "Resources")[1].split()[0])
            document.xref_set_key(resources, "ExtGState", "<< /GS1 << /PaddingNamePaddingName /Normal >> >>")
            data = document.tobytes()
        marker = b"/PaddingNamePaddingName/Normal"
        self.assertEqual(data.count(marker), 1)
        replacement = b"/BM /Normal /BM /Multiply"
        # Equal-length mutation retains a valid xref; only dictionary keys are
        # malformed, so a tolerant-parser repair cannot serve as equality proof.
        data = data.replace(marker, replacement.ljust(len(marker)))
        result = inspect(data)
        self.assertIsNone(result["pages"][0]["render_source_key"])
        self.assertIn("Exact page render-source identity could not be established.", result["coverage_reasons"])

    def test_render_source_key_includes_optional_content_configuration(self):
        data = pdf()
        with fitz.open(stream=data, filetype="pdf") as document:
            document.add_ocg("Optional", on=True)
            before = document.tobytes()
            document.set_layer(-1, basestate="OFF")
            after = document.tobytes()
        a, b = inspect_pdf_pair(before, after)
        self.assertIsNotNone(a["pages"][0]["render_source_key"])
        self.assertNotEqual(a["pages"][0]["render_source_key"], b["pages"][0]["render_source_key"])
