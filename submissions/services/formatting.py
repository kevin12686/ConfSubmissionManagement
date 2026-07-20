import hashlib
import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import fitz
from django.conf import settings as django_settings
from django.core.files import File
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Value, When
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.audit import audit_preview, audit_success
from submissions.services.builtin_title_author_extractor import get_title_author
from submissions.services.file_inspection import FileInspectionContext
from submissions.services.file_manager import (
    publication_pdf_info,
    publication_source_info,
    sanitize_filename_part,
)
from submissions.services.import_export import classify_uploaded_file
from submissions.services.import_preview import (
    _reset_pdf_dependent_state,
    _reset_source_dependent_state,
)
from submissions.services.text_utils import clean_note_text, natural_text_key
from submissions.services.verification import build_title_guard_context, titles_identical


FORMAT_FILTER_OPTIONS = [
    {"value": "needs_attention", "label": "Needs attention"},
    {"value": "pending", "label": "Pending"},
    {"value": "needs_edit", "label": "Needs edit"},
    {"value": "review_ok", "label": "Review OK"},
    {"value": "review_ok_no_edit", "label": "Review OK, no edit"},
    {"value": "edited", "label": "Edited"},
    {"value": "all", "label": "All"},
]
FORMAT_STATUS_LABELS = {
    "pending": "Pending",
    "needs_edit": "Needs edit",
    "review_ok": "Review OK",
}

FORMATTING_QUEUE_SESSION_KEY = "formatting_review_queues"
FORMATTING_SNAPSHOT_SESSION_KEY = "formatting_review_snapshots"
FORMATTING_WORKFLOW_TTL = timedelta(hours=2)
FORMATTING_QUEUE_LIMIT = 20
FORMATTING_SNAPSHOT_LIMIT = 5000


class FormattingWorkflowError(ValueError):
    pass


@transaction.atomic
def record_formatting_issue_from_pdf_preview(
    submission,
    issue_note,
    *,
    page_number=None,
    request=None,
):
    submission = FinalSubmission.objects.select_for_update().get(pk=submission.pk)
    if (
        not submission.active_version
        or submission.discarded
        or submission.excluded_from_publication
        or not InitialPaper.objects.filter(
            paper_id=submission.paper_id_filled
        ).exists()
    ):
        raise FormattingWorkflowError(
            "This record is no longer a current Paper Master publication candidate."
        )

    note = clean_note_text(issue_note)
    if not note:
        raise FormattingWorkflowError("Formatting issue note is required.")
    if page_number is not None:
        if page_number < 1:
            raise FormattingWorkflowError("Page number must be at least 1.")
        if submission.page_count is not None and page_number > submission.page_count:
            raise FormattingWorkflowError(
                f"Page {page_number} is outside this {submission.page_count}-page PDF."
            )
        note = f"Page {page_number}: {note}"

    previous_status = submission.format_status
    previous_notes = submission.format_notes
    previous_source_hash = submission.source_hash
    submission.format_status = "needs_edit"
    submission.format_notes = clean_note_text(
        "\n\n".join(
            value
            for value in (submission.format_notes, note)
            if str(value or "").strip()
        )
    )
    submission.source_hash = ""
    submission.save(
        update_fields=[
            "format_status",
            "format_notes",
            "source_hash",
            "updated_at",
        ]
    )
    audit_success(
        "formatting_issue_recorded_from_pdf_preview",
        "Formatting issue recorded from Process PDFs.",
        request=request,
        submission=submission,
        changed_fields=["format_status", "format_notes", "source_hash"],
        before={
            "format_status": previous_status,
            "format_notes": previous_notes,
            "source_hash": previous_source_hash,
        },
        after={
            "format_status": submission.format_status,
            "format_notes": submission.format_notes,
            "source_hash": submission.source_hash,
        },
        reset_flags={
            "format_review_reset_from_review_ok": previous_status == "review_ok",
            "format_review_source_binding_cleared": bool(previous_source_hash),
        },
        extra={"page_number": page_number},
    )
    return submission


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


