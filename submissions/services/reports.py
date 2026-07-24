import csv
import hashlib
import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.checks import (
    active_master_submission_map,
    author_count_rows,
    error_report_rows,
    publication_readiness_rows,
    rebuild_paper_authors,
    split_authors,
)
from submissions.services.excel_workbook import write_formatted_workbook
from submissions.services.exceptions import exception_rows
from submissions.services.file_manager import (
    publication_file_base_name,
    publication_pdf_info,
    publication_source_info,
    resolve_folder,
)
from submissions.services.import_export import submissions_to_frame
from submissions.services.audit import audit_blocked, audit_failure, audit_success
from submissions.services.publication_read import PublicationReadContext
from submissions.services.verification import normalize_title
from submissions.services.version_history import old_version_rows


EDITORIAL_WORKBOOK_SUPPORTING_SHEETS = {
    "exception_detail": "Exception Detail",
    "readiness_issues": "Readiness Issues",
    "paper_master": "Paper Master",
    "not_publishing": "Not Publishing",
    "author_count": "Author Count",
}


class PublicationPackageBlocked(ValueError):
    def __init__(self, message, blockers=None):
        super().__init__(message)
        self.blockers = blockers or []


_DRAFT_UNSAFE_CATEGORIES = {
    "Multiple Active Final Submissions",
    "Mixed Not Publishing Decision",
    "Duplicate Publication Filename",
}


def _timestamp():
    return timezone.now().strftime("%Y%m%d_%H%M%S_%f")


def _reports_folder():
    return resolve_folder(AppSetting.load().reports_folder)


def _excel_safe_value(value):
    if pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime) and timezone.is_aware(value):
        try:
            app_timezone = ZoneInfo(AppSetting.load().time_zone)
        except ZoneInfoNotFoundError:
            app_timezone = ZoneInfo("America/Chicago")
        return timezone.localtime(value, app_timezone).replace(tzinfo=None)
    return value


def _excel_safe_frame(frame):
    if frame.empty:
        return frame
    return frame.astype(object).map(_excel_safe_value)


def _whole_percent(value):
    if value is None:
        return ""
    return int(value)


def error_report_frame(*, context=None, author_rows=None):
    return pd.DataFrame(
        error_report_rows(
            context=context,
            author_rows=author_rows,
        )
    )


def author_count_frame(*, context=None, rows=None):
    if rows is None:
        rows = author_count_rows(
            context=context,
            include_file_links=False,
        )
    return pd.DataFrame(
        [
            {
                "normalized_author_name": row["normalized_author_name"],
                "display_author_name": row["display_author_name"],
                "publication_paper_count": row["publication_paper_count"],
                "paper_ids": row["paper_ids"],
                "duplicate_author_papers": row["duplicate_author_papers"],
                "status": row["status"],
                "waiver_valid": row["waiver_valid"],
                "waiver_reason": row["waiver_reason"],
                "waiver_approved_count": row["waiver_approved_count"],
            }
            for row in rows
        ]
    )


