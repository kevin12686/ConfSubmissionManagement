import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.audit import audit_preview, audit_success
from submissions.services.builtin_title_author_extractor import get_title_author
from submissions.services.file_inspection import FileInspectionContext
from submissions.services.import_export import classify_uploaded_file
from submissions.services.import_preview import _reset_pdf_dependent_state, _reset_source_dependent_state
from submissions.services.preview_storage import (
    purge_expired_preview_directories,
    save_preview_upload,
)
from submissions.services.recompute import recompute_active_and_duplicate_state
from submissions.services.file_manager import sanitize_filename_part
from submissions.services.text_utils import clean_note_text
from submissions.services.verification import build_title_guard_context, titles_identical
from submissions.services.workflow_evidence import (
    make_evidence_token,
    paper_master_edit_evidence,
    require_evidence_token,
    submission_group_evidence,
)


EDITOR_UPLOAD_PREVIEW_TTL_HOURS = 2


@dataclass(frozen=True)
class EditorConflictSnapshot:
    conflict_ids: tuple
    details: tuple

    @classmethod
    def load(cls):
        conflict_ids = tuple(editor_conflict_paper_ids())
        submissions = list(
            FinalSubmission.objects.filter(
                discarded=False,
                excluded_from_publication=False,
                paper_id_filled__in=conflict_ids,
            )
            .order_by("paper_id_filled", "submission_origin", "final_submission_id")
        )
        grouped = {}
        for submission in submissions:
            grouped.setdefault(submission.paper_id_filled, []).append(submission)
        details = []
        for paper_id in conflict_ids:
            items = grouped[paper_id]
            start2 = [item for item in items if item.submission_origin == "start2"]
            editor = [
                item for item in items if item.submission_origin == "editor_upload"
            ]
            details.append(
                {
                    "paper_id": paper_id,
                    "start2": start2,
                    "editor": editor,
                    "start2_ids": ", ".join(
                        item.final_submission_id for item in start2
                    ),
                    "editor_ids": ", ".join(
                        item.final_submission_id for item in editor
                    ),
                }
            )
        return cls(conflict_ids=conflict_ids, details=tuple(details))


def editor_conflict_paper_ids(snapshot=None):
    if snapshot is not None:
        return list(snapshot.conflict_ids)
    conflict_ids = (
        FinalSubmission.objects.filter(
            discarded=False,
            excluded_from_publication=False,
        )
        .exclude(paper_id_filled="")
        .values("paper_id_filled")
        .annotate(
            start2_count=Count(
                "pk",
                filter=Q(submission_origin="start2"),
            ),
            editor_count=Count(
                "pk",
                filter=Q(submission_origin="editor_upload"),
            ),
        )
        .filter(start2_count__gt=0, editor_count__gt=0)
        .values_list("paper_id_filled", flat=True)
    )
    return sorted(conflict_ids)


def editor_conflict_count(snapshot=None):
    return len(editor_conflict_paper_ids(snapshot))


def editor_conflict_details(snapshot=None):
    snapshot = snapshot or EditorConflictSnapshot.load()
    return list(snapshot.details)


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
    purge_expired_preview_directories(
        root,
        timedelta(hours=EDITOR_UPLOAD_PREVIEW_TTL_HOURS),
    )
    return root


def preview_editor_upload(cleaned_data):
    paper = cleaned_data["paper"]
    pdf, source = _split_editor_uploads(
        cleaned_data["pdf_file"], cleaned_data.get("source_file")
    )
    token = uuid.uuid4().hex
    token_root = editor_upload_preview_root() / token
    token_root.mkdir(parents=True, exist_ok=True)
    pdf_info = save_preview_upload(pdf, token_root, "editor_pdf")
    source_info = (
        save_preview_upload(source, token_root, "editor_source")
        if source
        else None
    )

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
        "paper_id": paper.paper_id,
        "paper_evidence": paper_master_edit_evidence(paper),
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
    audit_preview(
        "editor_upload_preview",
        "Editor upload title guard preview created.",
        object_type="InitialPaper",
        paper_id=paper.paper_id,
        result_counts={"requires_confirmation": requires_confirmation},
        file_changes={"pdf": pdf_info, "source": source_info or {}},
        after={
            "master_matches": master_matches,
            "final_matches": final_matches,
            "extraction_status": extraction_status,
        },
        extra={"token": token},
    )
    return _editor_upload_confirmation_context(payload)


@transaction.atomic
def apply_editor_upload_preview(token, confirmed=False):
    payload, token_root = load_editor_upload_preview(token)
    if payload.get("requires_confirmation") and not confirmed:
        raise ValueError("Editor upload title check requires confirmation.")
    paper = InitialPaper.objects.select_for_update().get(pk=payload["paper_pk"])
    if paper_master_edit_evidence(paper) != payload.get("paper_evidence"):
        raise ValueError(
            "Paper Master changed after the Editor Upload preview was created. "
            "Upload and review the current file again."
        )
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
        file_info = payload.get(key)
        if not file_info:
            continue
        path = Path(file_info["path"])
        if not path.exists():
            shutil.rmtree(token_root, ignore_errors=True)
            raise ValueError("Editor upload preview file is missing. Upload the files again.")
        if (
            path.stat().st_size != file_info.get("size")
            or FileInspectionContext().sha256(path, fresh=True)
            != file_info.get("sha256")
        ):
            shutil.rmtree(token_root, ignore_errors=True)
            raise ValueError(
                "Editor upload preview file changed after title review. "
                "Upload and review the file again."
            )
    return payload, token_root


