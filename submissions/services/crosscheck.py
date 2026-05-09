import csv
import re
import shutil
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.file_manager import publication_pdf_info, resolve_folder
from submissions.services.import_export import clean_value, normalize_columns, read_table, round_percent


CROSSCHECK_RESULT_TEMPLATE_COLUMNS = [
    "filename",
    "plagiarism_percent",
    "single_percent",
]

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_token(token):
    token = clean_value(token)
    if not token:
        raise ValueError("Batch token is required.")
    if not TOKEN_PATTERN.match(token):
        raise ValueError("Batch token may only contain letters, numbers, underscore, or hyphen.")
    return token


def parse_crosscheck_filename(filename):
    stem = Path(clean_value(filename)).stem
    if "_" not in stem:
        return None, None, "Filename must use PaperID_token format."
    paper_id, token = stem.split("_", 1)
    if not paper_id:
        return None, None, "Paper ID is missing before underscore."
    if not token:
        return None, None, "Token is missing after underscore."
    if "_" in paper_id:
        return None, None, "Paper ID may not contain underscore."
    return paper_id, token, ""


def crosscheck_export_root():
    return resolve_folder("data/crosscheck_upload")


def prepare_crosscheck_upload(token):
    token = validate_token(token)
    target_dir = crosscheck_export_root() / token
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = target_dir / f"crosscheck_manifest_{token}.csv"
    zip_path = target_dir / f"crosscheck_upload_{token}.zip"
    exported = []
    skipped = []

    with manifest_path.open("w", newline="", encoding="utf-8-sig") as manifest_file:
        writer = csv.DictWriter(
            manifest_file,
            fieldnames=[
                "paper_id",
                "final_submission_id",
                "source_publication_pdf",
                "exported_filename",
            ],
        )
        writer.writeheader()
        for submission in FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
        ).order_by("paper_id_filled"):
            paper_id = clean_value(submission.paper_id_filled)
            if not paper_id:
                skipped.append(_skip_row(submission, "Missing Paper ID."))
                continue
            if "_" in paper_id:
                skipped.append(_skip_row(submission, "Paper ID contains underscore."))
                continue
            publication_pdf = publication_pdf_info(submission)
            if not publication_pdf["exists"]:
                skipped.append(_skip_row(submission, "No publication PDF."))
                continue

            exported_filename = f"{paper_id}_{token}.pdf"
            target_pdf = target_dir / exported_filename
            shutil.copy2(publication_pdf["path"], target_pdf)
            exported.append(
                {
                    "paper_id": paper_id,
                    "final_submission_id": submission.final_submission_id,
                    "source_publication_pdf": publication_pdf["path"],
                    "exported_filename": exported_filename,
                }
            )
            writer.writerow(exported[-1])

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in exported:
            archive.write(target_dir / row["exported_filename"], row["exported_filename"])
        archive.write(manifest_path, manifest_path.name)

    return {
        "token": token,
        "target_dir": str(target_dir),
        "zip_path": str(zip_path),
        "download_url": "",
        "manifest_path": str(manifest_path),
        "exported_count": len(exported),
        "skipped_count": len(skipped),
        "skipped": skipped,
    }


def import_crosscheck_results(uploaded_file):
    frame = normalize_columns(read_table(uploaded_file))
    updated = 0
    invalid = []
    unmatched = []

    for index, row in enumerate(frame.to_dict("records"), start=2):
        filename = clean_value(row.get("filename"))
        paper_id, _token, error = parse_crosscheck_filename(filename)
        if error:
            invalid.append({"row": index, "filename": filename, "message": error})
            continue

        submission = _active_submission_for_paper_id(paper_id)
        if not submission:
            unmatched.append({"row": index, "filename": filename, "paper_id": paper_id})
            continue

        plagiarism_percent = _parse_percent(row.get("plagiarism_percent"))
        single_percent = _parse_percent(row.get("single_percent"))
        score_changed = (
            submission.similarity_score != plagiarism_percent
            or submission.single_similarity_score != single_percent
        )
        submission.similarity_score = plagiarism_percent
        submission.single_similarity_score = single_percent
        if score_changed and submission.plagiarism_report_path:
            submission.plagiarism_report_stale = True
        submission.plagiarism_imported_at = timezone.now()
        submission.save(
            update_fields=[
                "similarity_score",
                "single_similarity_score",
                "plagiarism_report_stale",
                "plagiarism_imported_at",
                "updated_at",
            ]
        )
        updated += 1

    return {"updated": updated, "invalid": invalid, "unmatched": unmatched}


def upload_crosscheck_reports(files):
    report_dir = resolve_folder(AppSetting.load().plagiarism_reports_folder)
    updated = 0
    invalid = []
    unmatched = []

    for file_obj in files:
        filename = Path(getattr(file_obj, "name", "")).name
        paper_id, _token, error = parse_crosscheck_filename(filename)
        if error or Path(filename).suffix.lower() != ".pdf":
            invalid.append({"filename": filename, "message": error or "Report must be a PDF."})
            continue
        submission = _active_submission_for_paper_id(paper_id)
        if not submission:
            unmatched.append({"filename": filename, "paper_id": paper_id})
            continue

        target = report_dir / filename
        with target.open("wb") as output:
            for chunk in file_obj.chunks():
                output.write(chunk)
        submission.plagiarism_report_path = str(target)
        submission.plagiarism_report_stale = False
        submission.plagiarism_imported_at = timezone.now()
        submission.save(
            update_fields=[
                "plagiarism_report_path",
                "plagiarism_report_stale",
                "plagiarism_imported_at",
                "updated_at",
            ]
        )
        updated += 1

    return {"updated": updated, "invalid": invalid, "unmatched": unmatched}


def _active_submission_for_paper_id(paper_id):
    return (
        FinalSubmission.objects.filter(
            paper_id_filled=paper_id,
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
        )
        .first()
    )


def _parse_percent(value):
    value = clean_value(value).replace("%", "")
    if value == "":
        return None
    if value.strip().startswith("<"):
        return Decimal("1")
    try:
        return round_percent(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def _skip_row(submission, reason):
    return {
        "paper_id": submission.paper_id_filled,
        "final_submission_id": submission.final_submission_id,
        "reason": reason,
    }
