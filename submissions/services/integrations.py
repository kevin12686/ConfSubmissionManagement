from django.utils import timezone

from submissions.models import FinalSubmission
from submissions.services.audit import audit_success
from submissions.services.checks import rebuild_paper_authors, reset_author_number_exception
from submissions.services.import_export import clean_value, normalize_columns, parse_decimal, read_table


def find_submission(row):
    final_id = clean_value(row.get("final_submission_id"))
    if final_id:
        submission = FinalSubmission.objects.filter(final_submission_id=final_id).first()
        if submission:
            return submission

    paper_id = clean_value(row.get("paper_id") or row.get("paper_id_filled"))
    if paper_id:
        return (
            FinalSubmission.objects.filter(
                paper_id_filled=paper_id,
                active_version=True,
                discarded=False,
                excluded_from_publication=False,
            )
            .first()
        )
    return None


def import_external_results(uploaded_file):
    frame = normalize_columns(read_table(uploaded_file))
    updated_title_author = 0
    updated_plagiarism = 0
    unmatched = 0

    for row in frame.to_dict("records"):
        submission = find_submission(row)
        if not submission:
            unmatched += 1
            continue

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

        submission.save()

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