def preview_formatting_upload(
    submission,
    cleaned_data,
    *,
    review_snapshot,
    workflow_context=None,
):
    validate_formatting_review_snapshot(submission, review_snapshot)
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
        "format_status_label": FORMAT_STATUS_LABELS.get(
            cleaned_data["format_status"],
            cleaned_data["format_status"],
        ),
        "format_notes": cleaned_data.get("format_notes", ""),
        "corrected_pdf": pdf_info,
        "corrected_source": source_info,
        "final_title": final_title,
        "dry_run_extracted_title": extracted_title,
        "extraction_status": extraction_status,
        "extraction_message": extraction_message,
        "review_snapshot": review_snapshot,
        "workflow_context": workflow_context or {},
    }
    (token_root / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    audit_preview(
        "formatting_upload_preview",
        "Corrected PDF title guard preview created.",
        submission=submission,
        result_counts={"requires_confirmation": not title_matches},
        file_changes={"corrected_pdf": pdf_info, "corrected_source": source_info or {}},
        after={
            "title_matches": title_matches,
            "extraction_status": extraction_status,
            "dry_run_extracted_title": extracted_title,
        },
        extra={"token": token},
    )

    return {
        "requires_confirmation": not title_matches,
        "token": token,
        "title_matches": title_matches,
        "final_title": final_title,
        "dry_run_extracted_title": extracted_title,
        "extraction_status": extraction_status,
        "extraction_message": extraction_message,
        "corrected_pdf_name": pdf_info["original_name"],
        "corrected_source_name": (
            source_info["original_name"] if source_info else ""
        ),
        "format_status": cleaned_data["format_status"],
        "format_status_label": FORMAT_STATUS_LABELS.get(
            cleaned_data["format_status"],
            cleaned_data["format_status"],
        ),
        "format_notes": cleaned_data.get("format_notes", ""),
        "title_guard": build_title_guard_context(
            extracted_title=extracted_title,
            references=[{"label": "Final Submission Title", "title": final_title}],
            extraction_status=extraction_status,
            extraction_message=extraction_message,
        ),
    }


def apply_formatting_upload_preview(token):
    payload, token_root = load_formatting_upload_preview(token)
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
        submission = save_formatting_review(
            payload["submission_id"],
            cleaned_data,
            payload["review_snapshot"],
        )
    except Exception:
        raise
    else:
        shutil.rmtree(token_root, ignore_errors=True)
    finally:
        for handle in opened_files:
            handle.close()
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
        if not payload.get(key):
            continue
        path = Path(payload[key]["path"])
        if not path.exists():
            raise ValueError("Formatting upload preview file is missing. Upload the files again.")
        if (
            path.stat().st_size != payload[key].get("size")
            or FileInspectionContext().sha256(path, fresh=True)
            != payload[key].get("sha256")
        ):
            raise ValueError(
                "Formatting upload preview file changed. Upload the files again."
            )
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
        "corrected_pdf_name": payload.get("corrected_pdf", {}).get(
            "original_name", ""
        ),
        "corrected_source_name": (
            payload.get("corrected_source", {}) or {}
        ).get("original_name", ""),
        "format_status": payload.get("format_status", "pending"),
        "format_status_label": FORMAT_STATUS_LABELS.get(
            payload.get("format_status", "pending"),
            payload.get("format_status", "pending"),
        ),
        "format_notes": payload.get("format_notes", ""),
        "workflow_context": payload.get("workflow_context", {}),
        "title_guard": build_title_guard_context(
            extracted_title=extracted_title,
            references=[{"label": "Final Submission Title", "title": final_title}],
            extraction_status=payload.get("extraction_status", ""),
            extraction_message=payload.get("extraction_message", ""),
        ),
    }


def cancel_formatting_upload_preview(token):
    payload, token_root = load_formatting_upload_preview(token)
    submission = FinalSubmission.objects.filter(pk=payload.get("submission_id")).first()
    shutil.rmtree(token_root, ignore_errors=True)
    audit_success(
        "formatting_upload_preview_canceled",
        "Corrected PDF title-guard preview canceled.",
        submission=submission,
        result_counts={"removed_preview": 1},
    )
    return payload


