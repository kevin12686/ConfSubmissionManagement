from django.utils import timezone

from submissions.models import AppSetting, AuthorLimitWaiver, FinalSubmission
from submissions.services.audit import audit_success
from submissions.services.checks import (
    author_count_rows,
    author_number_count,
    has_valid_author_number_exception,
    has_valid_plagiarism_percent_exception,
    has_valid_single_percent_exception,
    page_count_out_of_range,
    plagiarism_percent_over_threshold,
    reset_author_number_exception,
    reset_page_limit_exception,
    reset_plagiarism_percent_exception,
    reset_single_percent_exception,
    single_percent_over_threshold,
)
from submissions.services.file_manager import publication_pdf_info
from submissions.services.publication_read import PublicationReadContext


EXCEPTION_FILTER_OPTIONS = [
    {"value": "not_allowed", "label": "Not allowed"},
    {"value": "allowed", "label": "Allowed"},
    {"value": "stale", "label": "Stale"},
    {"value": "all", "label": "All"},
]
ROW_LEVEL_EXCEPTION_TYPES = {
    "page",
    "author_number",
    "plagiarism_percent",
    "single_percent",
}


def page_exception_status(submission, setting=None):
    setting = setting or AppSetting.load()
    if not page_count_out_of_range(submission, setting):
        return ""
    if submission.has_valid_page_limit_exception:
        return "allowed"
    if submission.page_limit_exception_approved:
        return "stale"
    return "not_allowed"


def author_number_exception_status(submission, setting=None):
    setting = setting or AppSetting.load()
    count = author_number_count(submission)
    if count <= setting.max_authors_per_paper:
        return ""
    if has_valid_author_number_exception(submission):
        return "allowed"
    if submission.author_number_exception_approved:
        return "stale"
    return "not_allowed"


def plagiarism_percent_exception_status(submission, setting=None):
    setting = setting or AppSetting.load()
    if not plagiarism_percent_over_threshold(submission, setting):
        return ""
    if has_valid_plagiarism_percent_exception(submission, setting):
        return "allowed"
    if submission.plagiarism_percent_exception_approved:
        return "stale"
    return "not_allowed"


def single_percent_exception_status(submission, setting=None):
    setting = setting or AppSetting.load()
    if not single_percent_over_threshold(submission, setting):
        return ""
    if has_valid_single_percent_exception(submission, setting):
        return "allowed"
    if submission.single_percent_exception_approved:
        return "stale"
    return "not_allowed"


def exception_status_label(status):
    return {
        "not_allowed": "Not allowed",
        "allowed": "Allowed exception",
        "stale": "Stale allowed exception",
    }.get(status, "")


def exception_status_level(status):
    return {
        "not_allowed": "danger",
        "allowed": "info",
        "stale": "warning",
    }.get(status, "secondary")


