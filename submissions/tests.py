import tempfile
import zipfile
from pathlib import Path

from django.core.files import File
from django.test import TestCase, override_settings
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.checks import _annotate_error_rows
from submissions.services.file_manager import publication_source_info
from submissions.services.reports import export_publication_package


class PublicationPackageTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(self.root / "reports")
        settings_obj.title_words_for_filename = 4
        settings_obj.save()

    def _write_file(self, relative_path, content):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _active_submission(self, **overrides):
        values = {
            "final_submission_id": "100",
            "paper_id_filled": "P001",
            "extracted_title": "Ready Paper",
            "extracted_authors": "Ada Lovelace; Alan Turing",
            "upload_date": timezone.now(),
            "current_file_path": str(self._write_file("files/ready.pdf", b"pdf")),
            "source_current_file_path": str(self._write_file("files/ready.docx", b"source")),
            "page_count": 8,
            "processing_status": "processed",
            "pdf_hash": "ready-hash",
            "active_version": True,
            "paper_id_verified": True,
            "title_author_verified": True,
            "extracted_title_verified": True,
            "similarity_score": 1,
            "single_similarity_score": 1,
            "format_status": "review_ok",
        }
        values.update(overrides)
        return FinalSubmission.objects.create(**values)

    def test_publication_package_blocks_empty_active_set(self):
        with self.assertRaisesMessage(
            ValueError,
            "Publication package blocked because there are no active final submissions.",
        ):
            export_publication_package()

    def test_publication_package_uses_source_current_path_before_source_file(self):
        old_source = self.media_root / "source_submissions" / "old.docx"
        old_source.parent.mkdir(parents=True, exist_ok=True)
        old_source.write_bytes(b"old source")
        current_source = self._write_file("files/current.docx", b"current source")
        submission = self._active_submission(
            extracted_title="Preferred Source",
            source_file="source_submissions/old.docx",
            source_current_file_path=str(current_source),
        )

        source_info = publication_source_info(submission)
        self.assertEqual(source_info["source"], "current")
        self.assertEqual(Path(source_info["path"]), current_source)

        zip_path = export_publication_package()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(
                archive.read("Source/P001-Preferred Source.docx"),
                b"current source",
            )

    def test_publication_package_blocks_unprocessed_corrected_pdf(self):
        submission = self._active_submission(
            final_submission_id="200",
            paper_id_filled="P002",
            extracted_title="Corrected Needs Processing",
            current_file_path=str(self._write_file("files/original.pdf", b"original pdf")),
            source_current_file_path=str(self._write_file("files/corrected.docx", b"source")),
            pdf_hash="stale-original-hash",
        )
        corrected_pdf = self._write_file("files/corrected.pdf", b"corrected pdf")
        with corrected_pdf.open("rb") as handle:
            submission.formatted_pdf_file.save("corrected.pdf", File(handle), save=True)

        with self.assertRaisesMessage(
            ValueError,
            "P002: corrected PDF needs Process PDFs",
        ):
            export_publication_package()

    def test_error_row_annotation_returns_annotated_rows(self):
        rows = [
            {
                "category": "Missing PDF",
                "paper_id": "P001",
                "final_submission_id": "100",
                "message": "No PDF.",
            }
        ]

        annotated = _annotate_error_rows(rows)

        self.assertIs(annotated, rows)
        self.assertEqual(rows[0]["group"], "Files / PDF Processing")
        self.assertEqual(rows[0]["level"], "warning")