def create_formatting_review_queue(
    session,
    *,
    query="",
    status_filter="needs_attention",
    current_submission_id=None,
):
    rows = sorted(
        list(formatting_rows(query, status_filter)),
        key=lambda submission: (
            natural_text_key(submission.paper_id_filled),
            natural_text_key(submission.final_submission_id),
            submission.pk,
        ),
    )
    ids = [submission.pk for submission in rows]
    if current_submission_id:
        try:
            current_submission_id = int(current_submission_id)
        except (TypeError, ValueError) as exc:
            raise FormattingWorkflowError("Invalid current formatting paper.") from exc
        if current_submission_id not in ids:
            raise FormattingWorkflowError(
                "The selected paper is not in this formatting filter. "
                "Return to the list or start an All-papers queue."
            )
    elif ids:
        current_submission_id = ids[0]
    else:
        current_submission_id = None

    token = uuid.uuid4().hex
    payload = {
        "created_at": timezone.now().isoformat(),
        "query": str(query or "").strip(),
        "filter": status_filter,
        "ids": ids,
    }
    queues = _load_session_payloads(
        session,
        FORMATTING_QUEUE_SESSION_KEY,
        FORMATTING_QUEUE_LIMIT,
    )
    queues[token] = payload
    session[FORMATTING_QUEUE_SESSION_KEY] = _trim_session_payloads(
        queues,
        FORMATTING_QUEUE_LIMIT,
    )
    session.modified = True
    return formatting_review_queue_context(
        session,
        token,
        current_submission_id=current_submission_id,
    )


def formatting_review_queue_context(
    session,
    token,
    *,
    current_submission_id=None,
):
    queues = _load_session_payloads(
        session,
        FORMATTING_QUEUE_SESSION_KEY,
        FORMATTING_QUEUE_LIMIT,
    )
    payload = queues.get(str(token or ""))
    if not payload:
        raise FormattingWorkflowError(
            "This Single Paper queue expired. Start Single Paper Mode again."
        )
    ids = [int(value) for value in payload.get("ids", [])]
    if current_submission_id in (None, ""):
        current_submission_id = ids[0] if ids else None
    else:
        try:
            current_submission_id = int(current_submission_id)
        except (TypeError, ValueError) as exc:
            raise FormattingWorkflowError("Invalid current formatting paper.") from exc
    if current_submission_id is not None and current_submission_id not in ids:
        raise FormattingWorkflowError(
            "This paper does not belong to the active Single Paper queue."
        )

    all_records = FinalSubmission.objects.in_bulk(ids)
    candidate_ids = set(
        _formatting_queryset().filter(pk__in=ids).values_list("pk", flat=True)
    )
    current_submission = all_records.get(current_submission_id)
    current_in_scope = bool(
        current_submission and current_submission_id in candidate_ids
    )
    index = ids.index(current_submission_id) if current_submission_id in ids else -1

    previous_submission = _nearest_queue_candidate(
        ids[:index] if index >= 0 else [],
        all_records,
        candidate_ids,
        reverse_order=True,
    )
    next_submission = _nearest_queue_candidate(
        ids[index + 1 :] if index >= 0 else ids,
        all_records,
        candidate_ids,
    )
    remaining = sum(
        1
        for candidate_id in (ids[index + 1 :] if index >= 0 else ids)
        if candidate_id in candidate_ids
    )
    list_query = {"filter": payload.get("filter", "needs_attention")}
    if payload.get("query"):
        list_query["q"] = payload["query"]
    back_url = f"{reverse('submissions:formatting')}?{urlencode(list_query)}"

    return {
        "token": token,
        "filter": payload.get("filter", "needs_attention"),
        "q": payload.get("query", ""),
        "current": current_submission,
        "current_in_scope": current_in_scope,
        "previous": previous_submission,
        "next": next_submission,
        "position": index + 1 if index >= 0 else 0,
        "total": len(ids),
        "remaining": remaining,
        "back_url": back_url,
        "previous_url": _formatting_queue_url(
            token,
            previous_submission.pk if previous_submission else None,
            status_filter=payload.get("filter", "needs_attention"),
            query=payload.get("query", ""),
        ),
        "next_url": _formatting_queue_url(
            token,
            next_submission.pk if next_submission else None,
            status_filter=payload.get("filter", "needs_attention"),
            query=payload.get("query", ""),
        ),
    }


def create_formatting_review_snapshot(session, submission):
    snapshot = _formatting_review_snapshot(submission)
    token = uuid.uuid4().hex
    snapshots = _load_session_payloads(
        session,
        FORMATTING_SNAPSHOT_SESSION_KEY,
        FORMATTING_SNAPSHOT_LIMIT,
    )
    snapshots[token] = snapshot
    session[FORMATTING_SNAPSHOT_SESSION_KEY] = _trim_session_payloads(
        snapshots,
        FORMATTING_SNAPSHOT_LIMIT,
    )
    session.modified = True
    return token