def _append_submission_exception_rows(
    rows,
    submission,
    setting,
    *,
    context=None,
    hydrate=True,
):
    page_status = page_exception_status(submission, setting)
    author_status = author_number_exception_status(submission, setting)
    plagiarism_status = plagiarism_percent_exception_status(submission, setting)
    single_status = single_percent_exception_status(submission, setting)
    if not any(
        [page_status, author_status, plagiarism_status, single_status]
    ):
        return
    publication_pdf = (
        publication_pdf_info(
            submission,
            context.file_inspection if context else None,
        )
        if hydrate
        else None
    )
    if page_status:
        rows.append(
            {
                "key": f"page:{submission.pk}",
                "type": "page",
                "type_label": "Page count",
                "status": page_status,
                "status_label": exception_status_label(page_status),
                "status_level": exception_status_level(page_status),
                "submission": submission,
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "subject": submission.paper_id_filled,
                "current_value": submission.page_count,
                "limit_label": f"{setting.page_minimum}-{setting.page_limit} pages",
                "approved_value": submission.page_limit_exception_page_count,
                "reason": submission.page_limit_exception_reason,
                "approved_at": submission.page_limit_exception_approved_at,
                "paper_ids": submission.paper_id_filled,
                "publication_pdf": publication_pdf,
            }
        )

    if author_status:
        count = author_number_count(submission)
        rows.append(
            {
                "key": f"author_number:{submission.pk}",
                "type": "author_number",
                "type_label": "Authors in paper",
                "status": author_status,
                "status_label": exception_status_label(author_status),
                "status_level": exception_status_level(author_status),
                "submission": submission,
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "subject": submission.paper_id_filled,
                "current_value": count,
                "limit_label": f"Max {setting.max_authors_per_paper} authors",
                "approved_value": submission.author_number_exception_author_count,
                "reason": submission.author_number_exception_reason,
                "approved_at": submission.author_number_exception_approved_at,
                "paper_ids": submission.paper_id_filled,
                "publication_pdf": publication_pdf,
            }
        )

    if plagiarism_status:
        rows.append(
            {
                "key": f"plagiarism_percent:{submission.pk}",
                "type": "plagiarism_percent",
                "type_label": "Plagiarism %",
                "status": plagiarism_status,
                "status_label": exception_status_label(plagiarism_status),
                "status_level": exception_status_level(plagiarism_status),
                "submission": submission,
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "subject": submission.paper_id_filled,
                "current_value": submission.similarity_score,
                "limit_label": f"Max {setting.plagiarism_percent_threshold}%",
                "approved_value": submission.plagiarism_percent_exception_approved_score,
                "reason": submission.plagiarism_percent_exception_reason,
                "approved_at": submission.plagiarism_percent_exception_approved_at,
                "paper_ids": submission.paper_id_filled,
                "publication_pdf": publication_pdf,
            }
        )

    if single_status:
        rows.append(
            {
                "key": f"single_percent:{submission.pk}",
                "type": "single_percent",
                "type_label": "Single %",
                "status": single_status,
                "status_label": exception_status_label(single_status),
                "status_level": exception_status_level(single_status),
                "submission": submission,
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "subject": submission.paper_id_filled,
                "current_value": submission.single_similarity_score,
                "limit_label": f"Max {setting.single_similarity_threshold}%",
                "approved_value": submission.single_percent_exception_approved_score,
                "reason": submission.single_percent_exception_reason,
                "approved_at": submission.single_percent_exception_approved_at,
                "paper_ids": submission.paper_id_filled,
                "publication_pdf": publication_pdf,
            }
        )


def exception_rows_for_submission(submission, setting=None):
    if not submission:
        return []
    setting = setting or AppSetting.load()
    rows = []
    _append_submission_exception_rows(rows, submission, setting)
    return rows


def exception_rows(
    status_filter="not_allowed",
    *,
    context=None,
    hydrate=True,
):
    context = context or PublicationReadContext.load()
    setting = context.settings
    rows = []
    active = sorted(
        context.publishable_submissions,
        key=lambda submission: (
            submission.paper_id_filled,
            submission.final_submission_id,
        ),
    )

    for submission in active:
        _append_submission_exception_rows(
            rows,
            submission,
            setting,
            context=context,
            hydrate=hydrate,
        )

    for author_row in author_count_rows(
        context=context,
        include_file_links=False,
    ):
        if not author_row["over_limit"]:
            continue
        waiver = author_row["waiver"]
        if author_row["waiver_valid"]:
            status = "allowed"
        elif waiver and waiver.approved:
            status = "stale"
        else:
            status = "not_allowed"
        rows.append(
            {
                "key": f"author_limit:{author_row['normalized_author_name']}",
                "type": "author_limit",
                "type_label": "Author paper count",
                "status": status,
                "status_label": exception_status_label(status),
                "status_level": exception_status_level(status),
                "submission": None,
                "paper_id": "",
                "final_submission_id": "",
                "normalized_author_name": author_row["normalized_author_name"],
                "subject": author_row["display_author_name"],
                "current_value": author_row["publication_paper_count"],
                "limit_label": f"Max {setting.author_paper_limit} papers",
                "approved_value": author_row["waiver_approved_count"],
                "reason": author_row["waiver_reason"],
                "approved_at": waiver.approved_at if waiver else None,
                "paper_ids": author_row["paper_ids"],
            }
        )

    if status_filter not in {option["value"] for option in EXCEPTION_FILTER_OPTIONS}:
        status_filter = "not_allowed"
    if status_filter != "all":
        rows = [row for row in rows if row["status"] == status_filter]
    return rows, status_filter


