from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from submissions.forms import FinalSubmissionImportForm, ImportFileForm
from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.checks import (
    author_count_rows,
    dashboard_counts,
    paper_id_effectively_verified,
    paper_title_matches_master,
    publication_readiness_rows,
)
from submissions.services.file_manager import (
    active_pdf_needs_processing,
    final_submission_display_pdf_info,
    final_submission_display_source_info,
    pdf_available_for_processing,
    publication_pdf_info,
)
from submissions.services.file_inspection import FileInspectionContext
from submissions.services.pdf_processor import processed_pdf_rows
from submissions.services.publication_read import PublicationReadContext
from submissions.services.editor_uploads import editor_conflict_paper_ids
from submissions.services.text_utils import natural_text_key
from submissions.services.version_history import (
    OLD_VERSION_FILTER_OPTIONS,
    filter_old_version_rows,
    old_version_counts,
    old_version_rows,
)


def verification_badge(submission, master_paper=None):
    if submission.excluded_from_publication:
        return "Excluded from publication", "secondary"
    if submission.paper_id_verified and not paper_title_matches_master(submission, master_paper):
        return "Verified, title differs", "warning"
    if paper_id_effectively_verified(submission, master_paper):
        if submission.paper_id_verified:
            return "Verified", "success"
        return "Auto-verified by title", "success"
    if submission.verification_status == "title_mismatch":
        return "Paper ID title mismatch", "warning"
    if submission.verification_status == "invalid_paper_id":
        return "Paper ID not in master list", "warning"
    return "Paper ID needs review", "danger"


def search_query(request):
    return request.GET.get("q", "").strip()


def focused_submission_context(
    submission,
    *,
    title,
    message,
    back_url,
    status_label="Focused record",
    status_level="primary",
    out_of_scope=False,
):
    return {
        "title": title,
        "message": message,
        "paper_id": submission.paper_id_filled or "No Paper ID",
        "final_submission_id": submission.final_submission_id,
        "origin": submission.get_submission_origin_display(),
        "status_label": status_label,
        "status_level": status_level,
        "out_of_scope": out_of_scope,
        "back_url": back_url,
        "edit_url": (
            reverse("submissions:final_submission_edit", args=[submission.pk])
            + f"?{urlencode({'next': back_url})}"
        ),
    }


def focused_paper_context(paper, *, message, back_url, submission=None):
    return {
        "title": "Focused publication paper",
        "message": message,
        "paper_id": paper.paper_id,
        "final_submission_id": (
            submission.final_submission_id if submission else ""
        ),
        "origin": submission.get_submission_origin_display() if submission else "",
        "status_label": "Current publication record" if submission else "No final",
        "status_level": "primary" if submission else "danger",
        "out_of_scope": False,
        "back_url": back_url,
        "edit_url": (
            reverse("submissions:final_submission_edit", args=[submission.pk])
            + f"?{urlencode({'next': back_url})}"
            if submission
            else ""
        ),
    }


def dashboard_context(context_builder):
    publication_context = PublicationReadContext.load()
    author_rows = author_count_rows(
        context=publication_context,
        include_file_links=False,
    )
    counts = dashboard_counts(
        context=publication_context,
        author_rows=author_rows,
    )
    return {
        "counts": counts,
        **context_builder(
            counts,
            publication_readiness_rows(
                context=publication_context,
                author_rows=author_rows,
            ),
        ),
    }


PAPER_MASTER_SORT_OPTIONS = [
    {"value": "paper_id_asc", "label": "Paper ID ascending"},
    {"value": "paper_id_desc", "label": "Paper ID descending"},
    {"value": "title_asc", "label": "Master title A-Z"},
    {"value": "title_desc", "label": "Master title Z-A"},
    {"value": "acceptance_status", "label": "Accept status"},
    {"value": "updated_desc", "label": "Recently updated"},
]


