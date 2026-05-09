from pathlib import Path

from django.db import transaction
from django.utils import timezone

from submissions.models import AppSetting
from submissions.services.checks import rebuild_paper_authors, reset_author_number_exception
from submissions.services.file_manager import resolve_folder, sanitize_filename_part
from submissions.services.import_export import _mark_duplicate_submissions
from submissions.services.import_preview import (
    _archive_and_unlink_corrected_files,
    _reset_extracted_title_match,
    _reset_pdf_dependent_state,
    _reset_source_dependent_state,
)
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.title_author_extraction import evaluate_extracted_title_match
from submissions.services.verification import evaluate_submission, title_similarity, titles_identical


IDENTITY_FIELDS = {
    "final_submission_id",
    "start2_paper_id_raw",
    "paper_id_filled",
    "final_submission_title",
    "upload_date",
}
EXTRACTED_METADATA_FIELDS = {"extracted_title", "extracted_authors"}
PLAGIARISM_FIELDS = {"similarity_score", "single_similarity_score"}
PUBLISHING_DECISION_FIELDS = {
    "excluded_from_publication",
    "publication_exclusion_reason",
    "publication_exclusion_notes",
}


def _empty_summary():
    return {
        "identity_recalculated": False,
        "pdf_reset": False,
        "source_reset": False,
        "plagiarism_stale": False,
        "active_versions_recalculated": False,
        "corrected_files_archived": False,
        "extracted_metadata_reset": False,
        "review_status_guarded": False,
        "not_publishing_changed": False,
    }


def _reset_identity_review(submission, message):
    submission.paper_id_verified = False
    submission.auto_verify_blocked = False
    submission.verified_at = None
    submission.verification_status = "pending"
    submission.title_match_score = None
    submission.verification_message = message


def _reset_extracted_metadata_review(submission, authors_changed):
    submission.title_author_source = "manual"
    submission.title_author_imported_at = timezone.now()
    submission.title_author_extraction_status = "extracted"
    submission.title_author_extraction_message = "Manually edited."
    submission.title_author_review_status = "pending"
    submission.title_author_verified = False
    submission.title_author_verified_at = None
    submission.duplicate_author_review_status = "pending"
    submission.duplicate_author_review_notes = ""
    submission.duplicate_author_reviewed_at = None
    if authors_changed:
        reset_author_number_exception(submission)
    _reset_extracted_title_match(
        submission,
        "Extracted metadata changed manually; extracted title comparison required again.",
    )


def _write_plagiarism_report(submission, report_file):
    report_dir = resolve_folder(AppSetting.load().plagiarism_reports_folder)
    paper_part = sanitize_filename_part(submission.paper_id_filled or "NO_PAPER_ID")
    final_part = sanitize_filename_part(submission.final_submission_id or "NO_FINAL_ID")
    target = report_dir / f"{paper_part}_{final_part}_report.pdf"
    with target.open("wb") as output:
        for chunk in report_file.chunks():
            output.write(chunk)
    submission.plagiarism_report_path = str(target)
    submission.plagiarism_report_stale = False
    submission.plagiarism_imported_at = timezone.now()


def _guard_review_fields(submission):
    guarded = False
    if submission.title_author_review_status == "review_ok" and (
        not submission.extracted_title.strip() or not submission.extracted_authors.strip()
    ):
        submission.title_author_review_status = "pending"
        submission.title_author_verified = False
        submission.title_author_verified_at = None
        guarded = True

    if submission.duplicate_author_review_status == "review_ok" and not submission.extracted_authors.strip():
        submission.duplicate_author_review_status = "pending"
        submission.duplicate_author_reviewed_at = None
        guarded = True

    if submission.extracted_title_verified:
        final_title = submission.final_submission_title or ""
        extracted_title = submission.extracted_title or ""
        score = title_similarity(final_title, extracted_title)
        manually_verifiable = (
            bool(final_title.strip())
            and bool(extracted_title.strip())
            and (
                titles_identical(final_title, extracted_title)
                or (score is not None and score >= 90)
            )
        )
        if not manually_verifiable:
            submission.extracted_title_verified = False
            submission.extracted_title_verified_at = None
            guarded = True
    return guarded


def _set_saved_file_paths(submission, pdf_changed, source_changed):
    update_fields = []
    if pdf_changed:
        submission.current_file_path = submission.pdf_file.path if submission.pdf_file else ""
        submission.original_file_name = Path(submission.pdf_file.name).name if submission.pdf_file else ""
        update_fields.extend(["current_file_path", "original_file_name"])
    if source_changed:
        submission.source_current_file_path = submission.source_file.path if submission.source_file else ""
        submission.source_original_file_name = (
            Path(submission.source_file.name).name if submission.source_file else ""
        )
        update_fields.extend(["source_current_file_path", "source_original_file_name"])
    if update_fields:
        submission.save(update_fields=update_fields + ["updated_at"])


