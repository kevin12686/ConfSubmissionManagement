import csv
import re
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
    rows = []
    for item in FinalSubmission.objects.filter(
        active_version=True, excluded_from_publication=True
    ).order_by("paper_id_filled", "final_submission_id"):
        rows.append(
            {
                "final_submission_id": item.final_submission_id,
                "author_entered_paper_id": item.start2_paper_id_raw,
                "paper_id_filled": item.paper_id_filled,
                "final_submission_title": item.final_submission_title,
                "final_submission_authors": item.final_submission_authors,
                "reason": item.get_publication_exclusion_reason_display(),
                "notes": item.publication_exclusion_notes,
                "marked_at": item.publication_excluded_at,
            }
        )
    return pd.DataFrame(rows)


def export_active_versions():
    path = _reports_folder() / f"active_publishable_versions_{_timestamp()}.xlsx"
    frame = submissions_to_frame(
        FinalSubmission.objects.filter(
            active_version=True, excluded_from_publication=False
        )
    )
    _excel_safe_frame(frame).to_excel(path, index=False)
    return path


def export_old_versions():
    path = _reports_folder() / f"old_versions_{_timestamp()}.xlsx"
    frame = submissions_to_frame(FinalSubmission.objects.filter(active_version=False))
    _excel_safe_frame(frame).to_excel(path, index=False)
    return path


def export_error_report():
    rebuild_paper_authors()
    path = _reports_folder() / f"readiness_issues_{_timestamp()}.xlsx"
    _excel_safe_frame(error_report_frame()).to_excel(path, index=False)
    return path


def export_author_count():
    rebuild_paper_authors()
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
    rebuild_paper_authors()
    settings_obj = AppSetting.load()
    papers = list(InitialPaper.objects.all().order_by("paper_id"))
    if not papers:
        raise ValueError("Publication package blocked because the Paper Master List is empty.")

    readiness_blockers = publication_readiness_rows()
    if readiness_blockers:
        preview = "; ".join(
            f"{row['paper_id'] or 'No Paper ID'}"
            f"{' / Final ' + str(row['final_submission_id']) if row.get('final_submission_id') else ''}: "
            f"{row['category']}"
            for row in readiness_blockers[:10]
        )
        if len(readiness_blockers) > 10:
            preview += f"; +{len(readiness_blockers) - 10} more"
        raise ValueError(
            "Publication package blocked because publication readiness checks failed: "
            f"{preview}"
        )

    active_by_paper = active_master_submission_map()
    package_items = []
    for paper in papers:
        submission = active_by_paper.get(paper.paper_id)
        if not submission:
            continue
        pdf_info = publication_pdf_info(submission)
        source_info = publication_source_info(submission)
        package_items.append((submission, pdf_info, source_info))

    if not package_items:
        raise ValueError("Publication package blocked because there are no publishable active final submissions.")

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
    rebuild_paper_authors()
    path = _reports_folder() / f"editorial_review_workbook_{_timestamp()}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        active_frame = submissions_to_frame(
            FinalSubmission.objects.filter(
                active_version=True, excluded_from_publication=False
            )
        )
        old_frame = submissions_to_frame(FinalSubmission.objects.filter(active_version=False))
        _excel_safe_frame(error_report_frame()).to_excel(
            writer, sheet_name="Readiness Issues", index=False
        )
        _excel_safe_frame(active_frame).to_excel(
            writer, sheet_name="Active Publishable", index=False
        )
        _excel_safe_frame(not_publishing_frame()).to_excel(
            writer, sheet_name="Not Publishing", index=False
        )
        _excel_safe_frame(author_count_frame()).to_excel(
            writer, sheet_name="Author Count", index=False
        )
        _excel_safe_frame(old_frame).to_excel(writer, sheet_name="Old Versions", index=False)
    return Path(path)