def exception_counts(rows=None):
    if rows is None:
        rows, _status_filter = exception_rows("all", hydrate=False)
    return {
        "not_allowed": sum(1 for row in rows if row["status"] == "not_allowed"),
        "allowed": sum(1 for row in rows if row["status"] == "allowed"),
        "stale": sum(1 for row in rows if row["status"] == "stale"),
        "all": len(rows),
    }


def hydrate_exception_rows(rows, *, context):
    hydrated = []
    for row in rows:
        if row.get("submission") and not row.get("publication_pdf"):
            hydrated.append(
                {
                    **row,
                    "publication_pdf": publication_pdf_info(
                        row["submission"],
                        context.file_inspection,
                    ),
                }
            )
        else:
            hydrated.append(row)
    return hydrated


def approve_exception(row, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Exception requires a reason note.")

    if row["type"] == "page":
        submission = row["submission"]
        if submission.page_count is None:
            raise ValueError("Cannot approve a page exception before page count is available.")
        submission.page_limit_exception_approved = True
        submission.page_limit_exception_reason = reason
        submission.page_limit_exception_page_count = submission.page_count
        submission.page_limit_exception_approved_at = timezone.now()
        submission.save(
            update_fields=[
                "page_limit_exception_approved",
                "page_limit_exception_reason",
                "page_limit_exception_page_count",
                "page_limit_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_allow",
            "Page count exception allowed.",
            submission=submission,
            reset_flags={"exception_type": "page"},
            after={"approved_value": submission.page_limit_exception_page_count, "reason": reason},
        )
        return

    if row["type"] == "author_number":
        submission = row["submission"]
        count = author_number_count(submission)
        if not count:
            raise ValueError("Cannot approve an author-count exception before authors are available.")
        submission.author_number_exception_approved = True
        submission.author_number_exception_reason = reason
        submission.author_number_exception_author_count = count
        submission.author_number_exception_approved_at = timezone.now()
        submission.save(
            update_fields=[
                "author_number_exception_approved",
                "author_number_exception_reason",
                "author_number_exception_author_count",
                "author_number_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_allow",
            "Author number exception allowed.",
            submission=submission,
            reset_flags={"exception_type": "author_number"},
            after={"approved_value": submission.author_number_exception_author_count, "reason": reason},
        )
        return

    if row["type"] == "author_limit":
        waiver, _created = AuthorLimitWaiver.objects.get_or_create(
            normalized_author_name=row["normalized_author_name"],
            defaults={"display_author_name": row["subject"]},
        )
        waiver.display_author_name = row["subject"]
        waiver.approved = True
        waiver.reason = reason
        waiver.approved_publication_paper_count = row["current_value"]
        waiver.approved_at = timezone.now()
        waiver.save()
        audit_success(
            "exception_allow",
            "Author paper-count exception allowed.",
            object_type="AuthorLimitWaiver",
            paper_id=row.get("paper_ids", ""),
            after={
                "normalized_author_name": waiver.normalized_author_name,
                "approved_publication_paper_count": waiver.approved_publication_paper_count,
                "reason": reason,
            },
        )
        return

    if row["type"] == "plagiarism_percent":
        submission = row["submission"]
        if submission.similarity_score is None:
            raise ValueError("Cannot approve a plagiarism exception before Plagiarism % is available.")
        submission.plagiarism_percent_exception_approved = True
        submission.plagiarism_percent_exception_reason = reason
        submission.plagiarism_percent_exception_approved_score = submission.similarity_score
        submission.plagiarism_percent_exception_approved_at = timezone.now()
        submission.save(
            update_fields=[
                "plagiarism_percent_exception_approved",
                "plagiarism_percent_exception_reason",
                "plagiarism_percent_exception_approved_score",
                "plagiarism_percent_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_allow",
            "Plagiarism % exception allowed.",
            submission=submission,
            reset_flags={"exception_type": "plagiarism_percent"},
            after={
                "approved_value": submission.plagiarism_percent_exception_approved_score,
                "threshold": AppSetting.load().plagiarism_percent_threshold,
                "reason": reason,
            },
        )
        return

    if row["type"] == "single_percent":
        submission = row["submission"]
        if submission.single_similarity_score is None:
            raise ValueError("Cannot approve a single-percent exception before Single % is available.")
        submission.single_percent_exception_approved = True
        submission.single_percent_exception_reason = reason
        submission.single_percent_exception_approved_score = submission.single_similarity_score
        submission.single_percent_exception_approved_at = timezone.now()
        submission.save(
            update_fields=[
                "single_percent_exception_approved",
                "single_percent_exception_reason",
                "single_percent_exception_approved_score",
                "single_percent_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_allow",
            "Single % exception allowed.",
            submission=submission,
            reset_flags={"exception_type": "single_percent"},
            after={
                "approved_value": submission.single_percent_exception_approved_score,
                "threshold": AppSetting.load().single_similarity_threshold,
                "reason": reason,
            },
        )
        return

    raise ValueError("Unknown exception type.")


def remove_exception(row):
    if row["type"] == "page":
        submission = row["submission"]
        reset_page_limit_exception(submission)
        submission.save(
            update_fields=[
                "page_limit_exception_approved",
                "page_limit_exception_reason",
                "page_limit_exception_page_count",
                "page_limit_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_remove",
            "Page count exception removed.",
            submission=submission,
            reset_flags={"exception_type": "page"},
        )
        return

    if row["type"] == "author_number":
        submission = row["submission"]
        reset_author_number_exception(submission)
        submission.save(
            update_fields=[
                "author_number_exception_approved",
                "author_number_exception_reason",
                "author_number_exception_author_count",
                "author_number_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_remove",
            "Author number exception removed.",
            submission=submission,
            reset_flags={"exception_type": "author_number"},
        )
        return

    if row["type"] == "plagiarism_percent":
        submission = row["submission"]
        reset_plagiarism_percent_exception(submission)
        submission.save(
            update_fields=[
                "plagiarism_percent_exception_approved",
                "plagiarism_percent_exception_reason",
                "plagiarism_percent_exception_approved_score",
                "plagiarism_percent_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_remove",
            "Plagiarism % exception removed.",
            submission=submission,
            reset_flags={"exception_type": "plagiarism_percent"},
        )
        return

    if row["type"] == "single_percent":
        submission = row["submission"]
        reset_single_percent_exception(submission)
        submission.save(
            update_fields=[
                "single_percent_exception_approved",
                "single_percent_exception_reason",
                "single_percent_exception_approved_score",
                "single_percent_exception_approved_at",
                "updated_at",
            ]
        )
        audit_success(
            "exception_remove",
            "Single % exception removed.",
            submission=submission,
            reset_flags={"exception_type": "single_percent"},
        )
        return

    if row["type"] == "author_limit":
        waiver, _created = AuthorLimitWaiver.objects.get_or_create(
            normalized_author_name=row["normalized_author_name"],
            defaults={"display_author_name": row["subject"]},
        )
        waiver.approved = False
        waiver.reason = ""
        waiver.approved_publication_paper_count = None
        waiver.approved_at = None
        waiver.save()
        audit_success(
            "exception_remove",
            "Author paper-count exception removed.",
            object_type="AuthorLimitWaiver",
            paper_id=row.get("paper_ids", ""),
            before={"normalized_author_name": waiver.normalized_author_name},
        )
        return

    raise ValueError("Unknown exception type.")
