import hashlib
import json
import shutil
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings as django_settings
from django.core.files import File
from django.utils import timezone

from submissions.forms import FinalSubmissionForm, InitialPaperForm
from submissions.models import FinalSubmission, InitialPaper
from submissions.services.audit import audit_preview, audit_success
from submissions.services.manual_edit import (
    IDENTITY_FIELDS,
    PLAGIARISM_FIELDS,
    apply_final_submission_manual_edit,
)
from submissions.services.paper_master import apply_initial_paper_manual_edit
from submissions.services.preview_storage import (
    PREVIEW_TOKEN_PATTERN,
    purge_expired_preview_directories,
    save_preview_upload,
)
from submissions.services.workflow_evidence import (
    final_submission_edit_evidence,
    paper_master_edit_evidence,
    require_evidence_token,
)


PREVIEW_TTL = timedelta(hours=2)
FINAL_FILE_FIELDS = ("pdf_file", "source_file", "plagiarism_report_file")


def record_edit_preview_root():
    root = django_settings.BASE_DIR / "data" / "record_edit_previews"
    root.mkdir(parents=True, exist_ok=True)
    purge_expired_preview_directories(root, PREVIEW_TTL)
    return root


def _sha256(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _field_path(value):
    if not value:
        return None
    try:
        path = Path(value.path)
    except (AttributeError, OSError, ValueError):
        path = Path(str(value))
    return path if path.exists() and path.is_file() else None


def _current_file_info(submission, field_name):
    if field_name == "plagiarism_report_file":
        value = submission.plagiarism_report_path
    else:
        value = getattr(submission, field_name)
    path = _field_path(value)
    if not path:
        return {"exists": False, "name": "", "sha256": "", "size": 0}
    return {
        "exists": True,
        "name": path.name,
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _json_form_value(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _values_equal(old, new):
    if isinstance(old, Decimal) or isinstance(new, Decimal):
        if old is None or new is None:
            return old is None and new is None
        return Decimal(str(old)) == Decimal(str(new))
    if isinstance(old, datetime) and isinstance(new, datetime):
        if timezone.is_aware(old) and timezone.is_naive(new):
            new = timezone.make_aware(new, timezone.get_current_timezone())
        elif timezone.is_naive(old) and timezone.is_aware(new):
            old = timezone.make_aware(old, timezone.get_current_timezone())
        # datetime-local controls commonly omit database microseconds. Treat that
        # display precision as unchanged so opening and saving a record does not
        # manufacture an identity change or reset review state.
        return old.replace(microsecond=0) == new.replace(microsecond=0)
    return old == new


def _display_value(value):
    if value is None or value == "":
        return "Empty"
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _model_changes(instance, form, fields):
    changes = []
    changed_fields = []
    for field_name in fields:
        old = getattr(instance, field_name)
        new = form.cleaned_data[field_name]
        if _values_equal(old, new):
            continue
        changed_fields.append(field_name)
        changes.append(
            {
                "field": field_name,
                "label": form.fields[field_name].label or field_name.replace("_", " ").title(),
                "old": _display_value(old),
                "new": _display_value(new),
                "kind": "field",
            }
        )
    return changed_fields, changes


def _write_payload(payload):
    token_root = record_edit_preview_root() / payload["token"]
    token_root.mkdir(parents=True, exist_ok=True)
    (token_root / "payload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def load_record_edit_preview(token, expected_kind=None):
    token = str(token or "").strip()
    if not PREVIEW_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid edit preview token. Review the changes again.")
    token_root = record_edit_preview_root() / token
    payload_path = token_root / "payload.json"
    if token_root.is_symlink() or not payload_path.exists():
        raise ValueError("Edit preview not found. Review the changes again.")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        shutil.rmtree(token_root, ignore_errors=True)
        raise ValueError("Edit preview is invalid. Review the changes again.") from exc
    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at)
    if timezone.now() - created_at > PREVIEW_TTL:
        shutil.rmtree(token_root, ignore_errors=True)
        raise ValueError("Edit preview expired. Review the current record again.")
    if expected_kind and payload.get("kind") != expected_kind:
        raise ValueError("Edit preview does not match this record type.")
    return payload, token_root


def cancel_record_edit_preview(token, *, expected_kind, request=None):
    payload, token_root = load_record_edit_preview(token, expected_kind)
    shutil.rmtree(token_root, ignore_errors=True)
    action = f"{expected_kind}_edit_cancel"
    audit_success(
        action,
        "Record edit preview canceled; no changes were applied.",
        request=request,
        object_type=payload.get("object_type", ""),
        paper_id=payload.get("paper_id", ""),
        final_submission_id=payload.get("final_submission_id", ""),
        changed_fields=payload.get("changed_fields", []),
    )


def _preview_response(payload):
    return {
        "token": payload.get("token", ""),
        "kind": payload.get("kind", ""),
        "title": payload.get("title", "Review changes"),
        "record_label": payload.get("record_label", ""),
        "changes": payload.get("changes", []),
        "impacts": payload.get("impacts", []),
        "changed_fields": payload.get("changed_fields", []),
        "return_url": payload.get("return_url", ""),
        "has_changes": bool(payload.get("changed_fields")),
    }


def preview_initial_paper_edit(paper, form, evidence_token, *, return_url="", request=None):
    paper = InitialPaper.objects.get(pk=paper.pk)
    require_evidence_token(
        evidence_token,
        "paper-master-edit",
        paper_master_edit_evidence(paper),
    )
    changed_fields, changes = _model_changes(
        paper,
        form,
        InitialPaperForm.Meta.fields,
    )
    impacts = []
    blocked = ""
    if "title" in changed_fields:
        affected = FinalSubmission.objects.filter(paper_id_filled=paper.paper_id).count()
        impacts.append(
            {
                "level": "warning",
                "label": "Paper ID review will reset",
                "detail": f"{affected} mapped Final Submission record(s) will require Paper ID review again.",
            }
        )
    if "paper_id" in changed_fields:
        mapped = FinalSubmission.objects.filter(paper_id_filled=paper.paper_id).count()
        if mapped:
            blocked = (
                "Paper ID cannot be renamed while Final Submissions are mapped to it. "
                "Remap those records before changing this ID."
            )
        else:
            impacts.append(
                {
                    "level": "warning",
                    "label": "Publication scope identifier will change",
                    "detail": "The Paper Master ID is the publication-scope key.",
                }
            )
    payload = {
        "token": uuid.uuid4().hex,
        "kind": "paper_master",
        "title": "Review Paper Master Changes",
        "record_label": paper.paper_id,
        "object_type": "InitialPaper",
        "object_id": paper.pk,
        "paper_id": paper.paper_id,
        "final_submission_id": "",
        "created_at": timezone.now().isoformat(),
        "evidence_token": evidence_token,
        "form_data": {
            field: _json_form_value(form.cleaned_data[field])
            for field in InitialPaperForm.Meta.fields
        },
        "changed_fields": changed_fields,
        "changes": changes,
        "impacts": impacts,
        "blocked": blocked,
        "return_url": return_url,
    }
    if changed_fields:
        _write_payload(payload)
    audit_preview(
        "paper_master_edit_preview",
        "Paper Master edit changes reviewed; no changes applied.",
        request=request,
        object_type="InitialPaper",
        paper_id=paper.paper_id,
        changed_fields=changed_fields,
        before={change["field"]: change["old"] for change in changes},
        after={change["field"]: change["new"] for change in changes},
        reset_flags={"paper_id_review": "title" in changed_fields},
        result_counts={"changed_fields": len(changed_fields), "blocked": bool(blocked)},
    )
    response = _preview_response(payload)
    response["blocked"] = blocked
    return response


def _final_form_data(submission, form, changed_fields):
    changed_fields = set(changed_fields)
    return {
        # Preserve the exact persisted value for unchanged fields. In particular,
        # this prevents a datetime-local round trip from becoming form.changed_data
        # when the confirmed edit is reconstructed.
        field: _json_form_value(
            form.cleaned_data[field]
            if field in changed_fields
            else getattr(submission, field)
        )
        for field in FinalSubmissionForm.Meta.fields
        if field not in {"pdf_file", "source_file"}
    }


def _file_change(submission, form, field_name, token_root):
    current = _current_file_info(submission, field_name)
    uploaded = form.files.get(field_name)
    clear_requested = bool(
        field_name in {"pdf_file", "source_file"}
        and form.cleaned_data.get(field_name) is False
    )
    label = form.fields[field_name].label or field_name.replace("_", " ").title()
    if clear_requested:
        if not current["exists"]:
            return None, None
        return (
            {
                "field": field_name,
                "label": label,
                "old": current["name"],
                "new": "Remove file",
                "kind": "file",
            },
            {"action": "clear", "current": current},
        )
    if not uploaded:
        return None, None
    saved = save_preview_upload(uploaded, token_root, field_name)
    if current["exists"] and saved["sha256"] == current["sha256"]:
        Path(saved["path"]).unlink(missing_ok=True)
        return None, None
    return (
        {
            "field": field_name,
            "label": label,
            "old": current["name"] or "No file",
            "new": saved["original_name"],
            "kind": "file",
            "old_hash": current["sha256"][:12],
            "new_hash": saved["sha256"][:12],
        },
        {"action": "replace", "current": current, "upload": saved},
    )


def _final_edit_impacts(submission, changed_fields):
    fields = set(changed_fields)
    impacts = []
    if fields & IDENTITY_FIELDS:
        impacts.append(
            {
                "level": "warning",
                "label": "Paper ID review will be recalculated",
                "detail": "The current verification evidence cannot be reused after an identity change.",
            }
        )
    if "final_submission_title" in fields:
        impacts.append(
            {
                "level": "warning",
                "label": "Extracted title comparison will reset",
                "detail": "The extracted title must be compared with the revised Final Title again.",
            }
        )
    if "final_submission_authors" in fields:
        impacts.append(
            {
                "level": "warning",
                "label": "Title/Author Review will return to Pending",
                "detail": "The reviewed author evidence changed and must be reviewed again.",
            }
        )
    if "pdf_file" in fields:
        impacts.append(
            {
                "level": "danger",
                "label": "PDF-dependent checks will reset",
                "detail": "Processing, thumbnails, title/author, plagiarism, formatting, and PDF-bound exceptions will require fresh evidence.",
            }
        )
    if "source_file" in fields:
        impacts.append(
            {
                "level": "danger",
                "label": "Source-dependent checks will reset",
                "detail": "Title/Author and Formatting review will require fresh evidence.",
            }
        )
    if fields & {"pdf_file", "source_file"} and submission.has_corrected_files:
        impacts.append(
            {
                "level": "danger",
                "label": "Corrected files will be archived and unlinked",
                "detail": "Existing corrected files cannot remain selected after their original evidence changes.",
            }
        )
    if fields & {"final_submission_id", "paper_id_filled", "upload_date"}:
        impacts.append(
            {
                "level": "warning",
                "label": "Active versions will be recalculated",
                "detail": "Only the configured active-version service determines whether this row becomes current.",
            }
        )
    if fields & PLAGIARISM_FIELDS and submission.plagiarism_report_path and "plagiarism_report_file" not in fields:
        impacts.append(
            {
                "level": "warning",
                "label": "Existing plagiarism report will be marked old",
                "detail": "Upload the report matching the revised scores to clear the stale-report note.",
            }
        )
    return impacts


def preview_final_submission_edit(
    submission,
    form,
    evidence_token,
    *,
    return_url="",
    request=None,
):
    submission = FinalSubmission.objects.get(pk=submission.pk)
    require_evidence_token(
        evidence_token,
        "final-submission-edit",
        final_submission_edit_evidence(submission),
    )
    model_fields = [
        field for field in FinalSubmissionForm.Meta.fields
        if field not in {"pdf_file", "source_file"}
    ]
    changed_fields, changes = _model_changes(submission, form, model_fields)
    token = uuid.uuid4().hex
    token_root = record_edit_preview_root() / token
    token_root.mkdir(parents=True, exist_ok=True)
    file_actions = {}
    try:
        for field_name in FINAL_FILE_FIELDS:
            change, action = _file_change(submission, form, field_name, token_root)
            if change:
                changes.append(change)
                changed_fields.append(field_name)
                file_actions[field_name] = action
        payload = {
            "token": token,
            "kind": "final_submission",
            "title": "Review Final Submission Changes",
            "record_label": f"{submission.final_submission_id} / {submission.paper_id_filled or 'No Paper ID'}",
            "object_type": "FinalSubmission",
            "object_id": submission.pk,
            "paper_id": submission.paper_id_filled,
            "final_submission_id": submission.final_submission_id,
            "created_at": timezone.now().isoformat(),
            "evidence_token": evidence_token,
            "form_data": _final_form_data(submission, form, changed_fields),
            "file_actions": file_actions,
            "changed_fields": changed_fields,
            "changes": changes,
            "impacts": _final_edit_impacts(submission, changed_fields),
            "blocked": "",
            "return_url": return_url,
        }
        if changed_fields:
            _write_payload(payload)
        else:
            shutil.rmtree(token_root, ignore_errors=True)
    except Exception:
        shutil.rmtree(token_root, ignore_errors=True)
        raise
    audit_preview(
        "final_submission_edit_preview",
        "Final Submission edit changes reviewed; no changes applied.",
        request=request,
        submission=submission,
        changed_fields=changed_fields,
        before={change["field"]: change["old"] for change in changes},
        after={change["field"]: change["new"] for change in changes},
        reset_flags={impact["label"]: True for impact in payload["impacts"]},
        file_changes={field: action["action"] for field, action in file_actions.items()},
        result_counts={"changed_fields": len(changed_fields)},
    )
    return _preview_response(payload)


def _validated_upload(action, token_root, stack):
    upload = action.get("upload") or {}
    path = Path(upload.get("path", ""))
    try:
        path.relative_to(token_root)
    except ValueError as exc:
        raise ValueError("Edit preview file path is invalid. Review the changes again.") from exc
    if not path.exists() or not path.is_file():
        raise ValueError("Edit preview file is missing. Review the changes again.")
    if path.stat().st_size != upload.get("size") or _sha256(path) != upload.get("sha256"):
        raise ValueError("Edit preview file changed after review. Review the changes again.")
    handle = stack.enter_context(path.open("rb"))
    return File(handle, name=upload.get("original_name") or path.name)


def apply_initial_paper_edit_preview(token, *, request=None):
    payload, token_root = load_record_edit_preview(token, "paper_master")
    try:
        paper = InitialPaper.objects.get(pk=payload["object_id"])
        form = InitialPaperForm(payload["form_data"], instance=paper)
        if not form.is_valid():
            raise ValueError("Edit preview data is no longer valid. Review the form again.")
        # ModelForm validation mutates its in-memory instance. Compare against a
        # fresh persisted row so confirmed changes cannot disappear at apply time.
        current = InitialPaper.objects.get(pk=paper.pk)
        changed_fields, _changes = _model_changes(
            current,
            form,
            InitialPaperForm.Meta.fields,
        )
        if changed_fields != payload.get("changed_fields", []):
            raise ValueError("Paper Master changed after preview. Review the current record again.")
        return apply_initial_paper_manual_edit(
            current,
            form,
            expected_evidence_token=payload["evidence_token"],
            request=request,
        )
    finally:
        shutil.rmtree(token_root, ignore_errors=True)


def apply_final_submission_edit_preview(token, *, request=None):
    payload, token_root = load_record_edit_preview(token, "final_submission")
    try:
        submission = FinalSubmission.objects.get(pk=payload["object_id"])
        require_evidence_token(
            payload["evidence_token"],
            "final-submission-edit",
            final_submission_edit_evidence(submission),
        )
        for field_name, action in payload.get("file_actions", {}).items():
            if _current_file_info(submission, field_name) != action.get("current"):
                raise ValueError(
                    "A current file changed after preview. Review the current record again."
                )
        post_data = dict(payload["form_data"])
        files = {}
        with ExitStack() as stack:
            for field_name, action in payload.get("file_actions", {}).items():
                if action.get("action") == "clear":
                    post_data[f"{field_name}-clear"] = "on"
                elif action.get("action") == "replace":
                    files[field_name] = _validated_upload(action, token_root, stack)
            form = FinalSubmissionForm(post_data, files, instance=submission)
            if not form.is_valid():
                raise ValueError("Edit preview data is no longer valid. Review the form again.")
            current = FinalSubmission.objects.get(pk=submission.pk)
            model_fields = [
                field
                for field in FinalSubmissionForm.Meta.fields
                if field not in {"pdf_file", "source_file"}
            ]
            model_changes, _changes = _model_changes(current, form, model_fields)
            confirmed_changes = set(model_changes) | set(
                payload.get("file_actions", {})
            )
            if confirmed_changes != set(payload.get("changed_fields", [])):
                raise ValueError(
                    "Final Submission changes no longer match the preview. "
                    "Review the current record again."
                )
            result = apply_final_submission_manual_edit(
                current,
                form,
                form.cleaned_data.get("plagiarism_report_file"),
                expected_evidence_token=payload["evidence_token"],
                request=request,
            )
        return result
    finally:
        shutil.rmtree(token_root, ignore_errors=True)
