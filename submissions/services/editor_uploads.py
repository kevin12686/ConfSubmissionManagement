from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.import_export import _mark_duplicate_submissions, classify_uploaded_file
from submissions.services.import_preview import _reset_pdf_dependent_state, _reset_source_dependent_state
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.file_manager import sanitize_filename_part


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


@transaction.atomic
def create_editor_submission(
    *,
    paper,
    pdf_file,
    source_file=None,
    notes="",
    final_submission_title="",
    final_submission_authors="",
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
        editor_upload_notes=notes,
        editor_uploaded_at=now,
        processing_status="pending",
        processing_message="Editor upload created. Run Process PDFs before publication.",
        title_author_review_status="pending",
        title_author_extraction_status="pending",
        format_status="pending",
        paper_id_verified=True,
        verification_status="verified",
        verification_message="Editor upload linked to Paper Master List by editor.",
        verified_at=now,
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
