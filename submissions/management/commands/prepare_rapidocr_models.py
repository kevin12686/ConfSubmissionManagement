"""Explicit offline import of the approved RapidOCR model pair."""
import os
from pathlib import Path
import shutil
import tempfile

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from submissions.services.rapidocr_comparison.runtime import MODEL_ENV, selected_model_hashes, validate_models


class Command(BaseCommand):
    help = "Import checksum-validated local OCR models. Does not download or enable OCR automatically."

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="Absolute directory containing both approved ONNX files")
        parser.add_argument("--destination", default=str(Path(settings.BASE_DIR) / "runtime" / "rapidocr-models"),
                            help=f"Durable absolute directory (optional {MODEL_ENV} override)")

    def handle(self, *args, **options):
        try:
            source = validate_models(options["source"])
            destination = Path(options["destination"])
            if not destination.is_absolute() or destination.is_symlink():
                raise ValueError("Destination must be an absolute, non-symlink directory.")
            destination.mkdir(parents=True, exist_ok=True)
            if source.resolve() != destination.resolve():
                with tempfile.TemporaryDirectory(prefix=".ocr-import-", dir=destination) as temporary:
                    staging = Path(temporary)
                    for name in selected_model_hashes():
                        shutil.copyfile(source / name, staging / name)
                    validate_models(staging)
                    for name in selected_model_hashes():
                        if (destination / name).is_symlink():
                            raise ValueError("Destination model must not be a symbolic link.")
                        os.replace(staging / name, destination / name)
            validate_models(destination)
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Validated local OCR models. Configure {MODEL_ENV}={destination}")
