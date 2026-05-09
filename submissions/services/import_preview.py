import hashlib
import json
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.checks import reset_author_number_exception, reset_page_limit_exception
from submissions.services.checks import resolve_official_paper_id
from submissions.services.import_export import (
    MASTER_SHEET_NAME,
    START2_SHEET_NAME,
    clean_value,
    classify_uploaded_file,
    normalize_columns,
    parse_upload_date,
    parse_submission_file_name,
    read_table,
)
from submissions.services.pdf_processor import determine_active_versions


PREVIEW_TTL_HOURS = 2


def preview_root():
    root = django_settings.BASE_DIR / "data" / "import_previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now():
    return timezone.now()


def _database_signature():
    initial_latest = InitialPaper.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    final_latest = FinalSubmission.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    return {
        "initial_count": InitialPaper.objects.count(),
        "initial_latest": initial_latest.isoformat() if initial_latest else "",
        "final_count": FinalSubmission.objects.count(),
        "final_latest": final_latest.isoformat() if final_latest else "",
    }


def _write_payload(payload):
    token_dir = preview_root() / payload["token"]
    token_dir.mkdir(parents=True, exist_ok=True)
    payload_path = token_dir / "preview.json"
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_preview(token):
    token = clean_value(token)
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("Invalid preview token. Please upload again.")
    payload_path = preview_root() / token / "preview.json"
    if not payload_path.exists():
        raise ValueError("Preview token not found. Please upload again.")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if _now() > timezone.datetime.fromisoformat(payload["expires_at"]):
        raise ValueError("Preview expired. Please upload again.")
    return payload


def _assert_fresh(payload):
    if payload.get("database_signature") != _database_signature():
        raise ValueError("Database changed after this preview was created. Please preview again before applying.")


def _changed(field, label, old, new, reset=""):
    old = old or ""
    new = new or ""
    if old == new:
        return None
    return {"field": field, "label": label, "old": old, "new": new, "reset": reset}


def _coerce_datetime(value):
    if not value:
        return None
    if isinstance(value, str):
        value = timezone.datetime.fromisoformat(value)
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _display_datetime(value):
    date_value = _coerce_datetime(value)
    if not date_value:
        return ""
    return timezone.localtime(date_value).strftime("%Y-%m-%d %H:%M:%S %Z")


def _changed_datetime(field, label, old, new, reset=""):
    old_value = _coerce_datetime(old)
    new_value = _coerce_datetime(new)
    if old_value and new_value and old_value == new_value:
        return None
    if not old_value and not new_value:
        return None
    return {
        "field": field,
        "label": label,
        "old": _display_datetime(old_value),
        "new": _display_datetime(new_value),
        "reset": reset,
    }


def _stats(rows):
    stats = {
        "new": 0,
        "unchanged": 0,
        "metadata_updated": 0,
        "paper_id_review_reset": 0,
        "title_match_reset": 0,
        "title_author_review_reset": 0,
        "pdf_reset": 0,
        "source_reset": 0,
        "corrected_files_archived": 0,
    }
    for row in rows:
        if row.get("status") == "new":
            stats["new"] += 1
        if row.get("status") == "unchanged":
            stats["unchanged"] += 1
        if row.get("changes"):
            stats["metadata_updated"] += 1
        for key in [
            "paper_id_review_reset",
            "title_match_reset",
            "title_author_review_reset",
            "pdf_reset",
            "source_reset",
            "corrected_files_archived",
        ]:
            if row.get(key):
                stats[key] += 1
    return stats


