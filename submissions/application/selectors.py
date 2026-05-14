from django.db.models import Q

from submissions.forms import FinalSubmissionImportForm, ImportFileForm
from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.checks import (
    dashboard_counts,
    paper_id_effectively_verified,
    paper_title_matches_master,
)
from submissions.services.file_manager import (
    active_pdfs_needing_processing,
    final_submission_display_pdf_info,
    final_submission_display_source_info,
    pdf_available_for_processing,
    publication_pdf_info,
)
from submissions.services.pdf_processor import processed_pdf_rows
from submissions.services.editor_uploads import editor_conflict_paper_ids
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


def dashboard_context(metric_sections_builder):
    counts = dashboard_counts()
    return {
        "counts": counts,
        "metric_sections": metric_sections_builder(counts),
    }


def paper_master_list_context(query=""):
    papers = InitialPaper.objects.all()
    if query:
        papers = papers.filter(
            Q(paper_id__icontains=query)
            | Q(acceptance_status__icontains=query)
            | Q(title__icontains=query)
            | Q(authors__icontains=query)
            | Q(notes__icontains=query)
        )
    note_summary = paper_note_summary()
    return {
        "papers": papers,
        "q": query,
        "import_form": ImportFileForm(),
        "note_summary": note_summary,
        "note_count": len(note_summary),
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


def final_submission_list_context(query="", score_level_builder=None, current_filter="all"):
    submissions = FinalSubmission.objects.all()
    valid_filters = {option["value"] for option in FINAL_SUBMISSION_FILTER_OPTIONS}
    if current_filter not in valid_filters:
        current_filter = "all"
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
    settings_obj = AppSetting.load()
    items = list(submissions)
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
        submission.display_pdf = final_submission_display_pdf_info(submission)
        submission.display_source = final_submission_display_source_info(submission)
    filter_tabs = [
        {**option, "count": tab_counts.get(option["value"], 0)}
        for option in FINAL_SUBMISSION_FILTER_OPTIONS
    ]
    return {
        "submissions": items,
        "q": query,
        "current_filter": current_filter,
        "filter_options": filter_tabs,
        "import_form": FinalSubmissionImportForm(),
    }


def processed_pdf_context():
    active_missing_pdf_rows = [
        submission
        for submission in FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
        )
        if not pdf_available_for_processing(submission)
    ]
    return {
        "processed_rows": processed_pdf_rows(),
        "settings_obj": AppSetting.load(),
        "active_needs_processing_rows": active_pdfs_needing_processing(),
        "active_missing_pdf_rows": active_missing_pdf_rows,
    }


def active_versions_context():
    submissions = list(FinalSubmission.objects.filter(active_version=True, discarded=False))
    master_by_id = {
        paper.paper_id: paper
        for paper in InitialPaper.objects.filter(
            paper_id__in={item.paper_id_filled for item in submissions}
        )
    }
    return {
        "rows": [_active_version_row(submission, master_by_id) for submission in submissions]
    }


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


def old_versions_context(current_filter="all"):
    rows = old_version_rows()
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
