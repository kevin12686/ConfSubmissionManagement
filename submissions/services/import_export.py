from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from django.db import transaction
from django.utils import timezone

from submissions.models import (
    AppSetting,
    FinalSubmission,
    InitialPaper,
)
from submissions.services.checks import clean_identifier, resolve_official_paper_id
from submissions.services.final_submission_state import (
    defer_submission_state_sync,
    sync_all_submission_state_records,
)
from submissions.services.file_manager import (
    corrected_pdf_needs_processing,
    publication_pdf_info,
    publication_source_info,
)
from submissions.services.pdf_processor import final_submission_sort_key
from submissions.services.text_utils import clean_note_text


MASTER_SHEET_NAME = "我們自己準備的表格"
START2_SHEET_NAME = "Start2導出的資料"
MAPPING_SHEET_NAME = "Mapping Table"


def is_excel_file(uploaded_file):
    name = getattr(uploaded_file, "name", str(uploaded_file)).lower()
    return name.endswith(".xlsx") or name.endswith(".xls")


def read_table(uploaded_file, preferred_sheet_names=None):
    name = getattr(uploaded_file, "name", str(uploaded_file)).lower()
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    if is_excel_file(uploaded_file):
        if preferred_sheet_names:
            excel = pd.ExcelFile(uploaded_file)
            for sheet_name in preferred_sheet_names:
                if sheet_name in excel.sheet_names:
                    return pd.read_excel(excel, sheet_name=sheet_name).fillna("")
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
        return pd.read_excel(uploaded_file).fillna("")
    return read_csv_with_encoding_fallback(uploaded_file).fillna("")