def _apply_publishing_decision(submission, changed_fields, summary):
    if not (PUBLISHING_DECISION_FIELDS & changed_fields):
        return
    summary["not_publishing_changed"] = True
    if submission.excluded_from_publication:
        submission.publication_excluded_at = submission.publication_excluded_at or timezone.now()
        submission.publication_exclusion_reason = submission.publication_exclusion_reason or "other"
        submission.paper_id_verified = False
        submission.auto_verify_blocked = True
        submission.verified_at = None
        submission.verification_status = "invalid_paper_id"
        submission.verification_message = (
            "Marked Not Publishing; excluded from publication readiness checks."
        )
    else:
        submission.publication_exclusion_reason = ""
        submission.publication_exclusion_notes = ""
        submission.publication_excluded_at = None
        submission.paper_id_verified = False
        submission.auto_verify_blocked = True
        submission.verified_at = None
        submission.verification_status = "pending"
        submission.title_match_score = None
        submission.verification_message = (
            "Not Publishing was undone; Paper ID must be reviewed again before publication."
        )


@transaction.atomic
def apply_final_submission_manual_edit(_submission, form, report_file=None):
    """Apply a FinalSubmission edit with publication-critical reset rules."""
    obj = form.save(commit=False)
    changed_fields = set(form.changed_data)
    report_file = (
        report_file
        if report_file is not None
        else form.cleaned_data.get("plagiarism_report_file")
    )
    summary = _empty_summary()

    pdf_changed = "pdf_file" in changed_fields
    source_changed = "source_file" in changed_fields
    identity_changed = bool(IDENTITY_FIELDS & changed_fields)
    active_version_needs_update = bool(
        {"final_submission_id", "paper_id_filled", "upload_date"} & changed_fields
    )
    extracted_metadata_changed = bool(EXTRACTED_METADATA_FIELDS & changed_fields)

    if identity_changed:
        _reset_identity_review(
            obj,
            "Submission identity changed; Paper ID review was recalculated.",
        )
        summary["identity_recalculated"] = True

    if pdf_changed or source_changed:
        if obj.has_corrected_files:
            _archive_and_unlink_corrected_files(obj)
            summary["corrected_files_archived"] = True
        else:
            obj.formatted_pdf_file = ""
            obj.formatted_source_file = ""
            obj.formatted_pdf_uploaded_at = None
            obj.formatted_source_uploaded_at = None

    if pdf_changed:
        _reset_pdf_dependent_state(
            obj,
            "Original PDF changed manually; run Process PDFs before publication.",
        )
        summary["pdf_reset"] = True
        summary["source_reset"] = source_changed
    elif source_changed:
        _reset_source_dependent_state(obj)
        summary["source_reset"] = True

    if extracted_metadata_changed and not pdf_changed:
        _reset_extracted_metadata_review(obj, "extracted_authors" in changed_fields)
        summary["extracted_metadata_reset"] = True

    if "final_submission_title" in changed_fields and not pdf_changed:
        _reset_extracted_title_match(
            obj,
            "Final Submission title changed; extracted title comparison required again.",
        )

    if PLAGIARISM_FIELDS & changed_fields:
        obj.plagiarism_imported_at = timezone.now()
        if obj.plagiarism_report_path and not report_file:
            obj.plagiarism_report_stale = True
            summary["plagiarism_stale"] = True

    if report_file:
        _write_plagiarism_report(obj, report_file)

    _apply_publishing_decision(obj, changed_fields, summary)
    if _guard_review_fields(obj):
        summary["review_status_guarded"] = True

    obj.save()
    _set_saved_file_paths(obj, pdf_changed, source_changed)

    if identity_changed:
        evaluate_submission(obj, save=True)

    if active_version_needs_update:
        determine_active_versions()
        _mark_duplicate_submissions()
        summary["active_versions_recalculated"] = True
        obj.refresh_from_db()

    should_evaluate_extracted_title = bool(
        {"final_submission_title", "extracted_title", "extracted_title_verified"} & changed_fields
    )
    if should_evaluate_extracted_title and not pdf_changed:
        evaluate_extracted_title_match(obj, save=True)

    if "extracted_authors" in changed_fields or pdf_changed:
        rebuild_paper_authors()

    return obj, summary
