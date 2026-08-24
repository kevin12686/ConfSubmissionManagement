import tempfile
from pathlib import Path

import fitz
from django.test import SimpleTestCase

from submissions.services.title_author_verification import (
    _build_header_layout,
    _draw_header,
    _find_author_text_matches,
    _find_author_text_rects,
    _find_text_rects,
    _verification_font,
    _verification_fonts,
    _verification_text_runs,
    generate_verification_image,
)


class TitleAuthorRendererRegressionTests(SimpleTestCase):
    def test_unicode_header_text_is_embedded_without_base14_glyph_loss(self):
        document = fitz.open()
        try:
            page = document.new_page(width=420, height=300)
            layout = _build_header_layout(
                page.rect.width,
                "unicode-paper.pdf",
                "跨語言出版 Étude",
                ["李明", "José Álvarez", "Жуков Иван"],
                "BUILT-IN",
            )

            _draw_header(page, layout, page.rect.width)

            rendered_text = page.get_text()
            self.assertIn("跨語言出版 Étude", rendered_text)
            self.assertIn("李明", rendered_text)
            self.assertIn("José Álvarez", rendered_text)
            self.assertIn("Жуков Иван", rendered_text)
        finally:
            document.close()

    def test_math_symbol_header_uses_bundled_fallback_without_overlap(self):
        self.assertFalse(_verification_font().has_glyph(ord("⋆")))
        self.assertTrue(
            any(face.font.has_glyph(ord("⋆")) for face in _verification_fonts()[1:])
        )
        self.assertEqual(
            [
                (run.text, run.face.alias)
                for run in _verification_text_runs("Math ⋆")
            ],
            [
                ("Math ", "verification_unicode"),
                ("⋆", "verification_math"),
            ],
        )

        document = fitz.open()
        try:
            page = document.new_page(width=420, height=300)
            layout = _build_header_layout(
                page.rect.width,
                "math-paper.pdf",
                "Absorbing Markov Chains ⋆",
                ["Plain Author"],
                "BUILT-IN",
            )

            _draw_header(page, layout, page.rect.width)

            self.assertIn("Absorbing Markov Chains ⋆", page.get_text())
            title_line = next(
                line
                for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", [])
                if "Absorbing Markov Chains" in "".join(
                    span["text"] for span in line["spans"]
                )
            )
            self.assertEqual(len(title_line["spans"]), 2)
            primary_span, fallback_span = title_line["spans"]
            self.assertEqual(primary_span["font"], "Droid Sans Fallback Regular")
            self.assertEqual(fallback_span["font"], "NotoSansMath-Regular")
            self.assertLessEqual(primary_span["bbox"][2], fallback_span["bbox"][0])
        finally:
            document.close()

    def test_verification_image_accepts_math_symbol_in_header(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            pdf_path = root / "source.pdf"
            target_dir = root / "evidence"
            document = fitz.open()
            try:
                page = document.new_page()
                page.insert_text((72, 72), "Plain source title", fontsize=16)
                page.insert_text((72, 110), "Plain Author", fontsize=12)
                document.save(pdf_path)
            finally:
                document.close()

            output_path, missing_authors = generate_verification_image(
                pdf_path,
                "Plain source title ⋆",
                "Plain Author",
                "BUILT-IN",
                target_dir,
                ["Plain Author"],
            )

            self.assertTrue(output_path.exists())
            self.assertEqual(missing_authors, [])

    def test_unsupported_header_glyph_fails_before_writing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            pdf_path = root / "source.pdf"
            target_dir = root / "evidence"
            document = fitz.open()
            try:
                page = document.new_page()
                page.insert_text((72, 72), "Plain source title")
                document.save(pdf_path)
            finally:
                document.close()

            with self.assertRaisesRegex(
                ValueError,
                "cannot represent all header characters",
            ):
                generate_verification_image(
                    pdf_path,
                    "Unsupported emoji 😀",
                    "Plain Author",
                    "BUILT-IN",
                    target_dir,
                    ["Plain Author"],
                )

            self.assertFalse(target_dir.exists())

    def test_short_author_name_matches_word_boundary_not_linear(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), "Linear Systems", fontsize=12)
            page.insert_text((72, 100), "Li Wei", fontsize=12)

            matches = _find_text_rects(page, "Li", page.rect)

            self.assertEqual(len(matches), 1)
            self.assertGreater(matches[0].y0, 80)
        finally:
            document.close()

    def test_normal_multiword_and_punctuation_tolerant_names_still_match(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text(
                (72, 100),
                "Ada Lovelace, Jean-Luc Picard, and Alan Turing",
                fontsize=12,
            )

            ada_matches = _find_text_rects(page, "Ada Lovelace", page.rect)
            jean_luc_matches = _find_text_rects(page, "Jean Luc Picard", page.rect)

            self.assertTrue(ada_matches)
            self.assertTrue(jean_luc_matches)
        finally:
            document.close()

    def test_author_match_ignores_merged_unicode_superscript_affiliation(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname¹", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Firstname Lastname",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertLess(matches[-1].x1, source_word[2])
        finally:
            document.close()

    def test_author_match_ignores_merged_plain_numeric_affiliation(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname1,2", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Firstname Lastname",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertLess(matches[-1].x1, source_word[2])
        finally:
            document.close()

    def test_author_match_excludes_trailing_symbol_not_present_in_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname*", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Firstname Lastname",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertLess(matches[-1].x1, source_word[2])
        finally:
            document.close()

    def test_author_match_includes_symbol_present_in_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname*", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Firstname Lastname*",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertAlmostEqual(matches[-1].x1, source_word[2], delta=0.5)
        finally:
            document.close()

    def test_author_match_requires_internal_punctuation_to_match(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Jean-Luc Picard", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Jean Luc Picard",
                page.rect,
            )

            self.assertFalse(matches)
        finally:
            document.close()

    def test_author_match_is_case_sensitive(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "firstname lastname",
                page.rect,
            )

            self.assertFalse(matches)
        finally:
            document.close()

    def test_author_match_excludes_trailing_list_punctuation(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname,", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Firstname Lastname",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertLess(matches[-1].x1, source_word[2])
        finally:
            document.close()

    def test_author_match_includes_superscript_present_in_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Firstname Lastname¹", fontsize=12)

            matches = _find_author_text_rects(
                page,
                "Firstname Lastname¹",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertAlmostEqual(matches[-1].x1, source_word[2], delta=0.5)
        finally:
            document.close()

    def test_author_match_ignores_orcid_check_digit_x_after_name(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text(
                (72, 100),
                "Hiroshi Igaki2[0009-0009-0810-983X]",
                fontsize=12,
            )

            matches = _find_author_text_matches(
                page,
                "Hiroshi Igaki",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertFalse(matches[0].partial)
            self.assertLess(matches[0].rects[-1].x1, source_word[2])
        finally:
            document.close()

    def test_author_match_ignores_orcid_and_joined_conjunction_after_name(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text(
                (72, 100),
                "Florian Leitner-Fischer1[0009-0006-8797-188X]*and",
                fontsize=12,
            )

            matches = _find_author_text_matches(
                page,
                "Florian Leitner-Fischer",
                page.rect,
            )

            self.assertTrue(matches)
            self.assertFalse(matches[0].partial)
        finally:
            document.close()

    def test_verification_image_accepts_author_with_superscript_affiliation(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            pdf_path = root / "superscript-author.pdf"
            document = fitz.open()
            try:
                page = document.new_page(width=420, height=300)
                page.insert_text((72, 72), "A Publication Title", fontsize=16)
                page.insert_text((72, 110), "Firstname Lastname¹", fontsize=12)
                document.save(pdf_path)
            finally:
                document.close()

            output_path, missing_authors = generate_verification_image(
                pdf_path,
                "A Publication Title",
                "Firstname Lastname",
                "BUILT-IN",
                root / "evidence",
                ["Firstname Lastname"],
            )

            self.assertTrue(output_path.exists())
            self.assertEqual(missing_authors, [])

    def test_longer_surname_is_an_orange_partial_match(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "John Smithson¹", fontsize=12)

            matches = _find_author_text_matches(page, "John Smith", page.rect)

            self.assertTrue(matches)
            self.assertTrue(matches[0].partial)
            source_word = page.get_text("words", sort=True)[-1]
            self.assertLess(matches[0].rects[-1].x1, source_word[2])
        finally:
            document.close()

    def test_single_word_prefix_is_an_orange_partial_match(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Linear", fontsize=12)

            matches = _find_author_text_matches(page, "Li", page.rect)

            self.assertTrue(matches)
            self.assertTrue(matches[0].partial)
        finally:
            document.close()

    def test_complete_match_suppresses_partial_matches_for_same_author(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Li Linear Li", fontsize=12)

            matches = _find_author_text_matches(page, "Li", page.rect)

            self.assertEqual(len(matches), 2)
            self.assertTrue(all(not match.partial for match in matches))
            matched_text = [
                page.get_textbox(match.rects[0]).strip()
                for match in matches
            ]
            self.assertEqual(matched_text, ["Li", "Li"])
        finally:
            document.close()

    def test_header_layout_carries_author_evidence_states(self):
        layout = _build_header_layout(
            420,
            "paper.pdf",
            "A Publication Title",
            ["Exact Author", "Partial Author", "Missing Author"],
            "BUILT-IN",
            ["matched", "partial", "missing"],
        )

        self.assertEqual(
            layout["author_match_states"],
            ["matched", "partial", "missing"],
        )

    def test_strict_title_match_does_not_ignore_trailing_number(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Model1", fontsize=12)

            matches = _find_text_rects(page, "Model", page.rect)

            self.assertFalse(matches)
        finally:
            document.close()

    def test_title_match_excludes_trailing_symbol_absent_from_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "A Publication Title*", fontsize=12)

            matches = _find_text_rects(
                page,
                "A Publication Title",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertLess(matches[-1].x1, source_word[2])
        finally:
            document.close()

    def test_title_match_includes_symbol_present_in_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "A Publication Title*", fontsize=12)

            matches = _find_text_rects(
                page,
                "A Publication Title*",
                page.rect,
            )
            source_word = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertAlmostEqual(matches[-1].x1, source_word[2], delta=0.5)
        finally:
            document.close()

    def test_title_match_includes_separate_symbol_present_in_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "A Publication Title *", fontsize=12)

            matches = _find_text_rects(
                page,
                "A Publication Title *",
                page.rect,
            )
            source_symbol = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertAlmostEqual(matches[-1].x1, source_symbol[2], delta=0.5)
        finally:
            document.close()

    def test_title_match_excludes_separate_symbol_absent_from_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "A Publication Title *", fontsize=12)

            matches = _find_text_rects(
                page,
                "A Publication Title",
                page.rect,
            )
            source_symbol = page.get_text("words", sort=True)[-1]

            self.assertTrue(matches)
            self.assertLessEqual(matches[-1].x1, source_symbol[0])
        finally:
            document.close()

    def test_title_match_splits_around_pdf_only_internal_punctuation(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "Jean-Luc", fontsize=12)

            matches = _find_text_rects(page, "Jean Luc", page.rect)
            hyphen = page.search_for("-")[0]

            self.assertEqual(len(matches), 2)
            self.assertLessEqual(matches[0].x1, hyphen.x0)
            self.assertGreaterEqual(matches[1].x0, hyphen.x1)
        finally:
            document.close()

    def test_title_match_includes_internal_symbols_present_in_extraction(self):
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 100), "C++ Verification", fontsize=12)

            matches = _find_text_rects(page, "C++ Verification", page.rect)
            source_word = page.get_text("words", sort=True)[0]

            self.assertTrue(matches)
            self.assertGreaterEqual(matches[0].x1, source_word[2] - 0.5)
        finally:
            document.close()

    def test_bundled_font_covers_representative_unicode_scripts(self):
        font = _verification_font()

        for character in "中文ÉtudeЖуков":
            if not character.isspace():
                self.assertTrue(font.has_glyph(ord(character)), character)
