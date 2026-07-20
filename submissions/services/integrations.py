from django.db import transaction
from django.utils import timezone

from submissions.models import FinalSubmission
from submissions.services.audit import audit_success
from submissions.services.checks import rebuild_paper_authors, reset_author_number_exception
from submissions.services.final_submission_state import bulk_update_submissions
from submissions.services.import_export import clean_value, normalize_columns, parse_decimal, read_table
from submissions.services.import_preview import _clear_title_author_manual_override


def find_submission(row):
    final_id = clean_value(row.get("final_submission_id"))
    if final_id:
        return (
            FinalSubmission.objects.select_for_update()
            .filter(final_submission_id=final_id)
            .first()
        )

    paper_id = clean_value(row.get("paper_id") or row.get("paper_id_filled"))
    if paper_id:
        candidates = list(
            FinalSubmission.objects.select_for_update()
            .filter(
                paper_id_filled=paper_id,
                active_version=True,
                discarded=False,
                excluded_from_publication=False,
            )
            .order_by("pk")[:2]
        )
        if len(candidates) > 1:
            raise ValueError(
                f"Paper ID {paper_id} has multiple active Final Submissions. "
                "Resolve version state before importing external results."
            )
        return candidates[0] if candidates else None
    return None


@transaction.atomic
def import_external_results(uploaded_file):
    frame = normalize_columns(read_table(uploaded_file))
    updated_title_author = 0
    updated_plagiarism = 0
    unmatched = 0
    changed_submissions = []
    changed_by_id = {}
    imported_row_by_submission = {}

    for row_number, row in enumerate(frame.to_dict("records"), start=2):
        submission = find_submission(row)
        if not submission:
            unmatched += 1
            continue
        previous_row = imported_row_by_submission.get(submission.pk)
        if previous_row is not None:
            raise ValueError(
                f"External result rows {previous_row} and {row_number} both "
                f"target Final Submission {submission.final_submission_id}. "
                "Keep exactly one row per Final Submission."
            )
        imported_row_by_submission[submission.pk] = row_number
        submission = changed_by_id.setdefault(submission.pk, submission)

        title_author_changed = False
        plagiarism_changed = False
        plagiarism_score_changed = False
        plagiarism_report_changed = False

        if "extracted_title" in row:
            submission.extracted_title = clean_value(row.get("extracted_title"))
            title_author_changed = True
        if "extracted_authors" in row:
            submission.extracted_authors = clean_value(row.get("extracted_authors"))
            title_author_changed = True
        if title_author_changed:
            submission.title_author_source = "external_import"
            submission.title_author_imported_at = timezone.now()
            submission.title_author_extraction_status = "extracted"
            submission.title_author_extraction_message = "Imported from external results file."
            _clear_title_author_manual_override(submission)
            submission.title_author_review_status = "pending"
            submission.title_author_verified = False
            submission.title_author_verified_at = None
            submission.duplicate_author_review_status = "pending"
            submission.duplicate_author_review_notes = ""
            submission.duplicate_author_reviewed_at = None
            if "extracted_authors" in row:
                reset_author_number_exception(submission)
            submission.extracted_title_verified = False
            submission.extracted_title_verified_at = None
            submission.extracted_title_auto_verify_blocked = False
            updated_title_author += 1

        if "plagiarism_status" in row:
            submission.plagiarism_status = clean_value(row.get("plagiarism_status"))
            plagiarism_changed = True
        if "similarity_score" in row:
            similarity_score = parse_decimal(row.get("similarity_score"))
            if submission.similarity_score != similarity_score:
                plagiarism_score_changed = True
            submission.similarity_score = similarity_score
            plagiarism_changed = True
        if "single_similarity_score" in row:
            single_similarity_score = parse_decimal(row.get("single_similarity_score"))
            if submission.single_similarity_score != single_similarity_score:
                plagiarism_score_changed = True
            submission.single_similarity_score = single_similarity_score
            plagiarism_changed = True
        if "plagiarism_report_path" in row:
            submission.plagiarism_report_path = clean_value(row.get("plagiarism_report_path"))
            plagiarism_changed = True
            plagiarism_report_changed = True
        if plagiarism_changed:
            if plagiarism_report_changed:
                submission.plagiarism_report_stale = False
            elif plagiarism_score_changed and submission.plagiarism_report_path:
                submission.plagiarism_report_stale = True
            submission.plagiarism_imported_at = timezone.now()
            updated_plagiarism += 1

        changed_submissions.append(submission)

    bulk_update_submissions(
        changed_submissions,
        [
            "extracted_title",
            "extracted_authors",
            "title_author_source",
            "title_author_imported_at",
            "title_author_extraction_status",
            "title_author_extraction_message",
            "title_author_verification_image",
            "title_author_manual_override_reason",
            "title_author_manual_override_at",
            "title_author_review_status",
            "title_author_verified",
            "title_author_verified_at",
            "duplicate_author_review_status",
            "duplicate_author_review_notes",
            "duplicate_author_reviewed_at",
            "author_number_exception_approved",
            "author_number_exception_reason",
            "author_number_exception_author_count",
            "author_number_exception_approved_at",
            "extracted_title_match_status",
            "extracted_title_match_score",
            "extracted_title_match_message",
            "extracted_title_verified",
            "extracted_title_auto_verify_blocked",
            "extracted_title_verified_at",
            "plagiarism_status",
            "similarity_score",
            "single_similarity_score",
            "plagiarism_report_path",
            "plagiarism_report_stale",
            "plagiarism_imported_at",
        ],
    )

    if updated_title_author:
        rebuild_paper_authors()

    result = {
        "updated_title_author": updated_title_author,
        "updated_plagiarism": updated_plagiarism,
        "unmatched": unmatched,
    }
    audit_success(
        "external_results_import",
        "External results imported.",
        result_counts=result,
        reset_flags={
            "title_author_review": bool(updated_title_author),
            "extracted_title_match": bool(updated_title_author),
            "duplicate_author_review": bool(updated_title_author),
        },
    )
    return result
