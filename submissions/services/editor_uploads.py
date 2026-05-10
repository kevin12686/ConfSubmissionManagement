import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.builtin_title_author_extractor import get_title_author
from submissions.services.import_export import _mark_duplicate_submissions, classify_uploaded_file
from submissions.services.import_preview import _reset_pdf_dependent_state, _reset_source_dependent_state
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.file_manager import sanitize_filename_part
from submissions.services.text_utils import clean_note_text
from submissions.services.verification import text_diff_html, titles_identical


EDITOR_UPLOAD_PREVIEW_TTL_HOURS = 2


def editor_conflict_paper_ids():
    rows = (
        FinalSubmission.objects.filter(
            discarded=False,
            excluded_from_publication=False,
        )
        .exclude(paper_id_filled="")
        .values_list("paper_id_filled", "submission_origin")
    )
    grouped = {}
    for paper_id, origin in rows:
        grouped.setdefault(paper_id, set()).add(origin)
    return sorted(
        paper_id
        for paper_id, origins in grouped.items()
        if {"start2", "editor_upload"} <= origins
    )


def editor_conflict_count():
    return len(editor_conflict_paper_ids())


def editor_conflict_details():
    details = []
    for paper_id in editor_conflict_paper_ids():
        submissions = list(
            FinalSubmission.objects.filter(
                paper_id_filled=paper_id,
                discarded=False,
                excluded_from_publication=False,
            ).order_by("submission_origin", "final_submission_id")
        )
        start2 = [item for item in submissions if item.submission_origin == "start2"]
        editor = [item for item in submissions if item.submission_origin == "editor_upload"]
        details.append(
            {
                "paper_id": paper_id,
                "start2": start2,
                "editor": editor,
                "start2_ids": ", ".join(item.final_submission_id for item in start2),
                "editor_ids": ", ".join(item.final_submission_id for item in editor),
            }
        )
    return details


def submission_has_editor_conflict(submission):
    if not submission or submission.discarded or submission.excluded_from_publication:
        return False
    return submission.paper_id_filled in set(editor_conflict_paper_ids())


def generate_editor_final_id(paper_id):
    base = f"EDITOR-{sanitize_filename_part(paper_id)}"
    existing = set(
        FinalSubmission.objects.filter(final_submission_id__startswith=f"{base}-")
        .values_list("final_submission_id", flat=True)
    )
    index = 1
    while True:
        candidate = f"{base}-{index:03d}"
        if candidate not in existing:
            return candidate
        index += 1


def _split_editor_uploads(pdf_file, source_file):
    pdf = None
    source = None
    for file_obj in [pdf_file, source_file]:
        if not file_obj:
            continue
        kind = classify_uploaded_file(getattr(file_obj, "name", ""))
        if kind == "pdf":
            pdf = file_obj
        elif kind == "source":
            source = file_obj
        elif file_obj is pdf_file and pdf is None:
            pdf = file_obj
        elif file_obj is source_file and source is None:
            source = file_obj
    if not pdf:
        raise ValueError("Editor upload requires a PDF file.")
    return pdf, source


def editor_upload_preview_root():
    root = django_settings.MEDIA_ROOT / "editor_upload_previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def preview_editor_upload(cleaned_data):
    paper = cleaned_data["paper"]
    pdf, source = _split_editor_uploads(
        cleaned_data["pdf_file"], cleaned_data.get("source_file")
    )
    token = uuid.uuid4().hex
    token_root = editor_upload_preview_root() / token
    token_root.mkdir(parents=True, exist_ok=True)
    pdf_info = _save_preview_upload(pdf, token_root, "editor_pdf")
    source_info = _save_preview_upload(source, token_root, "editor_source") if source else None

    extracted_title = ""
    extraction_status = "extracted"
    extraction_message = ""
    try:
        extracted_title, _authors, _count = get_title_author(pdf_info["path"], verify=False)
        extracted_title = extracted_title or ""
    except Exception as exc:
        extraction_status = "error"
        extraction_message = f"Title extraction failed: {exc}"

    master_title = paper.title or ""
    form_final_title = cleaned_data.get("final_submission_title", "") or ""
    effective_final_title = form_final_title or master_title
    master_matches = bool(
        extraction_status == "extracted"
        and master_title
        and extracted_title
        and titles_identical(master_title, extracted_title)
    )
    final_matches = bool(
        extraction_status == "extracted"
        and effective_final_title
        and extracted_title
        and titles_identical(effective_final_title, extracted_title)
    )
    requires_confirmation = not (master_matches and final_matches)
    if extraction_status == "extracted" and requires_confirmation:
        extraction_message = "Extracted PDF title differs from the selected Paper Master or Final Title."

    payload = {
        "token": token,
        "paper_pk": paper.pk,
        "created_at": timezone.now().isoformat(),
        "pdf": pdf_info,
        "source": source_info,
        "notes": clean_note_text(cleaned_data.get("notes", "")),
        "final_submission_title": form_final_title,
        "final_submission_authors": cleaned_data.get("final_submission_authors", "") or "",
        "master_title": master_title,
        "effective_final_title": effective_final_title,
        "dry_run_extracted_title": extracted_title,
        "extraction_status": extraction_status,
        "extraction_message": extraction_message,
        "master_matches": master_matches,
        "final_matches": final_matches,
        "requires_confirmation": requires_confirmation,
    }
    (token_root / "payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return _editor_upload_confirmation_context(payload)


def apply_editor_upload_preview(token, confirmed=False):
    payload, token_root = load_editor_upload_preview(token)
    if payload.get("requires_confirmation") and not confirmed:
        raise ValueError("Editor upload title check requires confirmation.")
    paper = InitialPaper.objects.get(pk=payload["paper_pk"])
    opened_files = []
    try:
        pdf_handle = open(payload["pdf"]["path"], "rb")
        opened_files.append(pdf_handle)
        pdf_file = File(pdf_handle, name=payload["pdf"]["original_name"])
        source_file = None
        if payload.get("source"):
            source_handle = open(payload["source"]["path"], "rb")
            opened_files.append(source_handle)
            source_file = File(source_handle, name=payload["source"]["original_name"])
        verified = bool(not payload.get("requires_confirmation"))
        message = (
            "Editor upload title matched Paper Master List and Final Title."
            if verified
            else "Editor upload title differed during dry-run; Paper ID must be verified manually."
        )
        return create_editor_submission(
            paper=paper,
            pdf_file=pdf_file,
            source_file=source_file,
            notes=payload.get("notes", ""),
            final_submission_title=payload.get("final_submission_title", ""),
            final_submission_authors=payload.get("final_submission_authors", ""),
            paper_id_verified=verified,
            verification_message=message,
        )
    finally:
        for handle in opened_files:
            handle.close()
        shutil.rmtree(token_root, ignore_errors=True)


def load_editor_upload_preview(token):
    token_root = editor_upload_preview_root() / sanitize_filename_part(token)
    payload_path = token_root / "payload.json"
    if not payload_path.exists():
        raise ValueError("Editor upload preview expired or does not exist.")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    created_at = datetime.fromisoformat(payload["created_at"])
    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at)
    if timezone.now() - created_at > timedelta(hours=EDITOR_UPLOAD_PREVIEW_TTL_HOURS):
        shutil.rmtree(token_root, ignore_errors=True)
        raise ValueError("Editor upload preview expired. Upload the files again.")
    for key in ["pdf", "source"]:
        if payload.get(key) and not Path(payload[key]["path"]).exists():
            raise ValueError("Editor upload preview file is missing. Upload the files again.")
    return payload, token_root


