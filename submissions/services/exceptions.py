from django.utils import timezone

from submissions.models import AppSetting, AuthorLimitWaiver, FinalSubmission
from submissions.services.checks import (
    author_count_rows,
    author_number_count,
    has_valid_author_number_exception,
    page_count_out_of_range,
    reset_author_number_exception,
    reset_page_limit_exception,
)


EXCEPTION_FILTER_OPTIONS = [
    {"value": "not_allowed", "label": "Not allowed"},
    {"value": "allowed", "label": "Allowed"},
    {"value": "stale", "label": "Stale"},
    {"value": "all", "label": "All"},
]


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


def exception_rows(status_filter="not_allowed"):
    setting = AppSetting.load()
    rows = []
    active = FinalSubmission.objects.filter(
        active_version=True,
        excluded_from_publication=False,
    ).order_by("paper_id_filled", "final_submission_id")

    for submission in active:
        status = page_exception_status(submission, setting)
        if status:
            rows.append(
                {
                    "key": f"page:{submission.pk}",
                    "type": "page",
                    "type_label": "Page count",
                    "status": status,
                    "status_label": exception_status_label(status),
                    "status_level": exception_status_level(status),
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
                }
            )

        status = author_number_exception_status(submission, setting)
        if status:
            count = author_number_count(submission)
            rows.append(
                {
                    "key": f"author_number:{submission.pk}",
                    "type": "author_number",
                    "type_label": "Authors in paper",
                    "status": status,
                    "status_label": exception_status_label(status),
                    "status_level": exception_status_level(status),
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
                }
            )

    for author_row in author_count_rows():
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
        rows, _status_filter = exception_rows("all")
    return {
        "not_allowed": sum(1 for row in rows if row["status"] == "not_allowed"),
        "allowed": sum(1 for row in rows if row["status"] == "allowed"),
        "stale": sum(1 for row in rows if row["status"] == "stale"),
        "all": len(rows),
    }


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
        return

    raise ValueError("Unknown exception type.")
