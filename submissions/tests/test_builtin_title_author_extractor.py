import tempfile
from pathlib import Path
from unittest.mock import patch

import pymupdf
from django.test import SimpleTestCase

from submissions.services.builtin_title_author_extractor import (
    format_author_names,
    get_title_author,
)
from submissions.services.checks import split_authors


class BuiltinTitleAuthorExtractorTests(SimpleTestCase):
    def test_formats_parsed_author_names_consistently(self):
        self.assertEqual(format_author_names([]), "")
        self.assertEqual(format_author_names(["Ada Lovelace"]), "Ada Lovelace")
        self.assertEqual(
            format_author_names(["Ada Lovelace", "Alan Turing"]),
            "Ada Lovelace and Alan Turing",
        )
        self.assertEqual(
            format_author_names(["Ada Lovelace", "Alan Turing", "Grace Hopper"]),
            "Ada Lovelace, Alan Turing, and Grace Hopper",
        )

    def test_supported_separator_styles_round_trip_to_canonical_format(self):
        cases = {
            "A and B": "A and B",
            "A, B": "A and B",
            "A and B and C": "A, B, and C",
            "A, B, and C": "A, B, and C",
            "A; B; C": "A, B, and C",
            "A & B": "A and B",
        }
        for raw_authors, expected in cases.items():
            with self.subTest(raw_authors=raw_authors):
                self.assertEqual(
                    format_author_names(split_authors(raw_authors)),
                    expected,
                )

    def test_all_and_multiline_authors_use_one_canonical_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "all-and-authors.pdf"
            document = pymupdf.open()
            page = document.new_page(width=1200, height=800)
            page.insert_text((72, 72), "Confidence-Based Assessment", fontsize=18)
            page.insert_text(
                (72, 112),
                "Vasile Rus and Panayiota Kendeou and Matthew L.",
                fontsize=12,
            )
            page.insert_text(
                (72, 132),
                "Bernacki and Amy Cook and Andrew Tawfik",
                fontsize=12,
            )
            page.insert_text(
                (72, 164),
                "Department of Computer Science",
                fontsize=10,
            )
            document.save(pdf_path)
            document.close()

            with patch(
                "submissions.services.title_author_verification.generate_verification_image"
            ) as renderer:
                title, authors, author_count = get_title_author(
                    pdf_path,
                    verify=True,
                    verify_folder=Path(temp_dir) / "verification",
                )

        expected_names = [
            "Vasile Rus",
            "Panayiota Kendeou",
            "Matthew L. Bernacki",
            "Amy Cook",
            "Andrew Tawfik",
        ]
        self.assertEqual(title, "Confidence-Based Assessment")
        self.assertEqual(
            authors,
            (
                "Vasile Rus, Panayiota Kendeou, Matthew L. Bernacki, "
                "Amy Cook, and Andrew Tawfik"
            ),
        )
        self.assertEqual(author_count, 5)
        self.assertEqual(split_authors(authors), expected_names)

        renderer.assert_called_once()
        renderer_args = renderer.call_args.args
        self.assertEqual(renderer_args[2], authors)
        self.assertEqual(renderer_args[5], expected_names)