def _editor_upload_confirmation_context(payload):
    return {
        **payload,
        "diff_master_html": text_diff_html(
            payload.get("master_title", ""), payload.get("dry_run_extracted_title", "")
        )
        if payload.get("master_title") and payload.get("dry_run_extracted_title")
        else "",
        "diff_final_html": text_diff_html(
            payload.get("effective_final_title", ""), payload.get("dry_run_extracted_title", "")
        )
        if payload.get("effective_final_title") and payload.get("dry_run_extracted_title")
        else "",
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


@transaction.atomic
def create_editor_submission(
    *,
    paper,
    pdf_file,
    source_file=None,
    notes="",
    final_submission_title="",
    final_submission_authors="",
    paper_id_verified=True,
    verification_message="Editor upload linked to Paper Master List by editor.",
):
    if not isinstance(paper, InitialPaper):
        paper = InitialPaper.objects.get(pk=paper)
    pdf, source = _split_editor_uploads(pdf_file, source_file)
    now = timezone.now()
    submission = FinalSubmission(
        final_submission_id=generate_editor_final_id(paper.paper_id),
        start2_paper_id_raw=paper.paper_id,
        paper_id_filled=paper.paper_id,
        final_submission_title=final_submission_title or paper.title,
        final_submission_authors=final_submission_authors or paper.authors,
        upload_date=now,
        submission_origin="editor_upload",
        editor_upload_notes=clean_note_text(notes),
        editor_uploaded_at=now,
        processing_status="pending",
        processing_message="Editor upload created. Run Process PDFs before publication.",
        title_author_review_status="pending",
        title_author_extraction_status="pending",
        format_status="pending",
        paper_id_verified=paper_id_verified,
        auto_verify_blocked=not paper_id_verified,
        verification_status="verified" if paper_id_verified else "pending",
        verification_message=verification_message,
        verified_at=now if paper_id_verified else None,
    )
    submission.pdf_file.save(pdf.name, ContentFile(pdf.read()), save=False)
    submission.original_file_name = Path(pdf.name).name
    if source:
        submission.source_file.save(source.name, ContentFile(source.read()), save=False)
        submission.source_original_file_name = Path(source.name).name
    submission.save()
    submission.current_file_path = submission.pdf_file.path
    if submission.source_file:
        submission.source_current_file_path = submission.source_file.path
        _reset_source_dependent_state(submission)
    _reset_pdf_dependent_state(
        submission,
        "Editor-uploaded PDF needs Process PDFs.",
    )
    submission.save()
    determine_active_versions()
    _mark_duplicate_submissions()
    submission.refresh_from_db()
    return submission


@transaction.atomic
def discard_submission(submission, notes):
    notes = (notes or "").strip()
    if not notes:
        raise ValueError("Discard requires a note.")
    submission.discarded = True
    submission.discard_notes = notes
    submission.discarded_at = timezone.now()
    submission.active_version = False
    submission.save(
        update_fields=[
            "discarded",
            "discard_notes",
            "discarded_at",
            "active_version",
            "updated_at",
        ]
    )
    determine_active_versions()
    _mark_duplicate_submissions()
    return submission


@transaction.atomic
def undo_discard_submission(submission):
    submission.discarded = False
    submission.discard_notes = ""
    submission.discarded_at = None
    submission.save(update_fields=["discarded", "discard_notes", "discarded_at", "updated_at"])
    determine_active_versions()
    _mark_duplicate_submissions()
    return submission