def _sort_paper_master_rows(papers, current_sort):
    if current_sort in {"paper_id_asc", "paper_id_desc"}:
        return [
            pk
            for pk, _paper_id in sorted(
                papers.values_list("pk", "paper_id"),
                key=lambda row: natural_text_key(row[1]),
                reverse=current_sort == "paper_id_desc",
            )
        ]
    ordering = {
        "title_asc": ("title", "paper_id"),
        "title_desc": ("-title", "paper_id"),
        "acceptance_status": ("acceptance_status", "paper_id"),
        "updated_desc": ("-updated_at", "paper_id"),
    }
    return papers.order_by(*ordering[current_sort])


def paper_master_list_context(
    query="",
    current_sort="paper_id_asc",
    page_builder=None,
):
    all_papers = InitialPaper.objects.all()
    total_paper_count = all_papers.count()
    papers = all_papers
    valid_sorts = {option["value"] for option in PAPER_MASTER_SORT_OPTIONS}
    if current_sort not in valid_sorts:
        current_sort = "paper_id_asc"
    if query:
        papers = papers.filter(
            Q(paper_id__icontains=query)
            | Q(acceptance_status__icontains=query)
            | Q(title__icontains=query)
            | Q(authors__icontains=query)
            | Q(notes__icontains=query)
        )
    papers = _sort_paper_master_rows(papers, current_sort)
    displayed_paper_count = (
        len(papers)
        if isinstance(papers, list)
        else papers.count()
    )
    page = page_builder(papers) if page_builder else None
    selected_items = list(page.items if page else papers)
    if selected_items and isinstance(selected_items[0], int):
        paper_by_id = InitialPaper.objects.in_bulk(selected_items)
        selected_items = [
            paper_by_id[paper_id]
            for paper_id in selected_items
            if paper_id in paper_by_id
        ]
    note_summary = paper_note_summary()
    return {
        "papers": selected_items,
        "q": query,
        "current_sort": current_sort,
        "sort_options": PAPER_MASTER_SORT_OPTIONS,
        "total_paper_count": total_paper_count,
        "displayed_paper_count": displayed_paper_count,
        "import_form": ImportFileForm(),
        "note_summary": note_summary,
        "note_count": len(note_summary),
        "pagination": page,
    }


def paper_note_summary():
    return list(
        InitialPaper.objects.exclude(notes="")
        .order_by("paper_id")
        .values("id", "paper_id", "acceptance_status", "title", "notes")
    )


FINAL_SUBMISSION_FILTER_OPTIONS = [
    {"value": "all", "label": "All"},
    {"value": "version_conflicts", "label": "Version conflicts"},
    {"value": "editor_uploads", "label": "Editor uploads"},
    {"value": "discarded", "label": "Discarded"},
    {"value": "start2", "label": "Start2"},
]

FINAL_SUBMISSION_SORT_OPTIONS = [
    {"value": "paper_id_asc", "label": "Paper ID ascending"},
    {"value": "paper_id_desc", "label": "Paper ID descending"},
    {"value": "final_id_asc", "label": "Final ID ascending"},
    {"value": "final_id_desc", "label": "Final ID descending"},
    {"value": "upload_date_desc", "label": "Newest upload first"},
    {"value": "upload_date_asc", "label": "Oldest upload first"},
    {"value": "title_asc", "label": "Final title A-Z"},
    {"value": "current_first", "label": "Current finals first"},
]