def preview_initial_import(uploaded_file):
    frame = normalize_columns(read_table(uploaded_file, [MASTER_SHEET_NAME]))
    rows = []
    seen_ids = {}
    blocking_errors = []
    for index, row in enumerate(frame.to_dict("records"), start=2):
        paper_id = clean_value(row.get("paper_id") or row.get("submission id") or row.get("initial_submission_id"))
        if not paper_id:
            continue
        if paper_id in seen_ids:
            blocking_errors.append(
                f"Duplicate Paper ID '{paper_id}' in uploaded Master List rows {seen_ids[paper_id]} and {index}. Fix the file before applying."
            )
            continue
        seen_ids[paper_id] = index
        new_values = {
            "paper_id": paper_id,
            "acceptance_status": clean_value(
                row.get("acceptance_status") or row.get("accept status") or row.get("acceptance status")
            ),
            "title": clean_value(row.get("title")),
            "authors": clean_value(row.get("authors")),
        }
        existing = InitialPaper.objects.filter(paper_id=paper_id).first()
        if not existing:
            rows.append(
                {
                    "type": "initial",
                    "status": "new",
                    "identifier": paper_id,
                    "new": new_values,
                    "changes": [],
                    "affected_final_count": 0,
                    "paper_id_review_reset": False,
                    "title_match_reset": False,
                    "pdf_reset": False,
                    "source_reset": False,
                    "corrected_files_archived": False,
                }
            )
            continue
        changes = [
            change
            for change in [
                _changed("acceptance_status", "Accept Status", existing.acceptance_status, new_values["acceptance_status"]),
                _changed(
                    "title",
                    "Master Title",
                    existing.title,
                    new_values["title"],
                    "Paper ID review will reset",
                ),
                _changed("authors", "Master Authors", existing.authors, new_values["authors"]),
            ]
            if change
        ]
        master_title_changed = any(change["field"] == "title" for change in changes)
        affected_count = FinalSubmission.objects.filter(paper_id_filled=paper_id).count() if master_title_changed else 0
        rows.append(
            {
                "type": "initial",
                "status": "changed" if changes else "unchanged",
                "identifier": paper_id,
                "new": new_values,
                "changes": changes,
                "affected_final_count": affected_count,
                "paper_id_review_reset": master_title_changed,
                "title_match_reset": False,
                "pdf_reset": False,
                "source_reset": False,
                "corrected_files_archived": False,
            }
        )
    return _make_payload("initial", rows, blocking_errors=blocking_errors)


def preview_final_import(uploaded_file, submission_files=None):
    frame = normalize_columns(read_table(uploaded_file, [START2_SHEET_NAME]))
    token = uuid.uuid4().hex
    token_dir = preview_root() / token
    upload_lookup, invalid_files = _save_temp_submission_files(submission_files or [], token_dir)
    rows = []
    seen_ids = {}
    blocking_errors = []
    for index, row in enumerate(frame.to_dict("records"), start=2):
        final_id = clean_value(row.get("final_submission_id") or row.get("submission id"))
        if not final_id:
            continue
        if final_id in seen_ids:
            blocking_errors.append(
                f"Duplicate Final ID '{final_id}' in uploaded Final Submission rows {seen_ids[final_id]} and {index}. Fix the file before applying."
            )
            continue
        seen_ids[final_id] = index
        raw_paper_id = clean_value(
            row.get("start2_paper_id_raw")
            or row.get("author_entered_paper_id")
            or row.get("paper-id")
            or row.get("paper_id_filled")
            or row.get("paper_id")
        )
        final_submission_title = clean_value(row.get("final_submission_title") or row.get("title"))
        upload_date_raw = clean_value(row.get("upload_date"))
        new_values = {
            "final_submission_id": final_id,
            "start2_paper_id_raw": raw_paper_id,
            "paper_id_filled": resolve_official_paper_id(raw_paper_id, final_submission_title),
            "final_submission_title": final_submission_title,
            "final_submission_authors": clean_value(row.get("final_submission_authors") or row.get("authors")),
            "upload_date": parse_upload_date(upload_date_raw).isoformat() if upload_date_raw else "",
        }
        existing = FinalSubmission.objects.filter(final_submission_id=final_id).first()
        files = upload_lookup.get(final_id, {})
        if not existing:
            rows.append(_new_final_row(final_id, new_values, files))
            continue
        rows.append(_changed_final_row(existing, new_values, files))
    payload = _make_payload("final", rows, token=token, blocking_errors=blocking_errors)
    payload["invalid_files"] = invalid_files
    return _write_payload(payload)