def not_publishing_frame():
    columns = [
        "final_submission_id",
        "author_entered_paper_id",
        "paper_id_filled",
        "active_version",
        "version_state",
        "submission_origin",
        "active_replacement_final_id",
        "final_submission_title",
        "final_submission_authors",
        "reason",
        "notes",
        "marked_at",
    ]
    rows = []
    for item in FinalSubmission.objects.filter(
        excluded_from_publication=True, discarded=False
    ).order_by("paper_id_filled", "final_submission_id"):
        active_replacement = (
            FinalSubmission.objects.filter(
                active_version=True,
                discarded=False,
                paper_id_filled=item.paper_id_filled,
            )
            .exclude(pk=item.pk)
            .first()
        )
        rows.append(
            {
                "final_submission_id": item.final_submission_id,
                "author_entered_paper_id": item.start2_paper_id_raw,
                "paper_id_filled": item.paper_id_filled,
                "active_version": item.active_version,
                "version_state": "Current final" if item.active_version else "Inactive old version",
                "submission_origin": item.get_submission_origin_display(),
                "active_replacement_final_id": (
                    active_replacement.final_submission_id if active_replacement else ""
                ),
                "final_submission_title": item.final_submission_title,
                "final_submission_authors": item.final_submission_authors,
                "reason": item.get_publication_exclusion_reason_display(),
                "notes": item.publication_exclusion_notes,
                "marked_at": item.publication_excluded_at,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def paper_master_frame():
    return pd.DataFrame(
        [
            {
                "paper_id": paper.paper_id,
                "acceptance_status": paper.acceptance_status,
                "title": paper.title,
                "authors": paper.authors,
                "notes": paper.notes,
                "created_at": paper.created_at,
                "updated_at": paper.updated_at,
            }
            for paper in InitialPaper.objects.all().order_by("paper_id")
        ]
    )


def _title_comparison_label(paper, submission):
    master_title = (paper.title or "").strip()
    final_title = (
        (submission.final_submission_title or "").strip()
        if submission
        else ""
    )
    extracted_title = (
        (submission.extracted_title or "").strip()
        if submission
        else ""
    )
    titles = {
        "Master": master_title,
        "Final": final_title,
        "Extracted": extracted_title,
    }
    missing = [label for label, value in titles.items() if not value]
    if missing:
        return "Missing " + ", ".join(missing)
    if len(set(titles.values())) == 1:
        return "All titles match"
    normalized = {label: normalize_title(value) for label, value in titles.items()}
    if all(normalized.values()) and len(set(normalized.values())) == 1:
        return "Formatting-only difference"
    differences = [
        f"{label} title differs"
        for label in ("Master", "Final")
        if normalized[label] != normalized["Extracted"]
    ]
    return "; ".join(differences) or "Master and Final titles differ"


def _exception_paper_ids(row, valid_paper_ids):
    candidates = []
    for value in (row.get("paper_id"), row.get("paper_ids")):
        candidates.extend(
            part.strip()
            for part in str(value or "").split(",")
            if part.strip()
        )
    return [
        paper_id
        for paper_id in dict.fromkeys(candidates)
        if paper_id in valid_paper_ids
    ]


def _exception_value_label(row, value):
    if value in (None, ""):
        return ""
    if row.get("type") in {"plagiarism_percent", "single_percent"}:
        return f"{_whole_percent(value)}%"
    return str(value)


def _exception_summary(row):
    current = _exception_value_label(row, row.get("current_value"))
    approved = _exception_value_label(row, row.get("approved_value"))
    details = [row.get("status_label") or row.get("status") or ""]
    if current:
        details.append(f"current {current}")
    if row.get("limit_label"):
        details.append(str(row["limit_label"]))
    if approved:
        details.append(f"approved {approved}")
    summary = f"{row.get('type_label') or row.get('type')}: " + " - ".join(
        detail for detail in details if detail
    )
    if row.get("reason"):
        summary += f"\nReason: {row['reason']}"
    return summary


def exception_detail_frame(rows):
    columns = [
        "Paper ID",
        "Final ID",
        "Exception Type",
        "Subject",
        "Status",
        "Current Value",
        "Limit",
        "Approved Value",
        "Reason",
        "Approved At",
        "Related Paper IDs",
    ]
    return pd.DataFrame(
        [
            {
                "Paper ID": row.get("paper_id") or "",
                "Final ID": row.get("final_submission_id") or "",
                "Exception Type": row.get("type_label") or row.get("type") or "",
                "Subject": row.get("subject") or "",
                "Status": row.get("status_label") or row.get("status") or "",
                "Current Value": row.get("current_value"),
                "Limit": row.get("limit_label") or "",
                "Approved Value": row.get("approved_value"),
                "Reason": row.get("reason") or "",
                "Approved At": row.get("approved_at"),
                "Related Paper IDs": row.get("paper_ids") or row.get("paper_id") or "",
            }
            for row in rows
        ],
        columns=columns,
    )


def publication_detail_frame(
    *,
    context=None,
    author_rows=None,
    readiness_rows=None,
    all_exception_rows=None,
):
    context = context or PublicationReadContext.load()
    author_rows = author_rows or author_count_rows(
        context=context,
        include_file_links=False,
    )
    readiness_rows = (
        readiness_rows
        if readiness_rows is not None
        else publication_readiness_rows(
            context=context,
            author_rows=author_rows,
        )
    )
    if all_exception_rows is None:
        all_exception_rows, _status_filter = exception_rows(
            "all",
            context=context,
            hydrate=False,
        )

    valid_paper_ids = context.valid_paper_ids
    blockers_by_paper = defaultdict(list)
    for row in readiness_rows:
        for paper_id in _exception_paper_ids(row, valid_paper_ids):
            blockers_by_paper[paper_id].append(row)

    exceptions_by_paper = defaultdict(list)
    for row in all_exception_rows:
        for paper_id in _exception_paper_ids(row, valid_paper_ids):
            exceptions_by_paper[paper_id].append(row)

    active_by_paper = defaultdict(list)
    for submission in context.active_submissions:
        if submission.paper_id_filled in valid_paper_ids:
            active_by_paper[submission.paper_id_filled].append(submission)

    columns = [
        "Paper ID",
        "Final ID",
        "Version Origin",
        "Extracted Title",
        "Master Title",
        "Final Title",
        "Title Comparison",
        "Extracted Authors",
        "Number of Authors",
        "Number of Pages",
        "Plagiarism %",
        "Single %",
        "Exceptions",
        "Publication Readiness",
        "Blocking Issues",
    ]
    rows = []
    for paper in sorted(context.papers, key=lambda item: item.paper_id):
        active_group = active_by_paper.get(paper.paper_id, [])
        submission = active_group[0] if len(active_group) == 1 else None
        blockers = blockers_by_paper.get(paper.paper_id, [])
        if paper.paper_id in context.excluded_paper_ids:
            readiness = "Not publishing"
        elif blockers or not submission:
            readiness = "Not ready"
        else:
            readiness = "Ready"
        final_ids = (
            ", ".join(
                sorted(item.final_submission_id for item in active_group)
            )
            if len(active_group) > 1
            else (submission.final_submission_id if submission else "")
        )
        rows.append(
            {
                "Paper ID": paper.paper_id,
                "Final ID": final_ids,
                "Version Origin": (
                    submission.get_submission_origin_display()
                    if submission
                    else ""
                ),
                "Extracted Title": submission.extracted_title if submission else "",
                "Master Title": paper.title,
                "Final Title": (
                    submission.final_submission_title if submission else ""
                ),
                "Title Comparison": _title_comparison_label(paper, submission),
                "Extracted Authors": (
                    submission.extracted_authors if submission else ""
                ),
                "Number of Authors": (
                    len(split_authors(submission.extracted_authors))
                    if submission and submission.extracted_authors
                    else ""
                ),
                "Number of Pages": submission.page_count if submission else "",
                "Plagiarism %": (
                    _whole_percent(submission.similarity_score)
                    if submission
                    else ""
                ),
                "Single %": (
                    _whole_percent(submission.single_similarity_score)
                    if submission
                    else ""
                ),
                "Exceptions": "\n\n".join(
                    _exception_summary(row)
                    for row in exceptions_by_paper.get(paper.paper_id, [])
                ),
                "Publication Readiness": readiness,
                "Blocking Issues": "\n".join(
                    f"{row.get('category')}: {row.get('message')}"
                    for row in blockers
                ),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _write_single_sheet(path, sheet_name, frame):
    return write_formatted_workbook(
        path,
        [(sheet_name, _excel_safe_frame(frame))],
    )


def export_active_versions():
    path = _reports_folder() / f"active_publishable_versions_{_timestamp()}.xlsx"
    frame = submissions_to_frame(
        FinalSubmission.objects.filter(
            active_version=True, excluded_from_publication=False, discarded=False
        )
    )
    _write_single_sheet(path, "Active Raw Data", frame)
    audit_success(
        "export_active_versions",
        "Active publishable versions exported.",
        result_counts={"rows": len(frame)},
        file_changes={"path": str(path)},
    )
    return path


def export_old_versions():
    path = _reports_folder() / f"old_versions_{_timestamp()}.xlsx"
    frame = old_versions_frame()
    _write_single_sheet(path, "Old Versions", frame)
    audit_success(
        "export_old_versions",
        "Old versions exported.",
        result_counts={"rows": len(frame)},
        file_changes={"path": str(path)},
    )
    return path


def old_versions_frame():
    queryset = FinalSubmission.objects.filter(active_version=False)
    frame = submissions_to_frame(queryset)
    if frame.empty:
        return frame
    rows_by_final_id = {
        row["submission"].final_submission_id: row for row in old_version_rows(queryset)
    }
    frame["old_version_status"] = frame["final_submission_id"].map(
        lambda final_id: rows_by_final_id[final_id]["version_status_label"]
    )
    frame["inactive_reason"] = frame["final_submission_id"].map(
        lambda final_id: rows_by_final_id[final_id]["inactive_reason"]
    )
    frame["active_replacement_final_id"] = frame["final_submission_id"].map(
        lambda final_id: (
            rows_by_final_id[final_id]["active_replacement"].final_submission_id
            if rows_by_final_id[final_id]["active_replacement"]
            else ""
        )
    )
    return frame


def export_error_report():
    rebuild_paper_authors()
    path = _reports_folder() / f"readiness_issues_{_timestamp()}.xlsx"
    frame = error_report_frame()
    _write_single_sheet(path, "Readiness Issues", frame)
    audit_success(
        "export_error_report",
        "Readiness issues exported.",
        result_counts={"rows": len(frame)},
        file_changes={"path": str(path)},
    )
    return path


def export_author_count():
    rebuild_paper_authors()
    path = _reports_folder() / f"author_count_{_timestamp()}.xlsx"
    frame = author_count_frame()
    _write_single_sheet(path, "Author Count", frame)
    audit_success(
        "export_author_count",
        "Author count exported.",
        result_counts={"rows": len(frame)},
        file_changes={"path": str(path)},
    )
    return path


def _readiness_blocker_preview(readiness_blockers):
    preview = "; ".join(
        f"{row['paper_id'] or 'No Paper ID'}"
        f"{' / Final ' + str(row['final_submission_id']) if row.get('final_submission_id') else ''}: "
        f"{row['category']}"
        for row in readiness_blockers[:10]
    )
    if len(readiness_blockers) > 10:
        preview += f"; +{len(readiness_blockers) - 10} more"
    return preview


def _publication_warning_rows(readiness_blockers, skipped_rows):
    rows = []
    for blocker in readiness_blockers:
        rows.append(
            {
                "Type": "Readiness blocker",
                "Paper ID": blocker.get("paper_id") or "",
                "Final ID": blocker.get("final_submission_id") or "",
                "Category": blocker.get("category") or "",
                "Message": blocker.get("message") or "",
            }
        )
    for skipped in skipped_rows:
        rows.append(
            {
                "Type": "Skipped from draft package",
                "Paper ID": skipped.get("paper_id") or "",
                "Final ID": skipped.get("final_submission_id") or "",
                "Category": skipped.get("category") or "",
                "Message": skipped.get("message") or "",
            }
        )
    return rows


def _csv_bytes(fieldnames, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _publication_source_extension(path):
    path = Path(path)
    suffixes = path.suffixes
    if len(suffixes) >= 2:
        compound = "".join(suffixes[-2:])
        if compound.casefold() in {".tar.gz", ".tar.bz2", ".tar.xz"}:
            return compound
    return path.suffix or ".source"


def export_publication_package(force=False):
    generated_paths = []
    try:
        publication_context = PublicationReadContext.load(
            require_stable_database=True
        )
        settings_obj = publication_context.settings
        papers = sorted(
            publication_context.papers,
            key=lambda paper: paper.paper_id,
        )
        if not papers:
            exc = PublicationPackageBlocked(
                "Publication package blocked because the Paper Master List is empty."
            )
            audit_blocked("publication_package_export", str(exc), result_counts={"force": force})
            raise exc

        readiness_blockers = publication_readiness_rows(
            context=publication_context,
            strict_hash=True,
        )
        draft_unsafe_blockers = [
            row
            for row in readiness_blockers
            if row.get("category") in _DRAFT_UNSAFE_CATEGORIES
        ]
        if force and draft_unsafe_blockers:
            preview = _readiness_blocker_preview(draft_unsafe_blockers)
            exc = PublicationPackageBlocked(
                "Draft publication package blocked because unresolved version or "
                f"filename ambiguity could select the wrong file: {preview}",
                blockers=draft_unsafe_blockers,
            )
            audit_blocked(
                "publication_package_export",
                str(exc),
                result_counts={
                    "force": force,
                    "blockers": len(draft_unsafe_blockers),
                },
                extra={"blockers": draft_unsafe_blockers[:20]},
            )
            raise exc
        if readiness_blockers and not force:
            preview = _readiness_blocker_preview(readiness_blockers)
            exc = PublicationPackageBlocked(
                "Publication package blocked because publication readiness checks failed: "
                f"{preview}",
                blockers=readiness_blockers,
            )
            audit_blocked(
                "publication_package_export",
                str(exc),
                result_counts={"force": force, "blockers": len(readiness_blockers)},
                extra={"blockers": readiness_blockers[:20]},
            )
            raise exc

        active_by_paper = active_master_submission_map(publication_context)
        package_items = []
        skipped_rows = []
        for paper in papers:
            submission = active_by_paper.get(paper.paper_id)
            if not submission:
                if force:
                    skipped_rows.append(
                        {
                            "paper_id": paper.paper_id,
                            "final_submission_id": "",
                            "category": "Missing Final Submission",
                            "message": "Skipped because this Paper Master record has no active final submission.",
                        }
                    )
                continue
            pdf_info = publication_pdf_info(
                submission,
                publication_context.file_inspection,
            )
            source_info = publication_source_info(
                submission,
                publication_context.file_inspection,
            )
            if force and not pdf_info["exists"]:
                skipped_rows.append(
                    {
                        "paper_id": paper.paper_id,
                        "final_submission_id": submission.final_submission_id,
                        "category": "Missing PDF",
                        "message": "Skipped because no publication PDF file is available.",
                    }
                )
                continue
            if force and not source_info["exists"]:
                skipped_rows.append(
                    {
                        "paper_id": paper.paper_id,
                        "final_submission_id": submission.final_submission_id,
                        "category": "Missing Source File",
                        "message": "Skipped because no publication source file is available.",
                    }
                )
                continue
            package_items.append((submission, pdf_info, source_info))

        if not package_items:
            blockers = readiness_blockers + skipped_rows
            if force:
                exc = PublicationPackageBlocked(
                    "Draft publication package could not be created because no papers have both publication PDF and source files.",
                    blockers=blockers,
                )
                audit_blocked(
                    "publication_package_export",
                    str(exc),
                    result_counts={"force": force, "blockers": len(blockers)},
                    extra={"blockers": blockers[:20]},
                )
                raise exc
            exc = PublicationPackageBlocked(
                "Publication package blocked because there are no publishable active final submissions."
            )
            audit_blocked("publication_package_export", str(exc), result_counts={"force": force})
            raise exc

        timestamp = _timestamp()
        reports_folder = _reports_folder()
        zip_prefix = "publication_package_draft" if force else "publication_package"
        zip_path = reports_folder / f"{zip_prefix}_{timestamp}.zip"
        pending_zip_path = zip_path.with_suffix(f"{zip_path.suffix}.part")
        manifest_name = f"publication_manifest_{timestamp}.csv"
        manifest_path = reports_folder / manifest_name
        warnings_name = f"publication_package_warnings_{timestamp}.csv"
        warnings_path = reports_folder / warnings_name
        generated_paths.extend([pending_zip_path, zip_path, manifest_path])
        if force:
            generated_paths.append(warnings_path)

        manifest_rows = []
        for submission, _pdf_info, _source_info in package_items:
            authors = split_authors(submission.extracted_authors)
            manifest_rows.append(
                {
                    "ID": submission.paper_id_filled,
                    "Extracted Title": submission.extracted_title,
                    "Extracted Author": submission.extracted_authors,
                    "Author Number": len(authors) if submission.extracted_authors else "",
                    "Page Number": submission.page_count,
                    "Similarity (P)": _whole_percent(submission.similarity_score),
                    "Similarity (S)": _whole_percent(submission.single_similarity_score),
                }
            )

        manifest_fieldnames = [
            "ID",
            "Extracted Title",
            "Extracted Author",
            "Author Number",
            "Page Number",
            "Similarity (P)",
            "Similarity (S)",
        ]
        manifest_bytes = _csv_bytes(manifest_fieldnames, manifest_rows)
        manifest_path.write_bytes(manifest_bytes)

        warning_bytes = b""
        if force:
            warning_rows = _publication_warning_rows(readiness_blockers, skipped_rows)
            warning_bytes = _csv_bytes(
                ["Type", "Paper ID", "Final ID", "Category", "Message"],
                warning_rows,
            )
            warnings_path.write_bytes(warning_bytes)

        expected_entry_names = [manifest_name]
        with ZipFile(pending_zip_path, "w", compression=ZIP_DEFLATED) as package:
            package.writestr(manifest_name, manifest_bytes)
            if force:
                package.writestr(warnings_name, warning_bytes)
                expected_entry_names.append(warnings_name)
            for submission, pdf_info, source_info in package_items:
                base_name = publication_file_base_name(
                    submission.paper_id_filled,
                    submission.extracted_title,
                    settings_obj.title_words_for_filename,
                )
                pdf_bytes = publication_context.file_inspection.read_snapshot_bytes(
                    pdf_info["path"]
                )
                if (
                    not force
                    and hashlib.sha256(pdf_bytes).hexdigest() != submission.pdf_hash
                ):
                    raise ValueError(
                        "Publication PDF changed after readiness validation for "
                        f"{submission.paper_id_filled} / Final "
                        f"{submission.final_submission_id}."
                    )
                pdf_entry_name = f"PDF/{base_name}.pdf"
                package.writestr(pdf_entry_name, pdf_bytes)
                expected_entry_names.append(pdf_entry_name)
                source_path = Path(source_info["path"])
                source_extension = _publication_source_extension(source_path)
                source_bytes = publication_context.file_inspection.read_snapshot_bytes(
                    source_path
                )
                if (
                    not force
                    and hashlib.sha256(source_bytes).hexdigest()
                    != submission.source_hash
                ):
                    raise ValueError(
                        "Publication source changed after formatting review for "
                        f"{submission.paper_id_filled} / Final "
                        f"{submission.final_submission_id}."
                    )
                source_entry_name = f"Source/{base_name}{source_extension}"
                package.writestr(source_entry_name, source_bytes)
                expected_entry_names.append(source_entry_name)

        if len(expected_entry_names) != len(set(expected_entry_names)):
            raise ValueError(
                "Publication package entry names are not unique. No package was retained."
            )
        with ZipFile(pending_zip_path, "r") as package:
            actual_entry_names = package.namelist()
            if actual_entry_names != expected_entry_names:
                raise ValueError(
                    "Publication package entries differ from the immutable export plan."
                )
            corrupt_entry = package.testzip()
            if corrupt_entry:
                raise ValueError(
                    f"Publication package verification failed for {corrupt_entry}."
                )
        publication_context.assert_database_unchanged()
        pending_zip_path.replace(zip_path)
        audit_success(
            "publication_package_export",
            "Publication package exported.",
            result_counts={
                "force": force,
                "paper_count": len(package_items),
                "skipped_count": len(skipped_rows),
                "readiness_blockers": len(readiness_blockers),
            },
            file_changes={"zip_path": str(zip_path), "manifest_path": str(manifest_path)},
            extra={"skipped": skipped_rows[:20]},
        )
        return zip_path
    except PublicationPackageBlocked:
        raise
    except Exception as exc:
        for path in generated_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        audit_failure(
            "publication_package_export",
            exc,
            "Publication package export failed.",
            result_counts={"force": force},
        )
        raise


def export_all_reports(*, supporting_sheets=None):
    requested_sheets = set(supporting_sheets or ())
    selected_supporting_sheets = [
        key
        for key in EDITORIAL_WORKBOOK_SUPPORTING_SHEETS
        if key in requested_sheets
    ]
    rebuild_paper_authors()
    path = (
        _reports_folder()
        / f"editorial_publication_workbook_{_timestamp()}.xlsx"
    )
    context = PublicationReadContext.load()
    author_rows = author_count_rows(
        context=context,
        include_file_links=False,
    )
    readiness_rows = publication_readiness_rows(
        context=context,
        author_rows=author_rows,
    )
    all_exception_rows, _status_filter = exception_rows(
        "all",
        context=context,
        hydrate=False,
    )
    sheets = [
        (
            "Publication Detail",
            publication_detail_frame(
                context=context,
                author_rows=author_rows,
                readiness_rows=readiness_rows,
                all_exception_rows=all_exception_rows,
            ),
        ),
    ]
    supporting_frames = {
        "exception_detail": lambda: exception_detail_frame(all_exception_rows),
        "readiness_issues": lambda: error_report_frame(
            context=context,
            author_rows=author_rows,
        ),
        "paper_master": paper_master_frame,
        "not_publishing": not_publishing_frame,
        "author_count": lambda: author_count_frame(
            context=context,
            rows=author_rows,
        ),
    }
    sheets.extend(
        (
            EDITORIAL_WORKBOOK_SUPPORTING_SHEETS[key],
            supporting_frames[key](),
        )
        for key in selected_supporting_sheets
    )
    write_formatted_workbook(
        path,
        [
            (sheet_name, _excel_safe_frame(frame))
            for sheet_name, frame in sheets
        ],
    )
    audit_success(
        "export_editorial_review_workbook",
        "Editorial publication workbook exported.",
        result_counts={
            "publication_rows": len(sheets[0][1]),
            "exception_rows": len(all_exception_rows),
            "supporting_sheets": len(selected_supporting_sheets),
        },
        file_changes={
            "path": str(path),
            "sheets": [sheet_name for sheet_name, _frame in sheets],
        },
    )
    return Path(path)
