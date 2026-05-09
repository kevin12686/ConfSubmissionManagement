import hashlib
import re
import shutil
from pathlib import Path

import fitz
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone
from pypdf import PdfReader

from submissions.models import AppSetting, FinalSubmission, sync_final_submission_state_records
from submissions.services.checks import reset_page_limit_exception
from submissions.services.file_manager import (
    copy_pdf_to_folder,
    resolve_folder,
    sanitize_filename_part,
    source_pdf_path,
    title_short_name,
)


def calculate_pdf_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calculate_page_count(path):
    reader = PdfReader(str(path))
    return len(reader.pages)


def thumbnails_root():
    path = django_settings.MEDIA_ROOT / "pdf_thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_pdf_thumbnails(submission, pdf_path, max_width=220):
    target_dir = thumbnails_root() / sanitize_filename_part(submission.final_submission_id)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    document = fitz.open(str(pdf_path))
    try:
        for index, page in enumerate(document, start=1):
            width = page.rect.width or 1
            scale = max_width / width
            matrix = fitz.Matrix(scale, scale)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(str(target_dir / f"page-{index}.png"))
    finally:
        document.close()
    return target_dir


def thumbnail_urls(submission):
    if not submission.thumbnail_folder:
        return []
    folder = Path(submission.thumbnail_folder)
    if not folder.exists():
        return []
    try:
        relative_folder = folder.relative_to(django_settings.MEDIA_ROOT)
    except ValueError:
        return []
    urls = []
    for path in sorted(folder.glob("page-*.png"), key=_thumbnail_sort_key):
        urls.append(f"{django_settings.MEDIA_URL}{relative_folder.as_posix()}/{path.name}")
    return urls


def _thumbnail_sort_key(path):
    stem = path.stem
    number = stem.split("-")[-1]
    return int(number) if number.isdigit() else 0


def final_submission_sort_key(submission):
    value = str(submission.final_submission_id or "")
    parts = re.split(r"(\d+)", value)
    natural_parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower()) for part in parts if part
    )
    numeric_chunks = re.findall(r"\d+", value)
    numeric_value = int(numeric_chunks[-1]) if numeric_chunks else -1
    return numeric_value, natural_parts, submission.created_at


def scan_incoming_folder():
    setting = AppSetting.load()
    incoming = resolve_folder(setting.incoming_folder)
    created = 0
    updated = 0
    for path in incoming.glob("*.pdf"):
        final_submission_id = path.stem
        submission, was_created = FinalSubmission.objects.get_or_create(
            final_submission_id=final_submission_id,
            defaults={
                "paper_id_filled": "",
                "upload_date": timezone.now(),
                "original_file_name": path.name,
                "current_file_path": str(path),
            },
        )
        if was_created:
            created += 1
        else:
            if not submission.current_file_path:
                submission.current_file_path = str(path)
                submission.original_file_name = submission.original_file_name or path.name
                submission.save(update_fields=["current_file_path", "original_file_name", "updated_at"])
                updated += 1
    return {"created": created, "updated": updated}


def determine_active_versions():
    setting = AppSetting.load()
    with transaction.atomic():
        FinalSubmission.objects.update(active_version=False)
        paper_ids = (
            FinalSubmission.objects.filter(discarded=False)
            .exclude(paper_id_filled="")
            .order_by()
            .values_list("paper_id_filled", flat=True)
            .distinct()
        )
        for paper_id in paper_ids:
            submissions = list(
                FinalSubmission.objects.filter(paper_id_filled=paper_id, discarded=False)
            )
            editor_submissions = [
                submission
                for submission in submissions
                if submission.submission_origin == "editor_upload"
            ]
            candidate_submissions = editor_submissions or submissions
            if setting.active_version_rule == "upload_date":
                newest = max(
                    candidate_submissions,
                    key=lambda submission: (
                        submission.upload_date,
                        final_submission_sort_key(submission),
                    ),
                ) if candidate_submissions else None
            else:
                newest = (
                    max(candidate_submissions, key=final_submission_sort_key)
                    if candidate_submissions
                    else None
                )
            if newest:
                newest.active_version = True
                newest.save(update_fields=["active_version", "updated_at"])
    sync_final_submission_state_records()
    from submissions.services.checks import rebuild_paper_authors

    rebuild_paper_authors()