def load_formatting_review_snapshot(
    session,
    token,
    *,
    submission_id=None,
):
    snapshots = _load_session_payloads(
        session,
        FORMATTING_SNAPSHOT_SESSION_KEY,
        FORMATTING_SNAPSHOT_LIMIT,
    )
    snapshot = snapshots.get(str(token or ""))
    if not snapshot:
        raise FormattingWorkflowError(
            "This formatting review page expired. Reload the current paper before saving."
        )
    if submission_id is not None and int(snapshot["submission_id"]) != int(submission_id):
        raise FormattingWorkflowError(
            "The formatting review snapshot does not belong to this paper."
        )
    return snapshot


def discard_formatting_review_snapshot(session, token):
    snapshots = _load_session_payloads(
        session,
        FORMATTING_SNAPSHOT_SESSION_KEY,
        FORMATTING_SNAPSHOT_LIMIT,
    )
    if snapshots.pop(str(token or ""), None) is not None:
        session[FORMATTING_SNAPSHOT_SESSION_KEY] = snapshots
        session.modified = True


def save_formatting_review(submission_id, cleaned_data, review_snapshot):
    with transaction.atomic():
        submission = FinalSubmission.objects.select_for_update().get(pk=submission_id)
        validate_formatting_review_snapshot(submission, review_snapshot)
        return update_formatting_submission(submission, cleaned_data)


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
    return {
        "path": str(path),
        "original_name": original_name,
        "size": path.stat().st_size,
        "sha256": FileInspectionContext().sha256(path, fresh=True),
    }


def _formatting_queryset(query=""):
    submissions = FinalSubmission.objects.filter(
        active_version=True,
        discarded=False,
        excluded_from_publication=False,
        paper_id_filled__in=InitialPaper.objects.values("paper_id"),
    )
    if query:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=query)
            | Q(paper_id_filled__icontains=query)
            | Q(final_submission_title__icontains=query)
            | Q(final_submission_authors__icontains=query)
        )
    return submissions


