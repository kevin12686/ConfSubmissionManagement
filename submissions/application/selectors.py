from django.db.models import Q

from submissions.forms import FinalSubmissionImportForm, ImportFileForm
from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.checks import (
    dashboard_counts,
    paper_id_effectively_verified,
    paper_title_matches_master,
)
from submissions.services.file_manager import publication_pdf_info
from submissions.services.pdf_processor import processed_pdf_rows


def verification_badge(submission, master_paper=None):
    if submission.excluded_from_publication:
        return "Excluded from publication", "secondary"
    if submission.paper_id_verified and not paper_title_matches_master(submission, master_paper):
        return "ID verified, title differs", "warning"
    if paper_id_effectively_verified(submission, master_paper):
        if submission.paper_id_verified:
            return "ID verified", "success"
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
        )
    return {"papers": papers, "q": query, "import_form": ImportFileForm()}


def final_submission_list_context(query="", score_level_builder=None):
    submissions = FinalSubmission.objects.all()
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
    return {"submissions": items, "q": query, "import_form": FinalSubmissionImportForm()}


def processed_pdf_context():
    return {
        "processed_rows": processed_pdf_rows(),
        "settings_obj": AppSetting.load(),
    }


def active_versions_context():
    submissions = list(FinalSubmission.objects.filter(active_version=True))
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


def old_versions_context():
    return {"submissions": FinalSubmission.objects.filter(active_version=False)}