def read_csv_with_encoding_fallback(uploaded_file):
    encodings = ["utf-8-sig", "utf-8", "cp950", "big5", "gb18030", "cp1252", "latin1"]
    last_error = None
    for encoding in encodings:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        try:
            return pd.read_csv(uploaded_file, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise UnicodeDecodeError(
        last_error.encoding if last_error else "unknown",
        last_error.object if last_error else b"",
        last_error.start if last_error else 0,
        last_error.end if last_error else 0,
        "Could not decode CSV using supported encodings.",
    )


def normalize_columns(frame):
    frame = frame.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    return frame


def clean_value(value):
    if pd.isna(value):
        return ""
    return clean_identifier(value)


def parse_decimal(value):
    value = clean_value(value).replace("%", "").strip()
    if value == "":
        return None
    if value.startswith("<"):
        return Decimal("1")
    try:
        return round_percent(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def round_percent(value):
    if value is None:
        return None
    try:
        return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def parse_upload_date(value):
    if pd.isna(value) or clean_value(value) == "":
        return timezone.now()
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return timezone.now()
    date_value = parsed.to_pydatetime()
    if timezone.is_naive(date_value):
        try:
            app_timezone = ZoneInfo(AppSetting.load().time_zone)
        except ZoneInfoNotFoundError:
            app_timezone = ZoneInfo("America/Chicago")
        return timezone.make_aware(date_value, app_timezone)
    return date_value


INITIAL_PAPER_TEMPLATE_COLUMNS = [
    "paper_id",
    "acceptance_status",
    "title",
    "authors",
    "notes",
]

FINAL_SUBMISSION_TEMPLATE_COLUMNS = [
    "final_submission_id",
    "author_entered_paper_id",
    "final_submission_title",
    "final_submission_authors",
    "upload_date",
    "uploaded_fields",
]

EXTERNAL_RESULTS_TEMPLATE_COLUMNS = [
    "final_submission_id",
    "paper_id",
    "extracted_title",
    "extracted_authors",
    "plagiarism_status",
    "similarity_score",
    "single_similarity_score",
    "plagiarism_report_path",
]


def import_initial_papers(uploaded_file):
    frame = normalize_columns(read_table(uploaded_file, [MASTER_SHEET_NAME]))
    return _import_initial_frame(frame)


@transaction.atomic
@defer_submission_state_sync()
def import_final_submissions(uploaded_file, submission_files=None):
    if is_excel_file(uploaded_file):
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        excel = pd.ExcelFile(uploaded_file)
        if MAPPING_SHEET_NAME in excel.sheet_names:
            return import_mapping_workbook(uploaded_file)

    frame = normalize_columns(read_table(uploaded_file, [START2_SHEET_NAME]))
    created = 0
    updated = 0
    attached_pdfs = 0
    attached_sources = 0
    file_lookup = _build_submission_file_lookup(submission_files or [])
    for row in frame.to_dict("records"):
        final_id = clean_value(row.get("final_submission_id") or row.get("submission id"))
        if not final_id:
            continue
        upload_date = parse_upload_date(row.get("upload_date"))
        raw_paper_id = clean_value(
            row.get("start2_paper_id_raw")
            or row.get("author_entered_paper_id")
            or row.get("paper-id")
            or row.get("paper_id_filled")
            or row.get("paper_id")
        )
        final_submission_title = clean_value(row.get("final_submission_title") or row.get("title"))
        defaults = {
            "start2_paper_id_raw": raw_paper_id,
            "paper_id_filled": resolve_official_paper_id(raw_paper_id, final_submission_title),
            "auto_verify_blocked": False,
            "final_submission_title": final_submission_title,
            "final_submission_authors": clean_value(row.get("final_submission_authors") or row.get("authors")),
            "upload_date": upload_date,
            "original_file_name": clean_value(row.get("pdf_file_name") or row.get("original_file_name")),
            "current_file_path": clean_value(row.get("current_file_path")),
            "extracted_title": clean_value(row.get("extracted_title")),
            "extracted_authors": clean_value(row.get("extracted_authors")),
            "title_author_source": clean_value(row.get("title_author_source")) or "unknown",
            "plagiarism_status": clean_value(row.get("plagiarism_status")),
            "similarity_score": parse_decimal(row.get("similarity_score")),
            "single_similarity_score": parse_decimal(row.get("single_similarity_score")),
            "plagiarism_report_path": clean_value(row.get("plagiarism_report_path")),
        }
        obj, was_created = FinalSubmission.objects.update_or_create(
            final_submission_id=final_id, defaults=defaults
        )
        attachments = _match_submission_files(final_id, file_lookup)
        if attachments["pdf"]:
            _attach_pdf(obj, attachments["pdf"])
            attached_pdfs += 1
        if attachments["source"]:
            _attach_source(obj, attachments["source"])
            attached_sources += 1
        created += int(was_created)
        updated += int(not was_created)
    from submissions.services.recompute import recompute_active_and_duplicate_state

    recompute_active_and_duplicate_state()
    evaluate_imported_submissions()
    return {
        "created": created,
        "updated": updated,
        "attached_pdfs": attached_pdfs,
        "attached_sources": attached_sources,
    }


@transaction.atomic
@defer_submission_state_sync()
def import_mapping_workbook(uploaded_file):
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    excel = pd.ExcelFile(uploaded_file)

    initial_result = {"created": 0, "updated": 0}
    if MASTER_SHEET_NAME in excel.sheet_names:
        master_frame = normalize_columns(
            pd.read_excel(excel, sheet_name=MASTER_SHEET_NAME).fillna("")
        )
        initial_result = _import_initial_frame(master_frame)

    start2_by_final_id = {}
    if START2_SHEET_NAME in excel.sheet_names:
        start2_frame = normalize_columns(
            pd.read_excel(excel, sheet_name=START2_SHEET_NAME).fillna("")
        )
        for row in start2_frame.to_dict("records"):
            final_id = clean_value(row.get("submission id") or row.get("final_submission_id"))
            if final_id:
                start2_by_final_id[final_id] = row

    mapping_frame = pd.read_excel(excel, sheet_name=MAPPING_SHEET_NAME, header=0).fillna("")
    created = 0
    updated = 0
    imported = 0

    for index, row in mapping_frame.iterrows():
        final_id = clean_value(row.iloc[0] if len(row) > 0 else "")
        official_paper_id = clean_value(row.iloc[1] if len(row) > 1 else "")
        if not final_id or not official_paper_id:
            continue

        start2_row = start2_by_final_id.get(final_id, {})
        raw_paper_id = clean_value(start2_row.get("paper-id") or official_paper_id)
        defaults = {
            "start2_paper_id_raw": raw_paper_id,
            "paper_id_filled": official_paper_id,
            "auto_verify_blocked": False,
            "final_submission_title": clean_value(start2_row.get("title")),
            "final_submission_authors": clean_value(start2_row.get("authors")),
            "mapping_source": MAPPING_SHEET_NAME,
            "mapping_order": int(index) + 2,
        }
        file_name = clean_value(row.iloc[14] if len(row) > 14 else "")
        if file_name:
            defaults["original_file_name"] = file_name
        obj, was_created = FinalSubmission.objects.update_or_create(
            final_submission_id=final_id, defaults=defaults
        )
        imported += 1
        created += int(was_created)
        updated += int(not was_created)

    from submissions.services.recompute import recompute_active_and_duplicate_state

    duplicate_count = recompute_active_and_duplicate_state()
    evaluate_imported_submissions()
    return {
        "created": created,
        "updated": updated,
        "imported": imported,
        "duplicates": duplicate_count,
        "initial_created": initial_result["created"],
        "initial_updated": initial_result["updated"],
    }


def _import_initial_frame(frame):
    created = 0
    updated = 0
    for row in frame.to_dict("records"):
        paper_id = clean_value(
            row.get("paper_id") or row.get("submission id") or row.get("initial_submission_id")
        )
        if not paper_id:
            continue
        defaults = {
            "acceptance_status": clean_value(
                row.get("acceptance_status") or row.get("accept status") or row.get("acceptance status")
            ),
            "title": clean_value(row.get("title")),
            "authors": clean_value(row.get("authors")),
            "notes": clean_note_text(row.get("notes")),
        }
        _obj, was_created = InitialPaper.objects.update_or_create(
            paper_id=paper_id, defaults=defaults
        )
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "updated": updated}


def _mark_duplicate_submissions(*, sync_state_records=True):
    setting = AppSetting.load()
    grouped_submissions = defaultdict(list)
    for submission in FinalSubmission.objects.filter(discarded=False).exclude(
        paper_id_filled=""
    ):
        grouped_submissions[submission.paper_id_filled].append(submission)
    duplicate_ids = set()
    for submissions in grouped_submissions.values():
        editor_submissions = [
            submission
            for submission in submissions
            if submission.submission_origin == "editor_upload"
        ]
        if editor_submissions:
            duplicate_ids.update(
                submission.pk
                for submission in submissions
                if submission.submission_origin == "start2"
            )
            submissions = editor_submissions
        if setting.active_version_rule == "upload_date":
            submissions.sort(
                key=lambda submission: (
                    submission.upload_date,
                    final_submission_sort_key(submission),
                )
            )
        else:
            submissions.sort(key=final_submission_sort_key)
        duplicate_ids.update(submission.pk for submission in submissions[:-1])

    FinalSubmission.objects.update(duplicate_submission=False)
    if duplicate_ids:
        FinalSubmission.objects.filter(pk__in=duplicate_ids).update(
            duplicate_submission=True,
            updated_at=timezone.now(),
        )
    if sync_state_records:
        sync_all_submission_state_records(domain_keys={"identity"})
    return len(duplicate_ids)


def _build_submission_file_lookup(submission_files):
    lookup = {}
    for uploaded_file in submission_files:
        name = clean_value(getattr(uploaded_file, "name", ""))
        if not name:
            continue
        parsed = parse_submission_file_name(name)
        if not parsed:
            continue
        final_id, declared_kind = parsed
        actual_kind = classify_uploaded_file(name)
        if actual_kind not in {"pdf", "source"}:
            actual_kind = declared_kind
        lookup.setdefault(final_id, {})[actual_kind] = uploaded_file
    return lookup


def parse_submission_file_name(file_name):
    import re

    match = re.match(
        r"^(?P<final_id>.+?)_file_Submit_(?P<kind>PDF|Source)\..+$",
        file_name,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    kind = match.group("kind").lower()
    return clean_value(match.group("final_id")), "pdf" if kind == "pdf" else "source"


def classify_uploaded_file(file_name):
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if extension == "pdf":
        return "pdf"
    if extension in {
        "doc",
        "docx",
        "tex",
        "zip",
        "rar",
        "7z",
        "tar",
        "gz",
        "bz2",
        "xz",
        "rtf",
        "odt",
    }:
        return "source"
    return "unknown"


def _match_submission_files(final_id, file_lookup):
    matched = file_lookup.get(clean_value(final_id), {})
    return {"pdf": matched.get("pdf"), "source": matched.get("source")}


def _attach_pdf(submission, pdf_file):
    if hasattr(pdf_file, "seek"):
        pdf_file.seek(0)
    submission.pdf_file.save(pdf_file.name, pdf_file, save=False)
    submission.original_file_name = pdf_file.name
    submission.current_file_path = submission.pdf_file.path
    submission.save(
        update_fields=[
            "pdf_file",
            "original_file_name",
            "current_file_path",
            "updated_at",
        ]
    )


def _attach_source(submission, source_file):
    if hasattr(source_file, "seek"):
        source_file.seek(0)
    submission.source_file.save(source_file.name, source_file, save=False)
    submission.source_original_file_name = source_file.name
    submission.source_current_file_path = submission.source_file.path
    submission.save(
        update_fields=[
            "source_file",
            "source_original_file_name",
            "source_current_file_path",
            "updated_at",
        ]
    )


def evaluate_imported_submissions():
    from submissions.services.verification import evaluate_submissions_bulk

    return evaluate_submissions_bulk()


def submissions_to_frame(queryset):
    rows = []
    items = list(queryset)
    master_by_id = {
        paper.paper_id: paper
        for paper in InitialPaper.objects.filter(
            paper_id__in={item.paper_id_filled for item in items if item.paper_id_filled}
        )
    }
    for item in items:
        master = master_by_id.get(item.paper_id_filled)
        publication_pdf = publication_pdf_info(item)
        publication_source = publication_source_info(item)
        rows.append(
            {
                "final_submission_id": item.final_submission_id,
                "start2_paper_id_raw": item.start2_paper_id_raw,
                "author_entered_paper_id": item.start2_paper_id_raw,
                "paper_id_filled": item.paper_id_filled,
                "paper_master_acceptance_status": master.acceptance_status if master else "",
                "paper_master_title": master.title if master else "",
                "paper_master_notes": master.notes if master else "",
                "submission_origin": item.submission_origin,
                "editor_upload_notes": item.editor_upload_notes,
                "editor_uploaded_at": item.editor_uploaded_at,
                "discarded": item.discarded,
                "discard_notes": item.discard_notes,
                "discarded_at": item.discarded_at,
                "excluded_from_publication": item.excluded_from_publication,
                "publication_exclusion_reason": item.publication_exclusion_reason,
                "publication_exclusion_notes": item.publication_exclusion_notes,
                "publication_excluded_at": item.publication_excluded_at,
                "final_submission_title": item.final_submission_title,
                "final_submission_authors": item.final_submission_authors,
                "upload_date": item.upload_date,
                "original_file_name": item.original_file_name,
                "source_original_file_name": item.source_original_file_name,
                "current_file_path": item.current_file_path,
                "source_current_file_path": item.source_current_file_path,
                "publication_pdf_path": publication_pdf["path"],
                "publication_pdf_source": publication_pdf["label"],
                "publication_source_file": publication_source["path"],
                "needs_processing_after_formatting": corrected_pdf_needs_processing(item),
                "extracted_title": item.extracted_title,
                "extracted_authors": item.extracted_authors,
                "page_count": item.page_count,
                "page_limit_exception_approved": item.page_limit_exception_approved,
                "page_limit_exception_reason": item.page_limit_exception_reason,
                "page_limit_exception_page_count": item.page_limit_exception_page_count,
                "pdf_hash": item.pdf_hash,
                "title_author_extraction_status": item.title_author_extraction_status,
                "title_author_extraction_message": item.title_author_extraction_message,
                "title_author_verification_image": item.title_author_verification_image,
                "title_author_manual_override_reason": item.title_author_manual_override_reason,
                "title_author_manual_override_at": item.title_author_manual_override_at,
                "title_author_review_status": item.title_author_review_status,
                "title_author_verified": item.title_author_verified,
                "title_author_verified_at": item.title_author_verified_at,
                "duplicate_author_review_status": item.duplicate_author_review_status,
                "duplicate_author_review_notes": item.duplicate_author_review_notes,
                "duplicate_author_reviewed_at": item.duplicate_author_reviewed_at,
                "author_number_exception_approved": item.author_number_exception_approved,
                "author_number_exception_reason": item.author_number_exception_reason,
                "author_number_exception_author_count": item.author_number_exception_author_count,
                "extracted_title_match_status": item.extracted_title_match_status,
                "extracted_title_match_score": item.extracted_title_match_score,
                "extracted_title_match_message": item.extracted_title_match_message,
                "extracted_title_verified": item.extracted_title_verified,
                "extracted_title_verified_at": item.extracted_title_verified_at,
                "active_version": item.active_version,
                "formatted_pdf_file": item.formatted_pdf_file.name if item.formatted_pdf_file else "",
                "formatted_source_file": item.formatted_source_file.name if item.formatted_source_file else "",
                "edited": item.has_corrected_files,
                "format_status": item.format_status,
                "format_status_label": item.get_format_status_display(),
                "format_notes": item.format_notes,
                "plagiarism_status": item.plagiarism_status,
                "similarity_score": item.similarity_score,
                "single_similarity_score": item.single_similarity_score,
                "plagiarism_percent_exception_approved": item.plagiarism_percent_exception_approved,
                "plagiarism_percent_exception_reason": item.plagiarism_percent_exception_reason,
                "plagiarism_percent_exception_approved_score": (
                    item.plagiarism_percent_exception_approved_score
                ),
                "plagiarism_percent_exception_approved_at": (
                    item.plagiarism_percent_exception_approved_at
                ),
                "single_percent_exception_approved": item.single_percent_exception_approved,
                "single_percent_exception_reason": item.single_percent_exception_reason,
                "single_percent_exception_approved_score": (
                    item.single_percent_exception_approved_score
                ),
                "single_percent_exception_approved_at": item.single_percent_exception_approved_at,
                "plagiarism_report_path": item.plagiarism_report_path,
                "plagiarism_report_stale": item.plagiarism_report_stale,
                "processing_status": item.processing_status,
                "processing_message": item.processing_message,
                "mapping_source": item.mapping_source,
                "mapping_order": item.mapping_order,
                "duplicate_submission": item.duplicate_submission,
                "paper_id_verified": item.paper_id_verified,
                "verification_status": item.verification_status,
                "title_match_score": item.title_match_score,
                "verification_message": item.verification_message,
            }
        )
    return pd.DataFrame(rows)
