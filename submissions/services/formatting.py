import hashlib
import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import fitz
from django.conf import settings as django_settings
from django.core.files import File
from django.db.models import Case, IntegerField, Value, When
from django.db.models import Q
from django.utils import timezone

from submissions.models import FinalSubmission
from submissions.services.builtin_title_author_extractor import get_title_author
from submissions.services.file_manager import publication_pdf_info, sanitize_filename_part
from submissions.services.import_export import classify_uploaded_file
from submissions.services.import_preview import (
    _reset_pdf_dependent_state,
    _reset_source_dependent_state,
)
from submissions.services.verification import text_diff_html, titles_identical


FORMAT_FILTER_OPTIONS = [
    {"value": "needs_attention", "label": "Needs attention"},
    {"value": "pending", "label": "Pending"},
    {"value": "needs_edit", "label": "Needs edit"},
    {"value": "review_ok", "label": "Review OK"},
    {"value": "edited", "label": "Edited"},
    {"value": "all", "label": "All"},
]


def source_file_type_label(file_name):
    extension = str(file_name or "").rsplit(".", 1)[-1].lower() if "." in str(file_name or "") else ""
    if extension in {"doc", "docx"}:
        return "Word"
    if extension in {"tex", "bib", "cls", "sty"}:
        return "TeX"
    if extension == "zip":
        return "ZIP"
    if extension == "rar":
        return "RAR"
    if extension == "7z":
        return "7Z"
    if extension in {"tar", "gz", "bz2", "xz"}:
        return "Archive"
    if extension == "rtf":
        return "RTF"
    if extension == "odt":
        return "ODT"
    return "Unknown"


def original_source_type_label(submission):
    file_name = (
        getattr(submission.source_file, "name", "")
        or submission.source_original_file_name
        or submission.source_current_file_path
    )
    return source_file_type_label(file_name)


def corrected_source_type_label(submission):
    file_name = getattr(submission.formatted_source_file, "name", "")
    return source_file_type_label(file_name)


def formatting_upload_preview_root():
    path = django_settings.MEDIA_ROOT / "formatting_upload_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview_formatting_upload(submission, cleaned_data):
    corrected_pdf, corrected_source = _normalize_corrected_uploads(
        cleaned_data.get("corrected_pdf"), cleaned_data.get("corrected_source")
    )
    if not corrected_pdf:
        return {"requires_confirmation": False, "token": ""}

    token = uuid.uuid4().hex
    token_root = formatting_upload_preview_root() / token
    token_root.mkdir(parents=True, exist_ok=True)

    pdf_info = _save_preview_upload(corrected_pdf, token_root, "corrected_pdf")
    source_info = (
        _save_preview_upload(corrected_source, token_root, "corrected_source")
        if corrected_source
        else None
    )
    extracted_title = ""
    extraction_status = "extracted"
    extraction_message = ""
    try:
        extracted_title, _authors, _author_count = get_title_author(
            pdf_info["path"], verify=False
        )
        extracted_title = extracted_title or ""
    except Exception as exc:
        extraction_status = "error"
        extraction_message = f"Title extraction failed: {exc}"

    final_title = submission.final_submission_title or ""
    title_matches = bool(
        extraction_status == "extracted"
        and final_title
        and extracted_title
        and titles_identical(final_title, extracted_title)
    )
    if extraction_status == "extracted" and not title_matches:
        extraction_message = "Extracted title does not match Final Submission title."

    payload = {
        "submission_id": submission.pk,
        "created_at": timezone.now().isoformat(),
        "format_status": cleaned_data["format_status"],
        "format_notes": cleaned_data.get("format_notes", ""),
        "corrected_pdf": pdf_info,
        "corrected_source": source_info,
        "final_title": final_title,
        "dry_run_extracted_title": extracted_title,
        "extraction_status": extraction_status,
        "extraction_message": extraction_message,
    }
    (token_root / "payload.json").write_text(json.dumps(payload), encoding="utf-8")

    return {
        "requires_confirmation": not title_matches,
        "token": token,
        "title_matches": title_matches,
        "final_title": final_title,
        "dry_run_extracted_title": extracted_title,
        "extraction_status": extraction_status,
        "extraction_message": extraction_message,
        "diff_html": text_diff_html(final_title, extracted_title)
        if final_title and extracted_title
        else "",
    }


def apply_formatting_upload_preview(token):
    payload, token_root = load_formatting_upload_preview(token)
    submission = FinalSubmission.objects.get(pk=payload["submission_id"])
    opened_files = []
    try:
        cleaned_data = {
            "format_status": payload["format_status"],
            "format_notes": payload.get("format_notes", ""),
            "corrected_pdf": None,
            "corrected_source": None,
        }
        if payload.get("corrected_pdf"):
            handle = open(payload["corrected_pdf"]["path"], "rb")
            opened_files.append(handle)
            cleaned_data["corrected_pdf"] = File(
                handle, name=payload["corrected_pdf"]["original_name"]
            )
        if payload.get("corrected_source"):
            handle = open(payload["corrected_source"]["path"], "rb")
            opened_files.append(handle)
            cleaned_data["corrected_source"] = File(
                handle, name=payload["corrected_source"]["original_name"]
            )
        update_formatting_submission(submission, cleaned_data)
    finally:
        for handle in opened_files:
            handle.close()
        shutil.rmtree(token_root, ignore_errors=True)
    return submission