def formatting_rows(query="", status_filter="needs_attention"):
    status_order = Case(
        When(format_status="pending", then=Value(0)),
        When(format_status="needs_edit", then=Value(1)),
        When(format_status="review_ok", then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    submissions = _formatting_queryset(query).annotate(status_order=status_order)
    if status_filter == "pending":
        submissions = submissions.filter(format_status="pending")
    elif status_filter == "needs_edit":
        submissions = submissions.filter(format_status="needs_edit")
    elif status_filter == "review_ok":
        submissions = submissions.filter(format_status="review_ok")
    elif status_filter == "review_ok_no_edit":
        submissions = submissions.filter(format_status="review_ok").filter(
            Q(formatted_pdf_file__isnull=True) | Q(formatted_pdf_file="")
        ).filter(
            Q(formatted_source_file__isnull=True) | Q(formatted_source_file="")
        )
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
    empty_pdf = Q(formatted_pdf_file__isnull=True) | Q(formatted_pdf_file="")
    empty_source = Q(formatted_source_file__isnull=True) | Q(
        formatted_source_file=""
    )
    return _formatting_queryset(query).aggregate(
        needs_attention=Count(
            "pk",
            filter=~Q(format_status="review_ok"),
        ),
        pending=Count("pk", filter=Q(format_status="pending")),
        needs_edit=Count("pk", filter=Q(format_status="needs_edit")),
        review_ok=Count("pk", filter=Q(format_status="review_ok")),
        review_ok_no_edit=Count(
            "pk",
            filter=Q(format_status="review_ok") & empty_pdf & empty_source,
        ),
        edited=Count(
            "pk",
            filter=~empty_pdf | ~empty_source,
        ),
        all=Count("pk"),
    )


def validate_formatting_review_snapshot(submission, snapshot):
    if not _formatting_queryset().filter(pk=submission.pk).exists():
        raise FormattingWorkflowError(
            "This Final Submission is no longer the active publication candidate. "
            "Reload Formatting Review before making changes."
        )
    current = _formatting_review_snapshot(submission)
    if current["updated_at"] != snapshot.get("updated_at"):
        raise FormattingWorkflowError(
            "This formatting record changed after the page was opened. "
            "Reload it and review the current files before saving."
        )
    for key, label in (
        ("publication_pdf", "publication PDF"),
        ("publication_source", "publication source"),
    ):
        if current[key] != snapshot.get(key):
            raise FormattingWorkflowError(
                f"The {label} changed after the page was opened. "
                "Reload the paper and review the current file before saving."
            )
    return current


def _formatting_review_snapshot(submission):
    if not _formatting_queryset().filter(pk=submission.pk).exists():
        raise FormattingWorkflowError(
            "This Final Submission is outside the current publication formatting scope."
        )
    inspection = FileInspectionContext()
    return {
        "created_at": timezone.now().isoformat(),
        "submission_id": submission.pk,
        "updated_at": submission.updated_at.isoformat(),
        "publication_pdf": _publication_file_snapshot(
            publication_pdf_info(submission, inspection, include_url=False),
            inspection,
        ),
        "publication_source": _publication_file_snapshot(
            publication_source_info(submission, inspection, include_url=False),
            inspection,
        ),
    }


def _publication_file_snapshot(info, inspection):
    snapshot = {
        "source": info.get("source", "missing"),
        "exists": bool(info.get("exists")),
        "name": "",
        "signature": None,
    }
    if not info.get("exists") or not info.get("path"):
        return snapshot
    path = Path(info["path"])
    try:
        snapshot["name"] = str(path.relative_to(django_settings.MEDIA_ROOT))
    except ValueError:
        snapshot["name"] = path.name
    status = inspection.status(path)
    snapshot["signature"] = list(status.signature) if status.signature else None
    return snapshot


def _load_session_payloads(session, key, limit):
    now = timezone.now()
    payloads = dict(session.get(key, {}))
    valid = {}
    for token, payload in payloads.items():
        try:
            created_at = datetime.fromisoformat(payload["created_at"])
            if timezone.is_naive(created_at):
                created_at = timezone.make_aware(created_at)
        except (KeyError, TypeError, ValueError):
            continue
        if now - created_at <= FORMATTING_WORKFLOW_TTL:
            valid[token] = payload
    if valid != payloads:
        session[key] = _trim_session_payloads(valid, limit)
        session.modified = True
    return valid


def _trim_session_payloads(payloads, limit):
    return dict(
        sorted(
            payloads.items(),
            key=lambda item: item[1].get("created_at", ""),
        )[-limit:]
    )


def _nearest_queue_candidate(
    ids,
    all_records,
    candidate_ids,
    *,
    reverse_order=False,
):
    candidates = reversed(ids) if reverse_order else ids
    for candidate_id in candidates:
        if candidate_id in candidate_ids and candidate_id in all_records:
            return all_records[candidate_id]
    return None


def _formatting_queue_url(
    token,
    current_submission_id,
    *,
    status_filter="needs_attention",
    query="",
):
    if not token or not current_submission_id:
        return ""
    params = {
        "mode": "single",
        "queue": token,
        "current": current_submission_id,
        "filter": status_filter,
    }
    if query:
        params["q"] = query
    return (
        f"{reverse('submissions:formatting')}?"
        f"{urlencode(params)}"
    )


def formatting_preview_info(
    submission,
    *,
    inspection=None,
    publication_pdf=None,
):
    inspection = inspection or FileInspectionContext()
    publication_pdf = publication_pdf or publication_pdf_info(
        submission,
        inspection,
    )
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
        status = inspection.status(pdf_path)
        signature = hashlib.sha256(
            (
                f"{pdf_path.resolve()}:"
                f"{status.signature}"
            ).encode("utf-8")
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
        submission.title_author_source = (
            "unknown"
            if previous_extraction["title_author_source"] == "manual_override"
            else previous_extraction["title_author_source"]
        )
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

    if submission.format_status == "review_ok":
        inspection = FileInspectionContext()
        source_info = publication_source_info(
            submission,
            inspection,
            include_url=False,
        )
        if not source_info["exists"]:
            raise ValueError(
                "Formatting cannot be marked Review OK without the selected "
                "publication source file."
            )
        submission.source_hash = inspection.sha256(
            source_info["path"],
            fresh=True,
        )
    else:
        submission.source_hash = ""

    submission.save()
    audit_success(
        "formatting_update",
        "Formatting review updated.",
        submission=submission,
        changed_fields=["format_status", "format_notes"],
        reset_flags={
            "pdf_dependent": bool(corrected_pdf),
            "source_dependent": bool(corrected_source and not corrected_pdf),
            "format_review_reset_from_review_ok": bool(has_new_corrected_file and previous_status == "review_ok"),
        },
        file_changes={
            "corrected_pdf": getattr(corrected_pdf, "name", "") if corrected_pdf else "",
            "corrected_source": getattr(corrected_source, "name", "") if corrected_source else "",
        },
        after={
            "format_status": submission.format_status,
            "source_hash": submission.source_hash,
        },
    )
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
