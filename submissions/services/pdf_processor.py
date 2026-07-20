import hashlib
import re
import shutil
from collections import defaultdict
from pathlib import Path

import fitz
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone
from pypdf import PdfReader

from submissions.models import (
    AppSetting,
    FinalSubmission,
    InitialPaper,
)
from submissions.services.checks import reset_page_limit_exception
from submissions.services.final_submission_state import (
    bulk_update_submissions,
    sync_all_submission_state_records,
)
from submissions.services.file_manager import (
    sanitize_filename_part,
    source_pdf_path,
)
from submissions.services.audit import audit_failure, audit_success


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


def determine_active_versions(*, sync_state_records=True, rebuild_authors=True):
    setting = AppSetting.load()
    grouped_submissions = defaultdict(list)
    for submission in FinalSubmission.objects.filter(discarded=False).exclude(
        paper_id_filled=""
    ):
        grouped_submissions[submission.paper_id_filled].append(submission)
    active_ids = []
    for submissions in grouped_submissions.values():
        editor_submissions = [
            submission
            for submission in submissions
            if submission.submission_origin == "editor_upload"
        ]
        candidate_submissions = editor_submissions or submissions
        if setting.active_version_rule == "upload_date":
            newest = (
                max(
                    candidate_submissions,
                    key=lambda submission: (
                        submission.upload_date,
                        final_submission_sort_key(submission),
                    ),
                )
                if candidate_submissions
                else None
            )
        else:
            newest = (
                max(candidate_submissions, key=final_submission_sort_key)
                if candidate_submissions
                else None
            )
        if newest:
            active_ids.append(newest.pk)

    with transaction.atomic():
        FinalSubmission.objects.update(active_version=False)
        if active_ids:
            FinalSubmission.objects.filter(pk__in=active_ids).update(
                active_version=True,
                updated_at=timezone.now(),
            )
    if sync_state_records:
        sync_all_submission_state_records(domain_keys={"identity"})
    if rebuild_authors:
        from submissions.services.checks import rebuild_paper_authors

        rebuild_paper_authors()


def _thumbnail_folder_ready(submission):
    return bool(submission.thumbnail_folder and Path(submission.thumbnail_folder).exists())


PDF_PROCESSING_UPDATE_FIELDS = [
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
]

PDF_CONTENT_RESET_UPDATE_FIELDS = [
    "page_limit_exception_approved",
    "page_limit_exception_reason",
    "page_limit_exception_page_count",
    "page_limit_exception_approved_at",
    "source_hash",
    "extracted_title",
    "extracted_authors",
    "title_author_source",
    "title_author_imported_at",
    "title_author_extraction_status",
    "title_author_extraction_message",
    "title_author_verification_image",
    "title_author_manual_override_reason",
    "title_author_manual_override_at",
    "title_author_review_status",
    "title_author_verified",
    "title_author_verified_at",
    "duplicate_author_review_status",
    "duplicate_author_review_notes",
    "duplicate_author_reviewed_at",
    "author_number_exception_approved",
    "author_number_exception_reason",
    "author_number_exception_author_count",
    "author_number_exception_approved_at",
    "extracted_title_verified",
    "extracted_title_auto_verify_blocked",
    "extracted_title_verified_at",
    "extracted_title_match_status",
    "extracted_title_match_score",
    "extracted_title_match_message",
    "plagiarism_status",
    "similarity_score",
    "single_similarity_score",
    "plagiarism_percent_exception_approved",
    "plagiarism_percent_exception_reason",
    "plagiarism_percent_exception_approved_score",
    "plagiarism_percent_exception_approved_at",
    "single_percent_exception_approved",
    "single_percent_exception_reason",
    "single_percent_exception_approved_score",
    "single_percent_exception_approved_at",
    "plagiarism_report_path",
    "plagiarism_report_stale",
    "plagiarism_imported_at",
    "format_status",
]

PDF_PROCESSING_PERSIST_FIELDS = list(
    dict.fromkeys(PDF_PROCESSING_UPDATE_FIELDS + PDF_CONTENT_RESET_UPDATE_FIELDS)
)


