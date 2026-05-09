from submissions.models import FinalSubmission
from submissions.services.file_manager import publication_pdf_info


OLD_VERSION_FILTER_OPTIONS = [
    {"value": "all", "label": "All"},
    {"value": "replaced", "label": "Replaced"},
    {"value": "discarded", "label": "Discarded"},
    {"value": "editor_uploads", "label": "Editor Uploads"},
    {"value": "start2", "label": "Start2"},
    {"value": "other", "label": "Other"},
]


def _active_replacements():
    return {
        submission.paper_id_filled: submission
        for submission in FinalSubmission.objects.filter(
            active_version=True, discarded=False
        ).exclude(paper_id_filled="")
    }


def classify_old_version(submission, active_by_paper=None):
    active_by_paper = active_by_paper if active_by_paper is not None else _active_replacements()
    replacement = active_by_paper.get(submission.paper_id_filled)
    if replacement and replacement.pk == submission.pk:
        replacement = None

    if submission.discarded:
        status_key = "discarded"
        status_label = "Discarded"
        status_class = "text-bg-dark"
        inactive_reason = "Discarded by editor"
    elif submission.duplicate_submission or replacement:
        status_key = "replaced"
        status_label = "Replaced"
        status_class = "text-bg-secondary"
        inactive_reason = "Replaced by active final version"
    else:
        status_key = "other"
        status_label = "Other inactive"
        status_class = "text-bg-light text-dark"
        inactive_reason = "Inactive without replacement/discard marker"

    origin_label = (
        "Editor Upload"
        if submission.submission_origin == "editor_upload"
        else "Start2"
    )
    origin_class = (
        "text-bg-primary"
        if submission.submission_origin == "editor_upload"
        else "text-bg-light text-dark"
    )

    note_items = []
    if submission.discard_notes:
        note_items.append({"label": "Discard note", "text": submission.discard_notes})
    if submission.editor_upload_notes:
        note_items.append({"label": "Editor note", "text": submission.editor_upload_notes})
    if submission.publication_exclusion_notes:
        note_items.append(
            {"label": "Not publishing note", "text": submission.publication_exclusion_notes}
        )
    note_summary = "; ".join(f"{item['label']}: {item['text']}" for item in note_items)

    return {
        "submission": submission,
        "version_status_key": status_key,
        "version_status_label": status_label,
        "version_status_class": status_class,
        "inactive_reason": inactive_reason,
        "origin_label": origin_label,
        "origin_class": origin_class,
        "active_replacement": replacement,
        "publication_pdf": publication_pdf_info(submission),
        "not_publishing_flag": submission.excluded_from_publication,
        "note_items": note_items,
        "note_summary": note_summary,
    }


def old_version_rows(queryset=None):
    queryset = queryset if queryset is not None else FinalSubmission.objects.filter(active_version=False)
    submissions = list(queryset.order_by("paper_id_filled", "final_submission_id"))
    active_by_paper = _active_replacements()
    return [classify_old_version(submission, active_by_paper) for submission in submissions]


def old_version_counts(rows):
    counts = {option["value"]: 0 for option in OLD_VERSION_FILTER_OPTIONS}
    counts["all"] = len(rows)
    for row in rows:
        counts[row["version_status_key"]] += 1
        if row["submission"].submission_origin == "editor_upload":
            counts["editor_uploads"] += 1
        else:
            counts["start2"] += 1
    return counts


def filter_old_version_rows(rows, current_filter):
    valid_filters = {option["value"] for option in OLD_VERSION_FILTER_OPTIONS}
    if current_filter not in valid_filters:
        current_filter = "all"
    if current_filter == "all":
        return rows, current_filter
    if current_filter == "editor_uploads":
        return [
            row
            for row in rows
            if row["submission"].submission_origin == "editor_upload"
        ], current_filter
    if current_filter == "start2":
        return [
            row
            for row in rows
            if row["submission"].submission_origin == "start2"
        ], current_filter
    return [
        row for row in rows if row["version_status_key"] == current_filter
    ], current_filter