def _sort_final_submission_rows(submissions, current_sort):
    items = list(
        submissions.values_list(
            "pk",
            "paper_id_filled",
            "final_submission_id",
            "upload_date",
            "created_at",
            "active_version",
            "discarded",
            "final_submission_title",
        )
    )
    pk = 0
    paper_id = 1
    final_id = 2
    upload_date = 3
    created_at = 4
    active_version = 5
    discarded = 6
    title = 7
    if current_sort in {"paper_id_asc", "paper_id_desc"}:
        items.sort(
            key=lambda item: (item[upload_date], item[created_at]),
            reverse=True,
        )
        populated = [item for item in items if item[paper_id]]
        missing = [item for item in items if not item[paper_id]]
        populated.sort(
            key=lambda item: natural_text_key(item[paper_id]),
            reverse=current_sort == "paper_id_desc",
        )
        return [item[pk] for item in populated + missing]
    if current_sort in {"final_id_asc", "final_id_desc"}:
        ordered = sorted(
            items,
            key=lambda item: natural_text_key(item[final_id]),
            reverse=current_sort == "final_id_desc",
        )
        return [item[pk] for item in ordered]
    if current_sort in {"upload_date_desc", "upload_date_asc"}:
        ordered = sorted(
            items,
            key=lambda item: (
                item[upload_date],
                item[created_at],
                natural_text_key(item[final_id]),
            ),
            reverse=current_sort == "upload_date_desc",
        )
        return [item[pk] for item in ordered]
    if current_sort == "title_asc":
        ordered = sorted(
            items,
            key=lambda item: (
                natural_text_key(item[title]),
                natural_text_key(item[paper_id]),
            ),
        )
        return [item[pk] for item in ordered]
    ordered = sorted(
        items,
        key=lambda item: (
            not item[active_version],
            item[discarded],
            natural_text_key(item[paper_id]),
            natural_text_key(item[final_id]),
        ),
    )
    return [item[pk] for item in ordered]


def final_submission_list_context(
    query="",
    score_level_builder=None,
    current_filter="all",
    current_sort="paper_id_asc",
    page_builder=None,
):
    submissions = FinalSubmission.objects.all()
    valid_filters = {option["value"] for option in FINAL_SUBMISSION_FILTER_OPTIONS}
    if current_filter not in valid_filters:
        current_filter = "all"
    valid_sorts = {option["value"] for option in FINAL_SUBMISSION_SORT_OPTIONS}
    if current_sort not in valid_sorts:
        current_sort = "paper_id_asc"
    if query:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=query)
            | Q(paper_id_filled__icontains=query)
            | Q(start2_paper_id_raw__icontains=query)
            | Q(final_submission_title__icontains=query)
            | Q(final_submission_authors__icontains=query)
            | Q(extracted_title__icontains=query)
            | Q(extracted_authors__icontains=query)
            | Q(processing_status__icontains=query)
        )
    conflict_ids = set(editor_conflict_paper_ids())
    tab_counts = {
        "all": submissions.count(),
        "version_conflicts": submissions.filter(paper_id_filled__in=conflict_ids).count(),
        "editor_uploads": submissions.filter(submission_origin="editor_upload").count(),
        "discarded": submissions.filter(discarded=True).count(),
        "start2": submissions.filter(submission_origin="start2").count(),
    }
    if current_filter == "version_conflicts":
        submissions = submissions.filter(paper_id_filled__in=conflict_ids)
    elif current_filter == "editor_uploads":
        submissions = submissions.filter(submission_origin="editor_upload")
    elif current_filter == "discarded":
        submissions = submissions.filter(discarded=True)
    elif current_filter == "start2":
        submissions = submissions.filter(submission_origin="start2")
    submissions = _sort_final_submission_rows(submissions, current_sort)
    page = page_builder(submissions) if page_builder else None
    settings_obj = AppSetting.load()
    item_ids = list(page.items if page else submissions)
    submission_by_id = FinalSubmission.objects.in_bulk(item_ids)
    items = [
        submission_by_id[submission_id]
        for submission_id in item_ids
        if submission_id in submission_by_id
    ]
    file_inspection = FileInspectionContext()
    master_by_id = {
        paper.paper_id: paper
        for paper in InitialPaper.objects.filter(
            paper_id__in={item.paper_id_filled for item in items}
        )
    }
    for submission in items:
        if score_level_builder:
            submission.plagiarism_percent_level = score_level_builder(
                submission.similarity_score,
                settings_obj.plagiarism_percent_threshold,
            )
            submission.single_percent_level = score_level_builder(
                submission.single_similarity_score,
                settings_obj.single_similarity_threshold,
            )
        label, level = verification_badge(
            submission,
            master_by_id.get(submission.paper_id_filled),
        )
        submission.verification_badge_label = label
        submission.verification_badge_level = level
        submission.version_conflict = submission.paper_id_filled in conflict_ids
        submission.display_pdf = final_submission_display_pdf_info(
            submission,
            file_inspection,
        )
        submission.display_source = final_submission_display_source_info(
            submission,
            file_inspection,
        )
    filter_tabs = [
        {**option, "count": tab_counts.get(option["value"], 0)}
        for option in FINAL_SUBMISSION_FILTER_OPTIONS
    ]
    return {
        "submissions": items,
        "q": query,
        "current_filter": current_filter,
        "current_sort": current_sort,
        "sort_options": FINAL_SUBMISSION_SORT_OPTIONS,
        "filter_options": filter_tabs,
        "import_form": FinalSubmissionImportForm(),
        "pagination": page,
    }


