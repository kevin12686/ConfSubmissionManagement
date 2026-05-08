import csv
import re
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.checks import author_count_rows, error_report_rows, split_authors
from submissions.services.file_manager import (
    corrected_pdf_needs_processing,
    publication_pdf_info,
    publication_source_info,
    resolve_folder,
)
from submissions.services.import_export import submissions_to_frame


def _timestamp():
    return timezone.now().strftime("%Y%m%d_%H%M%S")


def _reports_folder():
    return resolve_folder(AppSetting.load().reports_folder)


def _excel_safe_value(value):
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


def error_report_frame():
    return pd.DataFrame(error_report_rows())


def author_count_frame():
    rows = author_count_rows()
    return pd.DataFrame(
        [
            {
                "normalized_author_name": row["normalized_author_name"],
                "display_author_name": row["display_author_name"],
                "paper_count": row["paper_count"],
                "paper_ids": row["paper_ids"],
                "status": row["status"],
            }
            for row in rows
        ]
    )


def export_active_versions():
    path = _reports_folder() / f"active_final_versions_{_timestamp()}.xlsx"
    frame = submissions_to_frame(FinalSubmission.objects.filter(active_version=True))
    _excel_safe_frame(frame).to_excel(path, index=False)
    return path


def export_old_versions():
    path = _reports_folder() / f"old_versions_{_timestamp()}.xlsx"
    frame = submissions_to_frame(FinalSubmission.objects.filter(active_version=False))
    _excel_safe_frame(frame).to_excel(path, index=False)
    return path


def export_error_report():
    path = _reports_folder() / f"error_report_{_timestamp()}.xlsx"
    _excel_safe_frame(error_report_frame()).to_excel(path, index=False)
    return path


def export_author_count():
    path = _reports_folder() / f"author_count_{_timestamp()}.xlsx"
    _excel_safe_frame(author_count_frame()).to_excel(path, index=False)
    return path


def _publication_title_filename(title, word_limit):
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    if not words:
        return "UNTITLED"
    cleaned = " ".join(words[:word_limit]).strip()
    return cleaned or "UNTITLED"


def export_publication_package():
    settings_obj = AppSetting.load()
    submissions = list(
        FinalSubmission.objects.filter(active_version=True).order_by(
            "paper_id_filled", "final_submission_id"
        )
    )
    if not submissions:
        raise ValueError("Publication package blocked because there are no active final submissions.")

    missing = []
    blocked = []
    package_items = []
    for submission in submissions:
        pdf_info = publication_pdf_info(submission)
        source_info = publication_source_info(submission)
        if not pdf_info["exists"]:
            missing.append(f"{submission.paper_id_filled or submission.final_submission_id}: missing PDF")
        if not source_info["exists"]:
            missing.append(f"{submission.paper_id_filled or submission.final_submission_id}: missing source")
        if corrected_pdf_needs_processing(submission):
            blocked.append(
                f"{submission.paper_id_filled or submission.final_submission_id}: corrected PDF needs Process PDFs"
            )
        package_items.append((submission, pdf_info, source_info))

    blockers = missing + blocked
    if blockers:
        preview = "; ".join(blockers[:8])
        if len(blockers) > 8:
            preview += f"; +{len(blockers) - 8} more"
        raise ValueError(f"Publication package blocked because publication files are not ready: {preview}")

    timestamp = _timestamp()
    reports_folder = _reports_folder()
    zip_path = reports_folder / f"publication_package_{timestamp}.zip"
    manifest_name = f"publication_manifest_{timestamp}.csv"
    manifest_path = reports_folder / manifest_name

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
                "Similarity (P)": submission.similarity_score,
                "Similarity (S)": submission.single_similarity_score,
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

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as package:
        package.write(manifest_path, arcname=manifest_name)
        for submission, pdf_info, source_info in package_items:
            short_title = _publication_title_filename(
                submission.extracted_title,
                settings_obj.title_words_for_filename,
            )
            base_name = f"{submission.paper_id_filled}-{short_title}"
            package.write(pdf_info["path"], arcname=f"PDF/{base_name}.pdf")
            source_path = Path(source_info["path"])
            source_extension = source_path.suffix or ".source"
            package.write(source_path, arcname=f"Source/{base_name}{source_extension}")

    return zip_path


def export_all_reports():
    path = _reports_folder() / f"all_reports_{_timestamp()}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        active_frame = submissions_to_frame(FinalSubmission.objects.filter(active_version=True))
        old_frame = submissions_to_frame(FinalSubmission.objects.filter(active_version=False))
        _excel_safe_frame(active_frame).to_excel(
            writer, sheet_name="Active Versions", index=False
        )
        _excel_safe_frame(old_frame).to_excel(
            writer, sheet_name="Old Versions", index=False
        )
        _excel_safe_frame(error_report_frame()).to_excel(writer, sheet_name="Error Report", index=False)
        _excel_safe_frame(author_count_frame()).to_excel(writer, sheet_name="Author Count", index=False)
    return Path(path)