def process_submission_pdf(submission, force=False, *, save=True):
    submission._pdf_content_integrity_reset = False
    path = source_pdf_path(submission)
    if not path:
        submission.processing_status = "error"
        submission.processing_message = "Missing PDF file."
        if save:
            submission.save(
                update_fields=[
                    "processing_status",
                    "processing_message",
                    "updated_at",
                ]
            )
        return False

    try:
        previous_page_count = submission.page_count
        new_hash = calculate_pdf_hash(path)
        content_changed_outside_workflow = bool(
            submission.pdf_hash
            and submission.pdf_hash != new_hash
        )
        if (
            not force
            and submission.processing_status == "processed"
            and submission.pdf_hash == new_hash
            and submission.page_count is not None
            and _thumbnail_folder_ready(submission)
        ):
            return None
        if content_changed_outside_workflow:
            from submissions.services.import_preview import _reset_pdf_dependent_state

            _reset_pdf_dependent_state(
                submission,
                "PDF content changed outside the upload workflow; all dependent "
                "reviews were reset before processing.",
            )
            submission._pdf_content_integrity_reset = True
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
        if save:
            submission.save(
                update_fields=PDF_PROCESSING_PERSIST_FIELDS + ["updated_at"]
            )
        return True
    except Exception as exc:
        submission.processing_status = "error"
        submission.processing_message = f"PDF processing failed: {exc}"
        submission.thumbnail_status = "error"
        submission.thumbnail_message = f"Thumbnail generation failed: {exc}"
        if save:
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


def sync_debug_publication_files():
    from submissions.services.storage_inventory import sync_publication_pdf_debug_folder

    return sync_publication_pdf_debug_folder()


def process_all_pdfs(force=False):
    try:
        determine_active_versions()
        processed = 0
        skipped = 0
        errors = 0
        integrity_resets = 0
        error_rows = []
        publication_submissions = FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
            paper_id_filled__in=InitialPaper.objects.values("paper_id"),
        )
        pending_updates = []

        def flush_pending_updates():
            updates = list(pending_updates)
            pending_updates.clear()
            if updates:
                bulk_update_submissions(
                    updates,
                    PDF_PROCESSING_PERSIST_FIELDS,
                )

        try:
            for submission in publication_submissions:
                result = process_submission_pdf(
                    submission,
                    force=force,
                    save=False,
                )
                if result is True:
                    processed += 1
                    if submission._pdf_content_integrity_reset:
                        integrity_resets += 1
                    pending_updates.append(submission)
                elif result is None:
                    skipped += 1
                else:
                    errors += 1
                    pending_updates.append(submission)
                    error_rows.append(
                        {
                            "final_submission_id": submission.final_submission_id,
                            "paper_id": submission.paper_id_filled,
                            "author_entered_id": submission.start2_paper_id_raw,
                            "message": submission.processing_message or "Unknown processing error.",
                        }
                    )
                if len(pending_updates) >= 100:
                    flush_pending_updates()
        finally:
            flush_pending_updates()
        from submissions.services.checks import rebuild_paper_authors

        rebuild_paper_authors()
        debug_result = sync_debug_publication_files()
        result = {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "integrity_resets": integrity_resets,
            "error_rows": error_rows,
            "debug_synced": debug_result["synced_count"],
            "debug_skipped": debug_result["skipped_count"],
            "debug_manifest": debug_result["manifest_path"],
        }
        audit_success(
            "process_pdfs",
            "PDF processing completed.",
            result_counts={
                "processed": processed,
                "skipped": skipped,
                "errors": errors,
                "integrity_resets": integrity_resets,
                "debug_synced": debug_result["synced_count"],
                "debug_skipped": debug_result["skipped_count"],
                "force": force,
                "scope": "publication_candidates",
            },
            extra={"error_rows": error_rows[:20], "debug_manifest": debug_result["manifest_path"]},
        )
        return result
    except Exception as exc:
        audit_failure("process_pdfs", exc, "PDF processing failed.", result_counts={"force": force})
        raise


def processed_pdf_rows(
    limit=None,
    *,
    submissions=None,
    include_thumbnails=True,
):
    rows = []
    if submissions is None:
        submissions = FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
            paper_id_filled__in=InitialPaper.objects.values("paper_id"),
        ).order_by("paper_id_filled", "final_submission_id")
        if limit is not None:
            submissions = submissions[:limit]
    else:
        submissions = sorted(
            submissions,
            key=lambda submission: (
                submission.paper_id_filled,
                submission.final_submission_id,
            ),
        )
        if limit is not None:
            submissions = submissions[:limit]
    for submission in submissions:
        rows.append(
            {
                "submission": submission,
                "thumbnail_urls": (
                    thumbnail_urls(submission) if include_thumbnails else []
                ),
            }
        )
    return rows


def hydrate_processed_pdf_rows(rows):
    hydrated = []
    for row in rows:
        submission = row["submission"]
        urls = thumbnail_urls(submission)
        page_total = submission.page_count or len(urls)
        hydrated.append(
            {
                **row,
                "thumbnail_urls": urls,
                "page_numbers": range(1, page_total + 1),
            }
        )
    return hydrated
