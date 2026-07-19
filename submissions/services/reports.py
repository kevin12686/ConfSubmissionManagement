import csv
import hashlib
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
from submissions.services.file_manager import (
    publication_file_base_name,
    publication_pdf_info,
    publication_source_info,
    resolve_folder,
)
from submissions.services.import_export import submissions_to_frame
from submissions.services.audit import audit_blocked, audit_failure, audit_success
from submissions.services.publication_read import PublicationReadContext
from submissions.services.version_history import old_version_rows


class PublicationPackageBlocked(ValueError):
    def __init__(self, message, blockers=None):
        super().__init__(message)
        self.blockers = blockers or []


_DRAFT_UNSAFE_CATEGORIES = {
    "Multiple Active Final Submissions",
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


def error_report_frame():
    return pd.DataFrame(error_report_rows())


def author_count_frame():
    rows = author_count_rows()
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


def export_active_versions():
    path = _reports_folder() / f"active_publishable_versions_{_timestamp()}.xlsx"
    frame = submissions_to_frame(
        FinalSubmission.objects.filter(
            active_version=True, excluded_from_publication=False, discarded=False
        )
    )
    _excel_safe_frame(frame).to_excel(path, index=False)
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
    _excel_safe_frame(frame).to_excel(path, index=False)
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
    _excel_safe_frame(frame).to_excel(path, index=False)
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
    _excel_safe_frame(frame).to_excel(path, index=False)
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


def export_publication_package(force=False):
    generated_paths = []
    try:
        rebuild_paper_authors()
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
        manifest_name = f"publication_manifest_{timestamp}.csv"
        manifest_path = reports_folder / manifest_name
        warnings_name = f"publication_package_warnings_{timestamp}.csv"
        warnings_path = reports_folder / warnings_name
        generated_paths.extend([zip_path, manifest_path])
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

        with manifest_path.open("w", newline="", encoding="utf-8-sig") as manifest_file:
            writer = csv.DictWriter(
                manifest_file,
                fieldnames=[
                    "ID",
                    "Extracted Title",
                    "Extracted Author",
                    "Author Number",
                    "Page Number",
                    "Similarity (P)",
                    "Similarity (S)",
                ],
            )
            writer.writeheader()
            writer.writerows(manifest_rows)

        if force:
            warning_rows = _publication_warning_rows(readiness_blockers, skipped_rows)
            with warnings_path.open("w", newline="", encoding="utf-8-sig") as warnings_file:
                writer = csv.DictWriter(
                    warnings_file,
                    fieldnames=["Type", "Paper ID", "Final ID", "Category", "Message"],
                )
                writer.writeheader()
                writer.writerows(warning_rows)

        with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as package:
            package.write(manifest_path, arcname=manifest_name)
            if force:
                package.write(warnings_path, arcname=warnings_name)
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
                package.writestr(f"PDF/{base_name}.pdf", pdf_bytes)
                source_path = Path(source_info["path"])
                source_extension = source_path.suffix or ".source"
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
                package.writestr(
                    f"Source/{base_name}{source_extension}",
                    source_bytes,
                )

        publication_context.assert_database_unchanged()
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


def export_all_reports():
    rebuild_paper_authors()
    path = _reports_folder() / f"editorial_review_workbook_{_timestamp()}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        active_frame = submissions_to_frame(
            FinalSubmission.objects.filter(
                active_version=True, excluded_from_publication=False, discarded=False
            )
        )
        old_frame = old_versions_frame()
        _excel_safe_frame(error_report_frame()).to_excel(
            writer, sheet_name="Readiness Issues", index=False
        )
        _excel_safe_frame(active_frame).to_excel(
            writer, sheet_name="Active Publishable", index=False
        )
        _excel_safe_frame(paper_master_frame()).to_excel(
            writer, sheet_name="Paper Master", index=False
        )
        _excel_safe_frame(not_publishing_frame()).to_excel(
            writer, sheet_name="Not Publishing", index=False
        )
        _excel_safe_frame(author_count_frame()).to_excel(
            writer, sheet_name="Author Count", index=False
        )
        _excel_safe_frame(old_frame).to_excel(writer, sheet_name="Old Versions", index=False)
    audit_success(
        "export_editorial_review_workbook",
        "Editorial review workbook exported.",
        file_changes={"path": str(path)},
    )
    return Path(path)