def load_formatting_upload_preview(token):
    token_root = formatting_upload_preview_root() / sanitize_filename_part(token)
    payload_path = token_root / "payload.json"
    if not payload_path.exists():
        raise ValueError("Formatting upload preview expired or does not exist.")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    created_at = datetime.fromisoformat(payload["created_at"])
    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at)
    if timezone.now() - created_at > timedelta(hours=2):
        shutil.rmtree(token_root, ignore_errors=True)
        raise ValueError("Formatting upload preview expired. Upload the files again.")
    for key in ["corrected_pdf", "corrected_source"]:
        if payload.get(key) and not Path(payload[key]["path"]).exists():
            raise ValueError("Formatting upload preview file is missing. Upload the files again.")
    return payload, token_root


def formatting_upload_confirmation(token):
    payload, _token_root = load_formatting_upload_preview(token)
    final_title = payload.get("final_title", "")
    extracted_title = payload.get("dry_run_extracted_title", "")
    return {
        "token": token,
        "submission_id": payload["submission_id"],
        "final_title": final_title,
        "dry_run_extracted_title": extracted_title,
        "extraction_status": payload.get("extraction_status", ""),
        "extraction_message": payload.get("extraction_message", ""),
        "diff_html": text_diff_html(final_title, extracted_title)
        if final_title and extracted_title
        else "",
    }


def formatting_single_navigation(current_submission, query="", status_filter="needs_attention"):
    submissions = list(formatting_rows(query, status_filter))
    ids = [submission.pk for submission in submissions]
    if not current_submission or current_submission.pk not in ids:
        return {"previous": None, "next": None, "next_id": "", "position": 0, "total": len(ids)}
    index = ids.index(current_submission.pk)
    previous_submission = submissions[index - 1] if index > 0 else None
    next_submission = submissions[index + 1] if index + 1 < len(submissions) else None
    return {
        "previous": previous_submission,
        "next": next_submission,
        "next_id": next_submission.pk if next_submission else "",
        "position": index + 1,
        "total": len(ids),
    }


def _save_preview_upload(file_obj, token_root, prefix):
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    original_name = Path(getattr(file_obj, "name", prefix)).name
    suffix = Path(original_name).suffix
    filename = f"{prefix}-{sanitize_filename_part(Path(original_name).stem)}{suffix}"
    path = token_root / filename
    with open(path, "wb") as target:
        for chunk in file_obj.chunks():
            target.write(chunk)
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    return {"path": str(path), "original_name": original_name}


def formatting_rows(query="", status_filter="needs_attention"):
    status_order = Case(
        When(format_status="pending", then=Value(0)),
        When(format_status="needs_edit", then=Value(1)),
        When(format_status="review_ok", then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    submissions = FinalSubmission.objects.filter(active_version=True, discarded=False).annotate(
        status_order=status_order
    )
    if query:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=query)
            | Q(paper_id_filled__icontains=query)
            | Q(final_submission_title__icontains=query)
            | Q(final_submission_authors__icontains=query)
        )
    if status_filter == "pending":
        submissions = submissions.filter(format_status="pending")
    elif status_filter == "needs_edit":
        submissions = submissions.filter(format_status="needs_edit")
    elif status_filter == "review_ok":
        submissions = submissions.filter(format_status="review_ok")
    elif status_filter == "edited":
        submissions = submissions.filter(
            (Q(formatted_pdf_file__isnull=False) & ~Q(formatted_pdf_file=""))
            | (Q(formatted_source_file__isnull=False) & ~Q(formatted_source_file=""))
        )
    elif status_filter == "all":
        pass
    else:
        status_filter = "needs_attention"
        submissions = submissions.exclude(format_status="review_ok")
    return submissions.order_by("status_order", "paper_id_filled", "final_submission_id")


def formatting_filter_counts(query=""):
    return {
        option["value"]: formatting_rows(query, option["value"]).count()
        for option in FORMAT_FILTER_OPTIONS
    }


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
        previous_extraction = {
            "extracted_title": submission.extracted_title,
            "extracted_authors": submission.extracted_authors,
            "title_author_source": submission.title_author_source,
            "title_author_imported_at": submission.title_author_imported_at,
        }
        submission.formatted_pdf_file.save(corrected_pdf.name, corrected_pdf, save=False)
        submission.formatted_pdf_uploaded_at = timezone.now()
        _reset_pdf_dependent_state(submission)
        submission.extracted_title = previous_extraction["extracted_title"]
        submission.extracted_authors = previous_extraction["extracted_authors"]
        submission.title_author_source = previous_extraction["title_author_source"]
        submission.title_author_imported_at = previous_extraction["title_author_imported_at"]
        if submission.extracted_title or submission.extracted_authors:
            submission.title_author_extraction_message = (
                "Corrected PDF uploaded; previous extracted title/authors kept for reference. "
                "Re-extract before publication."
            )
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