PROCESS_PREVIEW_FILTER_OPTIONS = [
    {"value": "needs_processing", "label": "Needs processing"},
    {"value": "page_issues", "label": "Page issues"},
    {"value": "processed", "label": "Processed"},
    {"value": "all", "label": "All"},
]


def processed_pdf_context(
    query="",
    current_filter="all",
    exact_submission_id=None,
    *,
    include_thumbnails=True,
):
    publication_context = PublicationReadContext.load()
    settings_obj = publication_context.settings
    all_rows = processed_pdf_rows(
        submissions=publication_context.master_submissions,
        include_thumbnails=include_thumbnails,
    )
    active_needs_processing_rows = []
    active_missing_pdf_rows = []
    for row in all_rows:
        submission = row["submission"]
        has_selected_pdf = bool(
            submission.formatted_pdf_file or submission.pdf_file
        )
        row["missing_pdf"] = not has_selected_pdf
        row["needs_processing"] = bool(
            has_selected_pdf
            and (
                submission.processing_status != "processed"
                or submission.page_count is None
                or not submission.pdf_hash
                or submission.thumbnail_status != "processed"
                or not submission.thumbnail_folder
            )
        )
        if row["missing_pdf"]:
            active_missing_pdf_rows.append(submission)
        if row["needs_processing"]:
            active_needs_processing_rows.append(submission)
    if exact_submission_id is not None:
        active_needs_processing_rows = [
            submission
            for submission in active_needs_processing_rows
            if submission.pk == exact_submission_id
        ]
        active_missing_pdf_rows = [
            submission
            for submission in active_missing_pdf_rows
            if submission.pk == exact_submission_id
        ]
        all_rows = [
            row
            for row in all_rows
            if row["submission"].pk == exact_submission_id
        ]
        current_filter = "all"
    needs_processing_ids = {
        submission.pk for submission in active_needs_processing_rows
    }
    missing_pdf_ids = {submission.pk for submission in active_missing_pdf_rows}
    for row in all_rows:
        submission = row["submission"]
        row["needs_processing"] = submission.pk in needs_processing_ids
        row["missing_pdf"] = submission.pk in missing_pdf_ids
        row["page_issue"] = bool(
            submission.page_count is not None
            and (
                submission.page_count < settings_obj.page_minimum
                or submission.page_count > settings_obj.page_limit
            )
        )
        row["is_processed"] = bool(
            submission.processing_status == "processed"
            and not row["needs_processing"]
            and not row["missing_pdf"]
        )
    if query and exact_submission_id is None:
        lowered_query = query.casefold()
        all_rows = [
            row
            for row in all_rows
            if lowered_query
            in " ".join(
                [
                    row["submission"].paper_id_filled or "",
                    row["submission"].final_submission_id or "",
                    row["submission"].final_submission_title or "",
                ]
            ).casefold()
        ]
    counts = {
        "all": len(all_rows),
        "needs_processing": sum(1 for row in all_rows if row["needs_processing"]),
        "page_issues": sum(1 for row in all_rows if row["page_issue"]),
        "processed": sum(1 for row in all_rows if row["is_processed"]),
    }
    valid_filters = {option["value"] for option in PROCESS_PREVIEW_FILTER_OPTIONS}
    if current_filter not in valid_filters:
        current_filter = "all"
    if current_filter == "needs_processing":
        display_rows = [row for row in all_rows if row["needs_processing"]]
    elif current_filter == "page_issues":
        display_rows = [row for row in all_rows if row["page_issue"]]
    elif current_filter == "processed":
        display_rows = [row for row in all_rows if row["is_processed"]]
    else:
        display_rows = all_rows
    return {
        "processed_rows": display_rows,
        "settings_obj": settings_obj,
        "active_needs_processing_rows": active_needs_processing_rows,
        "active_missing_pdf_rows": active_missing_pdf_rows,
        "has_pdf_issues": bool(
            active_needs_processing_rows
            or active_missing_pdf_rows
            or counts["page_issues"]
        ),
        "q": query,
        "current_filter": current_filter,
        "filter_options": [
            {**option, "count": counts[option["value"]]}
            for option in PROCESS_PREVIEW_FILTER_OPTIONS
        ],
        "_publication_context": publication_context,
    }


