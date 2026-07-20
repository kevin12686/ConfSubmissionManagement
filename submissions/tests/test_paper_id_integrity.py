import io
import tempfile
from pathlib import Path

import pandas as pd
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from submissions.forms import InitialPaperForm
from submissions.models import FinalSubmission, InitialPaper
from submissions.services.checks import resolve_official_paper_id
from submissions.services.import_export import (
    MAPPING_SHEET_NAME,
    START2_SHEET_NAME,
    import_initial_papers,
)
from submissions.services.import_preview import (
    apply_import_preview,
    preview_final_import,
    preview_initial_import,
)


class PaperIdIntegrityTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        override = override_settings(
            BASE_DIR=self.root,
            MEDIA_ROOT=self.root / "media",
        )
        override.enable()
        self.addCleanup(override.disable)

    @staticmethod
    def _csv(name, text):
        return SimpleUploadedFile(
            name,
            text.encode("utf-8-sig"),
            content_type="text/csv",
        )

    def test_database_rejects_casefold_and_whitespace_variants(self):
        InitialPaper.objects.create(paper_id="P001", title="Canonical")

        with self.assertRaises(IntegrityError), transaction.atomic():
            InitialPaper.objects.create(
                paper_id="p001",
                title="Case collision",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            InitialPaper.objects.create(
                paper_id=" P002 ",
                title="Untrimmed",
            )

    def test_manual_form_strips_id_and_rejects_casefold_collision(self):
        InitialPaper.objects.create(paper_id="P001", title="Canonical")
        normalized = InitialPaperForm(
            {
                "paper_id": "  P002  ",
                "acceptance_status": "accepted",
                "title": "Normalized",
                "authors": "Ada",
                "notes": "",
            }
        )
        collision = InitialPaperForm(
            {
                "paper_id": "p001",
                "acceptance_status": "accepted",
                "title": "Collision",
                "authors": "Grace",
                "notes": "",
            }
        )

        self.assertTrue(normalized.is_valid(), normalized.errors)
        self.assertEqual(normalized.cleaned_data["paper_id"], "P002")
        self.assertFalse(collision.is_valid())
        self.assertIn(
            "different capitalization",
            collision.errors["paper_id"][0],
        )

    def test_master_import_uses_existing_official_capitalization(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="Old title",
            authors="Ada",
        )
        preview = preview_initial_import(
            self._csv(
                "master.csv",
                "paper_id,acceptance_status,title,authors\n"
                " p001 ,accepted,Updated title,Ada\n",
            )
        )

        self.assertEqual(preview["blocking_errors"], [])
        self.assertEqual(preview["rows"][0]["new"]["paper_id"], "P001")
        apply_import_preview(preview["token"])
        self.assertEqual(InitialPaper.objects.count(), 1)
        self.assertEqual(
            InitialPaper.objects.get().title,
            "Updated title",
        )

    def test_direct_master_import_uses_same_safe_pipeline_and_resets_review(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="Old title",
            authors="Ada",
        )
        submission = FinalSubmission.objects.create(
            final_submission_id="10",
            paper_id_filled="P001",
            final_submission_title="Old title",
            paper_id_verified=True,
            verification_status="verified",
        )

        result = import_initial_papers(
            self._csv(
                "master.csv",
                "paper_id,acceptance_status,title,authors\n"
                " p001 ,accepted,New title,Ada\n",
            )
        )

        submission.refresh_from_db()
        self.assertEqual(result, {"created": 0, "updated": 1})
        self.assertEqual(InitialPaper.objects.get().paper_id, "P001")
        self.assertEqual(InitialPaper.objects.get().title, "New title")
        self.assertFalse(submission.paper_id_verified)
        self.assertEqual(submission.verification_status, "pending")

    def test_master_import_blocks_casefold_duplicates_in_same_file(self):
        preview = preview_initial_import(
            self._csv(
                "master.csv",
                "paper_id,acceptance_status,title,authors\n"
                "P001,accepted,First,Ada\n"
                "p001,accepted,Second,Grace\n",
            )
        )

        self.assertEqual(len(preview["blocking_errors"]), 1)
        self.assertIn(
            "including capitalization variants",
            preview["blocking_errors"][0],
        )
        with self.assertRaisesMessage(
            ValueError,
            "Preview has blocking errors",
        ):
            apply_import_preview(preview["token"])
        self.assertFalse(InitialPaper.objects.exists())

    def test_resolver_returns_the_single_official_case(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="Canonical title",
        )

        self.assertEqual(
            resolve_official_paper_id("p001", "Canonical title"),
            "P001",
        )

    def test_mapping_workbook_normalizes_official_id_case(self):
        InitialPaper.objects.create(
            paper_id="P001",
            title="Canonical title",
        )
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame(
                [
                    {
                        "Submission ID": "10",
                        "Paper-ID": "p001",
                        "Title": "Canonical title",
                        "Authors": "Ada",
                    }
                ]
            ).to_excel(
                writer,
                sheet_name=START2_SHEET_NAME,
                index=False,
            )
            pd.DataFrame(
                [["10", "p001"]],
                columns=["Final ID", "Official Paper ID"],
            ).to_excel(
                writer,
                sheet_name=MAPPING_SHEET_NAME,
                index=False,
            )
        workbook = SimpleUploadedFile(
            "mapping.xlsx",
            buffer.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )

        preview = preview_final_import(workbook)

        self.assertEqual(preview["blocking_errors"], [])
        self.assertEqual(
            preview["final_rows"][0]["new"]["paper_id_filled"],
            "P001",
        )
        apply_import_preview(preview["token"])
        self.assertEqual(
            FinalSubmission.objects.get().paper_id_filled,
            "P001",
        )