def _make_payload(kind, rows, token=None, blocking_errors=None):
    created_at = _now()
    payload = {
        "token": token or uuid.uuid4().hex,
        "kind": kind,
        "created_at": created_at.isoformat(),
        "expires_at": (created_at + timedelta(hours=PREVIEW_TTL_HOURS)).isoformat(),
        "database_signature": _database_signature(),
        "rows": rows,
        "blocking_errors": blocking_errors or [],
    }
    payload["stats"] = _stats(rows)
    return _write_payload(payload)


def _new_final_row(final_id, new_values, files):
    pdf_change = "new" if files.get("pdf") else "missing"
    source_change = "new" if files.get("source") else "missing"
    has_pdf = bool(files.get("pdf"))
    has_source = bool(files.get("source"))
    invalid_master_id = bool(
        new_values["paper_id_filled"]
        and not InitialPaper.objects.filter(paper_id=new_values["paper_id_filled"]).exists()
    )
    return {
        "type": "final",
        "status": "new",
        "identifier": final_id,
        "new": new_values,
        "changes": [],
        "file_changes": _file_changes(files, pdf_change, source_change),
        "paper_id_review_reset": True,
        "title_match_reset": has_pdf,
        "title_author_review_reset": has_pdf,
        "pdf_reset": has_pdf,
        "source_reset": has_source,
        "corrected_files_archived": False,
        "author_entered_id_changed": False,
        "invalid_master_id": invalid_master_id,
        "active_version_impact": "Active versions will be recalculated after apply.",
    }


def _changed_final_row(existing, new_values, files):
    possible_changes = [
        _changed(
            "start2_paper_id_raw",
            "Author-entered ID",
            existing.start2_paper_id_raw,
            new_values["start2_paper_id_raw"],
            "Paper ID review will reset",
        ),
        _changed(
            "final_submission_title",
            "Final Title",
            existing.final_submission_title,
            new_values["final_submission_title"],
            "Paper ID review and title match review will reset",
        ),
        _changed("final_submission_authors", "Final Authors", existing.final_submission_authors, new_values["final_submission_authors"]),
    ]
    if new_values["upload_date"]:
        possible_changes.append(
            _changed_datetime(
                "upload_date",
                "Upload Date",
                existing.upload_date,
                new_values["upload_date"],
                "Active version may change if Settings uses upload date.",
            )
        )
    changes = [
        change
        for change in possible_changes
        if change
    ]
    pdf_change = _uploaded_file_status(existing, files.get("pdf"), "pdf")
    source_change = _uploaded_file_status(existing, files.get("source"), "source")
    pdf_changed = pdf_change in {"different", "new"}
    source_changed = source_change in {"different", "new"}
    corrected_archived = bool((pdf_changed or source_changed) and existing.has_corrected_files)
    id_changed = any(change["field"] == "start2_paper_id_raw" for change in changes)
    title_changed = any(change["field"] == "final_submission_title" for change in changes)
    upload_date_changed = any(change["field"] == "upload_date" for change in changes)
    return {
        "type": "final",
        "status": "changed" if changes or pdf_changed or source_changed else "unchanged",
        "identifier": existing.final_submission_id,
        "new": new_values,
        "changes": changes,
        "file_changes": _file_changes(files, pdf_change, source_change),
        "paper_id_review_reset": id_changed or title_changed,
        "title_match_reset": title_changed or pdf_changed or source_changed,
        "title_author_review_reset": pdf_changed or source_changed,
        "pdf_reset": pdf_changed,
        "source_reset": source_changed,
        "corrected_files_archived": corrected_archived,
        "author_entered_id_changed": id_changed,
        "active_version_impact": "Active versions will be recalculated after apply." if id_changed or upload_date_changed else "",
    }


def _file_changes(files, pdf_status, source_status):
    return {
        "pdf": {"status": pdf_status, **(files.get("pdf") or {})},
        "source": {"status": source_status, **(files.get("source") or {})},
    }