def cancel_editor_upload_preview(token, *, reason="canceled"):
    payload, token_root = load_editor_upload_preview(token)
    shutil.rmtree(token_root, ignore_errors=True)
    audit_success(
        "editor_upload_preview_canceled",
        "Editor upload title-guard preview canceled.",
        object_type="InitialPaper",
        paper_id=payload.get("paper_id", ""),
        result_counts={"reason": reason},
    )
    return payload


def _editor_upload_confirmation_context(payload):
    return {
        **payload,
        "title_guard": build_title_guard_context(
            extracted_title=payload.get("dry_run_extracted_title", ""),
            references=[
                {"label": "Paper Master Title", "title": payload.get("master_title", "")},
                {"label": "Final Title", "title": payload.get("effective_final_title", "")},
            ],
            extraction_status=payload.get("extraction_status", ""),
            extraction_message=payload.get("extraction_message", ""),
        ),
    }


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
    paper_pk = paper.pk if isinstance(paper, InitialPaper) else paper
    paper = InitialPaper.objects.select_for_update().get(pk=paper_pk)
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
    if hasattr(pdf, "seek"):
        pdf.seek(0)
    submission.pdf_file.save(pdf.name, ContentFile(pdf.read()), save=False)
    submission.original_file_name = Path(pdf.name).name
    if source:
        if hasattr(source, "seek"):
            source.seek(0)
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
    recompute_active_and_duplicate_state()
    submission.refresh_from_db()
    audit_success(
        "editor_upload_create",
        "Editor upload final submission created.",
        submission=submission,
        file_changes={
            "pdf": getattr(submission.pdf_file, "name", ""),
            "source": getattr(submission.source_file, "name", ""),
        },
        reset_flags={
            "processing": True,
            "title_author_review": True,
            "format_review": True,
            "plagiarism": True,
        },
        after={
            "paper_id_verified": submission.paper_id_verified,
            "verification_status": submission.verification_status,
        },
    )
    return submission


def _default_version_decision_token(submission, token):
    if token is not None:
        return token
    paper_id = (submission.paper_id_filled or "").strip()
    group = list(
        FinalSubmission.objects.filter(paper_id_filled=paper_id).order_by("pk")
    ) if paper_id else [submission]
    return make_evidence_token(
        "version-decision",
        submission_group_evidence(submission, group),
    )


def _locked_version_decision_group(submission):
    probe = FinalSubmission.objects.only("pk", "paper_id_filled").get(pk=submission.pk)
    paper_id = (probe.paper_id_filled or "").strip()
    filters = Q(pk=probe.pk)
    if paper_id:
        filters |= Q(paper_id_filled=paper_id)
    group = list(
        FinalSubmission.objects.select_for_update()
        .filter(filters)
        .order_by("pk")
    )
    locked = next((item for item in group if item.pk == probe.pk), None)
    if locked is None:
        raise ValueError("Final Submission no longer exists.")
    if (locked.paper_id_filled or "").strip() != paper_id:
        raise ValueError(
            "The Final Submission changed while this action was being applied. "
            "Reload and review the current version state."
        )
    return locked, group


def _copy_submission_state(source, target):
    if source is target:
        return
    for field in source._meta.concrete_fields:
        value = getattr(source, field.attname)
        if hasattr(value, "name"):
            value = value.name
        setattr(target, field.attname, value)


@transaction.atomic
def discard_submission(submission, notes, *, expected_evidence_token=None):
    caller_submission = submission
    notes = (notes or "").strip()
    if not notes:
        raise ValueError("Discard requires a note.")
    expected_evidence_token = _default_version_decision_token(
        submission,
        expected_evidence_token,
    )
    submission, group = _locked_version_decision_group(submission)
    require_evidence_token(
        expected_evidence_token,
        "version-decision",
        submission_group_evidence(submission, group),
    )
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
    recompute_active_and_duplicate_state()
    submission.refresh_from_db()
    audit_success(
        "discard_submission",
        "Final submission version discarded.",
        submission=submission,
        after={"discarded": True, "discard_notes": notes},
    )
    _copy_submission_state(submission, caller_submission)
    return submission


@transaction.atomic
def undo_discard_submission(submission, *, expected_evidence_token=None):
    caller_submission = submission
    expected_evidence_token = _default_version_decision_token(
        submission,
        expected_evidence_token,
    )
    submission, group = _locked_version_decision_group(submission)
    require_evidence_token(
        expected_evidence_token,
        "version-decision",
        submission_group_evidence(submission, group),
    )
    submission.discarded = False
    submission.discard_notes = ""
    submission.discarded_at = None
    submission.save(update_fields=["discarded", "discard_notes", "discarded_at", "updated_at"])
    recompute_active_and_duplicate_state()
    submission.refresh_from_db()
    audit_success(
        "undo_discard_submission",
        "Final submission discard undone.",
        submission=submission,
        after={"discarded": False},
    )
    _copy_submission_state(submission, caller_submission)
    return submission