def _thumbnail_folder_ready(submission):
    return bool(submission.thumbnail_folder and Path(submission.thumbnail_folder).exists())


def process_submission_pdf(submission, force=False):
    path = source_pdf_path(submission)
    if not path:
        submission.processing_status = "error"
        submission.processing_message = "Missing PDF file."
        submission.save(update_fields=["processing_status", "processing_message", "updated_at"])
        return False

    try:
        previous_page_count = submission.page_count
        new_hash = calculate_pdf_hash(path)
        if (
            not force
            and submission.processing_status == "processed"
            and submission.pdf_hash == new_hash
            and submission.page_count is not None
            and _thumbnail_folder_ready(submission)
        ):
            return None
        new_page_count = calculate_page_count(path)
        if previous_page_count != new_page_count:
            reset_page_limit_exception(submission)
        submission.page_count = new_page_count
        submission.pdf_hash = new_hash
        thumbnail_dir = render_pdf_thumbnails(submission, path)
        submission.processing_status = "processed"
        submission.processing_message = "PDF page count, hash, and thumbnails computed."
        submission.thumbnail_folder = str(thumbnail_dir)
        submission.thumbnail_status = "processed"
        submission.thumbnail_message = "PDF thumbnails generated."
        if not submission.current_file_path:
            submission.current_file_path = str(path)
        submission.save(
            update_fields=[
                "page_count",
                "page_limit_exception_approved",
                "page_limit_exception_reason",
                "page_limit_exception_page_count",
                "page_limit_exception_approved_at",
                "pdf_hash",
                "processing_status",
                "processing_message",
                "thumbnail_folder",
                "thumbnail_status",
                "thumbnail_message",
                "current_file_path",
                "updated_at",
            ]
        )
        return True
    except Exception as exc:
        submission.processing_status = "error"
        submission.processing_message = f"PDF processing failed: {exc}"
        submission.thumbnail_status = "error"
        submission.thumbnail_message = f"Thumbnail generation failed: {exc}"
        submission.save(
            update_fields=[
                "processing_status",
                "processing_message",
                "thumbnail_status",
                "thumbnail_message",
                "updated_at",
            ]
        )
        return False


def place_processed_files():
    from submissions.services.storage_inventory import repair_publication_paths

    result = repair_publication_paths(force=True)
    return result["pdf_repaired_count"]


def process_all_pdfs(force=False):
    scan_result = scan_incoming_folder()
    processed = 0
    skipped = 0
    errors = 0
    error_rows = []
    for submission in FinalSubmission.objects.all():
        result = process_submission_pdf(submission, force=force)
        if result is True:
            processed += 1
        elif result is None:
            skipped += 1
        else:
            errors += 1
            error_rows.append(
                {
                    "final_submission_id": submission.final_submission_id,
                    "paper_id": submission.paper_id_filled,
                    "author_entered_id": submission.start2_paper_id_raw,
                    "message": submission.processing_message or "Unknown processing error.",
                }
            )
    determine_active_versions()
    placed = place_processed_files()
    return {
        "scanned_created": scan_result["created"],
        "scanned_updated": scan_result["updated"],
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "error_rows": error_rows,
        "files_placed": placed,
    }


def processed_pdf_rows(limit=100):
    rows = []
    for submission in FinalSubmission.objects.exclude(page_count__isnull=True).order_by(
        "paper_id_filled", "final_submission_id"
    )[:limit]:
        rows.append(
            {
                "submission": submission,
                "thumbnail_urls": thumbnail_urls(submission),
            }
        )
    return rows