def _save_temp_submission_files(submission_files, token_dir):
    upload_dir = token_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    lookup = {}
    invalid = []
    for uploaded_file in submission_files:
        original_name = clean_value(getattr(uploaded_file, "name", ""))
        parsed = parse_submission_file_name(original_name)
        if not parsed:
            invalid.append(original_name)
            continue
        final_id, declared_kind = parsed
        actual_kind = classify_uploaded_file(original_name)
        kind = actual_kind if actual_kind in {"pdf", "source"} else declared_kind
        temp_name = f"{uuid.uuid4().hex}_{original_name}"
        temp_path = upload_dir / temp_name
        digest = hashlib.sha256()
        size = 0
        with temp_path.open("wb") as handle:
            for chunk in uploaded_file.chunks():
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
        lookup.setdefault(final_id, {})[kind] = {
            "original_name": original_name,
            "temp_path": str(temp_path),
            "hash": digest.hexdigest(),
            "size": size,
        }
    return lookup, invalid


def _uploaded_file_status(submission, file_info, kind):
    if not file_info:
        return "not uploaded"
    current_path = _current_original_file_path(submission, kind)
    if not current_path:
        return "new"
    return "same" if _file_hash(current_path) == file_info["hash"] else "different"


def _current_original_file_path(submission, kind):
    if kind == "pdf":
        if submission.pdf_file and Path(submission.pdf_file.path).exists():
            return Path(submission.pdf_file.path)
        if submission.current_file_path and Path(submission.current_file_path).exists():
            return Path(submission.current_file_path)
    if kind == "source":
        if submission.source_file and Path(submission.source_file.path).exists():
            return Path(submission.source_file.path)
        if submission.source_current_file_path and Path(submission.source_current_file_path).exists():
            return Path(submission.source_current_file_path)
    return None


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_import_preview(token):
    payload = load_preview(token)
    _assert_fresh(payload)
    if payload.get("blocking_errors"):
        raise ValueError("Preview has blocking errors. Fix the uploaded file and preview again.")
    if payload["kind"] == "initial":
        result = _apply_initial(payload)
    elif payload["kind"] == "final":
        result = _apply_final(payload)
    else:
        raise ValueError("Unknown preview type.")
    shutil.rmtree(preview_root() / payload["token"], ignore_errors=True)
    return result


@transaction.atomic
def _apply_initial(payload):
    for row in payload["rows"]:
        values = row["new"]
        paper, _created = InitialPaper.objects.update_or_create(
            paper_id=values["paper_id"],
            defaults={
                "acceptance_status": values["acceptance_status"],
                "title": values["title"],
                "authors": values["authors"],
            },
        )
        if row.get("paper_id_review_reset"):
            _reset_paper_id_review_for_paper(paper.paper_id, "Master Title changed; Paper ID review required again.")
    return payload["stats"]


@transaction.atomic
def _apply_final(payload):
    for row in payload["rows"]:
        values = row["new"]
        submission, created = FinalSubmission.objects.get_or_create(
            final_submission_id=values["final_submission_id"]
        )
        if created:
            submission.excluded_from_publication = False
            submission.publication_exclusion_reason = ""
            submission.publication_exclusion_notes = ""
            submission.publication_excluded_at = None
        submission.start2_paper_id_raw = values["start2_paper_id_raw"]
        if created or row.get("author_entered_id_changed"):
            submission.paper_id_filled = values["paper_id_filled"]
        submission.final_submission_title = values["final_submission_title"]
        submission.final_submission_authors = values["final_submission_authors"]
        if values.get("upload_date"):
            submission.upload_date = timezone.datetime.fromisoformat(values["upload_date"])
        if row.get("paper_id_review_reset"):
            _reset_paper_id_review(submission, "Imported metadata changed; Paper ID review required again.")
        if row.get("title_match_reset"):
            _reset_extracted_title_match(submission, "Final title or PDF changed; extracted title comparison required again.")
        if row.get("pdf_reset"):
            _reset_pdf_dependent_state(
                submission,
                "New PDF uploaded; needs Process PDFs."
                if row.get("status") == "new"
                else "PDF changed; needs Process PDFs.",
            )
        if row.get("source_reset"):
            _reset_source_dependent_state(submission)
        if row.get("corrected_files_archived"):
            _archive_and_unlink_corrected_files(submission)
        submission.save()
        file_changes = row.get("file_changes", {})
        if file_changes.get("pdf", {}).get("status") in {"new", "different"}:
            _attach_file(submission, file_changes["pdf"], "pdf")
        if file_changes.get("source", {}).get("status") in {"new", "different"}:
            _attach_file(submission, file_changes["source"], "source")
    determine_active_versions()
    from submissions.services.import_export import _mark_duplicate_submissions, evaluate_imported_submissions
    from submissions.services.checks import rebuild_paper_authors

    _mark_duplicate_submissions()
    evaluate_imported_submissions()
    rebuild_paper_authors()
    return payload["stats"]