def hydrate_processed_pdf_file_state(rows, *, context):
    hydrated = []
    for row in rows:
        submission = row["submission"]
        missing_pdf = not pdf_available_for_processing(
            submission,
            context.file_inspection,
        )
        needs_processing = bool(
            not missing_pdf
            and active_pdf_needs_processing(
                submission,
                context.file_inspection,
            )
        )
        hydrated.append(
            {
                **row,
                "missing_pdf": missing_pdf,
                "needs_processing": needs_processing,
                "is_processed": bool(
                    submission.processing_status == "processed"
                    and not needs_processing
                    and not missing_pdf
                ),
            }
        )
    return hydrated


def active_versions_context(query=""):
    submissions = list(
        FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
            paper_id_filled__in=InitialPaper.objects.values("paper_id"),
        )
    )
    master_by_id = {
        paper.paper_id: paper
        for paper in InitialPaper.objects.filter(
            paper_id__in={item.paper_id_filled for item in submissions}
        )
    }
    rows = [_active_version_row(submission, master_by_id) for submission in submissions]
    if query:
        lowered_query = query.casefold()
        rows = [
            row
            for row in rows
            if lowered_query
            in " ".join(
                [
                    row["submission"].paper_id_filled or "",
                    row["submission"].final_submission_id or "",
                    row["submission"].final_submission_title or "",
                    row["submission"].final_submission_authors or "",
                ]
            ).casefold()
        ]
    return {"rows": rows, "q": query}


def _active_version_row(submission, master_by_id):
    label, level = verification_badge(
        submission,
        master_by_id.get(submission.paper_id_filled),
    )
    return {
        "submission": submission,
        "publication_pdf": publication_pdf_info(submission),
        "verification_badge": {"label": label, "level": level},
    }


def old_versions_context(current_filter="all", *, hydrate=True):
    rows = old_version_rows(hydrate=hydrate)
    counts = old_version_counts(rows)
    filtered_rows, current_filter = filter_old_version_rows(rows, current_filter)
    filter_tabs = [
        {**option, "count": counts.get(option["value"], 0)}
        for option in OLD_VERSION_FILTER_OPTIONS
    ]
    current_label = next(
        option["label"] for option in filter_tabs if option["value"] == current_filter
    )
    return {
        "rows": filtered_rows,
        "summary_counts": counts,
        "current_filter": current_filter,
        "current_filter_label": current_label,
        "filter_options": filter_tabs,
    }
