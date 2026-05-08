import hashlib
from pathlib import Path

import fitz
from django.conf import settings as django_settings
from django.db.models import Case, IntegerField, Value, When
from django.db.models import Q
from django.utils import timezone

from submissions.models import FinalSubmission
from submissions.services.file_manager import publication_pdf_info, sanitize_filename_part
from submissions.services.import_export import classify_uploaded_file
from submissions.services.import_preview import (
    _reset_pdf_dependent_state,
    _reset_source_dependent_state,
)


def formatting_rows(query=""):
    status_order = Case(
        When(format_status="pending", then=Value(0)),
        When(format_status="needs_edit", then=Value(1)),
        When(format_status="review_ok", then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    submissions = FinalSubmission.objects.filter(active_version=True).annotate(
        status_order=status_order
    )
    if query:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=query)
            | Q(paper_id_filled__icontains=query)
            | Q(final_submission_title__icontains=query)
            | Q(final_submission_authors__icontains=query)
        )
    return submissions.order_by("status_order", "paper_id_filled", "final_submission_id")


def formatting_preview_info(submission):
    publication_pdf = publication_pdf_info(submission)
    if not publication_pdf["exists"]:
        return {
            "exists": False,
            "url": "",
            "path": "",
            "status": "missing",
            "message": "No publication PDF is available for preview.",
        }

    pdf_path = Path(publication_pdf["path"])
    try:
        stat = pdf_path.stat()
        signature = hashlib.sha256(
            f"{pdf_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
        ).hexdigest()[:16]
        preview_root = django_settings.MEDIA_ROOT / "format_previews"
        preview_root.mkdir(parents=True, exist_ok=True)
        filename = f"{sanitize_filename_part(submission.final_submission_id)}-{signature}.png"
        preview_path = preview_root / filename
        if not preview_path.exists():
            _render_first_page_upper_half(pdf_path, preview_path)
        return {
            "exists": True,
            "url": f"{django_settings.MEDIA_URL}format_previews/{filename}",
            "path": str(preview_path),
            "status": "ready",
            "message": "First page upper-half preview.",
        }
    except Exception as exc:
        return {
            "exists": False,
            "url": "",
            "path": "",
            "status": "error",
            "message": f"Preview generation failed: {exc}",
        }


def _render_first_page_upper_half(pdf_path, preview_path):
    document = fitz.open(str(pdf_path))
    try:
        if document.page_count < 1:
            raise ValueError("PDF has no pages.")
        page = document.load_page(0)
        rect = page.rect
        clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height * 0.5)
        matrix = fitz.Matrix(2, 2)
        pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        pixmap.save(str(preview_path))
    finally:
        document.close()


def update_formatting_submission(submission, cleaned_data):
    previous_status = submission.format_status
    corrected_pdf, corrected_source = _normalize_corrected_uploads(
        cleaned_data.get("corrected_pdf"), cleaned_data.get("corrected_source")
    )

    submission.format_status = cleaned_data["format_status"]
    submission.format_notes = cleaned_data.get("format_notes", "")
    has_new_corrected_file = bool(corrected_pdf or corrected_source)

    if corrected_pdf:
        submission.formatted_pdf_file.save(corrected_pdf.name, corrected_pdf, save=False)
        submission.formatted_pdf_uploaded_at = timezone.now()
        _reset_pdf_dependent_state(submission)
        submission.processing_message = "Corrected PDF uploaded. Run Process PDFs again to refresh page count/hash."

    if corrected_source:
        submission.formatted_source_file.save(corrected_source.name, corrected_source, save=False)
        submission.formatted_source_uploaded_at = timezone.now()
        if not corrected_pdf:
            _reset_source_dependent_state(submission)

    if has_new_corrected_file and previous_status == "review_ok":
        submission.format_status = "pending"

    submission.save()
    return submission


def _normalize_corrected_uploads(corrected_pdf, corrected_source):
    uploads = [file_obj for file_obj in [corrected_pdf, corrected_source] if file_obj]
    pdf = None
    source = None
    for file_obj in uploads:
        kind = classify_uploaded_file(getattr(file_obj, "name", ""))
        if kind == "pdf":
            pdf = file_obj
        elif kind == "source":
            source = file_obj
        elif file_obj is corrected_pdf and pdf is None:
            pdf = file_obj
        elif file_obj is corrected_source and source is None:
            source = file_obj
    return pdf, source