def _reset_paper_id_review_for_paper(paper_id, message):
    for submission in FinalSubmission.objects.filter(paper_id_filled=paper_id):
        _reset_paper_id_review(submission, message)
        submission.save()


def _reset_paper_id_review(submission, message):
    submission.paper_id_verified = False
    submission.auto_verify_blocked = False
    submission.verified_at = None
    submission.verification_status = "pending"
    submission.title_match_score = None
    submission.verification_message = message


def _reset_extracted_title_match(submission, message):
    submission.extracted_title_verified = False
    submission.extracted_title_auto_verify_blocked = True
    submission.extracted_title_verified_at = None
    submission.extracted_title_match_status = "pending"
    submission.extracted_title_match_score = None
    submission.extracted_title_match_message = message


def _reset_pdf_dependent_state(submission, processing_message="PDF changed; needs Process PDFs."):
    submission.page_count = None
    reset_page_limit_exception(submission)
    submission.pdf_hash = ""
    submission.thumbnail_folder = ""
    submission.thumbnail_status = ""
    submission.thumbnail_message = ""
    submission.processing_status = "pending"
    submission.processing_message = processing_message
    submission.extracted_title = ""
    submission.extracted_authors = ""
    submission.title_author_source = "unknown"
    submission.title_author_imported_at = None
    submission.title_author_extraction_status = "pending"
    submission.title_author_extraction_message = ""
    submission.title_author_verification_image = ""
    submission.title_author_review_status = "pending"
    submission.title_author_verified = False
    submission.title_author_verified_at = None
    submission.duplicate_author_review_status = "pending"
    submission.duplicate_author_review_notes = ""
    submission.duplicate_author_reviewed_at = None
    reset_author_number_exception(submission)
    _reset_extracted_title_match(submission, "PDF changed; extracted title comparison required again.")
    submission.plagiarism_status = ""
    submission.similarity_score = None
    submission.single_similarity_score = None
    submission.plagiarism_report_path = ""
    submission.plagiarism_report_stale = False
    submission.plagiarism_imported_at = None
    submission.format_status = "pending"


def _reset_source_dependent_state(submission):
    submission.format_status = "pending"
    submission.title_author_review_status = "pending"
    submission.title_author_verified = False
    submission.title_author_verified_at = None
    submission.duplicate_author_review_status = "pending"
    submission.duplicate_author_review_notes = ""
    submission.duplicate_author_reviewed_at = None
    _reset_extracted_title_match(
        submission,
        "Source file changed; title/author review required again.",
    )


def _archive_and_unlink_corrected_files(submission):
    archive_dir = (
        django_settings.MEDIA_ROOT
        / "invalidated_corrected_files"
        / clean_value(submission.final_submission_id)
        / _now().strftime("%Y%m%d_%H%M%S")
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    for field_name in ["formatted_pdf_file", "formatted_source_file"]:
        field_file = getattr(submission, field_name)
        if field_file:
            field_path = Path(field_file.path)
            if field_path.exists():
                shutil.move(str(field_path), archive_dir / field_path.name)
            setattr(submission, field_name, "")
    submission.formatted_pdf_uploaded_at = None
    submission.formatted_source_uploaded_at = None


def _attach_file(submission, file_info, kind):
    path = Path(file_info["temp_path"])
    with path.open("rb") as handle:
        if kind == "pdf":
            submission.pdf_file.save(file_info["original_name"], File(handle), save=False)
            submission.original_file_name = file_info["original_name"]
            submission.current_file_path = submission.pdf_file.path
        else:
            submission.source_file.save(file_info["original_name"], File(handle), save=False)
            submission.source_original_file_name = file_info["original_name"]
            submission.source_current_file_path = submission.source_file.path
    submission.save()
