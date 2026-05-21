import csv
import logging
import shutil
from pathlib import Path

from django.contrib import messages
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from submissions.forms import (
    AppSettingForm,
    CrossCheckExportForm,
    CrossCheckReportUploadForm,
    FinalSubmissionForm,
    FinalSubmissionImportForm,
    FormattingUploadForm,
    ImportFileForm,
    InitialPaperForm,
    SystemStateRestoreForm,
)
from submissions.models import AppSetting, FinalSubmission, InitialPaper, PaperAuthor
from submissions.services.checks import (
    author_count_rows,
    dashboard_counts,
    error_report_rows,
    error_report_severity_sections,
    error_report_sections,
    reset_author_number_exception,
)
from submissions.services.crosscheck import (
    CROSSCHECK_RESULT_TEMPLATE_COLUMNS,
    crosscheck_export_root,
    import_crosscheck_results,
    prepare_crosscheck_upload,
    upload_crosscheck_reports,
    validate_token,
)
from submissions.services.exceptions import (
    EXCEPTION_FILTER_OPTIONS,
    approve_exception,
    exception_counts,
    exception_rows,
    remove_exception,
)
from submissions.services.import_export import (
    EXTERNAL_RESULTS_TEMPLATE_COLUMNS,
    FINAL_SUBMISSION_TEMPLATE_COLUMNS,
    INITIAL_PAPER_TEMPLATE_COLUMNS,
    _mark_duplicate_submissions,
)
from submissions.services.import_preview import (
    apply_import_preview,
    preview_final_import,
    preview_initial_import,
)
from submissions.services.system_state import (
    CONFIRMATION_TEXT,
    SystemStateError,
    apply_system_state_restore,
    export_system_state,
    load_restore_preview,
    preview_system_state_restore,
)
from submissions.services.formatting import (
    FORMAT_FILTER_OPTIONS,
    formatting_filter_counts,
    formatting_preview_info,
    formatting_rows,
    update_formatting_submission,
)
from submissions.services.file_manager import (
    corrected_pdf_needs_processing,
    publication_pdf_info,
    publication_source_info,
    resolve_folder,
    sanitize_filename_part,
)
from submissions.services.organized_list import (
    ORGANIZED_LIST_FILTER_OPTIONS,
    ORGANIZED_LIST_SORT_OPTIONS,
    organized_list_rows,
)
from submissions.services.pdf_processor import processed_pdf_rows, process_all_pdfs
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.title_author_extraction import (
    extract_active_title_authors,
    extract_title_author_for_submission,
    extraction_overwrite_summary,
    set_title_author_review_status,
    title_author_extraction_rows,
    unverify_title_author,
    unverify_extracted_title,
    verify_extracted_title,
    verify_title_author,
)
from submissions.services import reports
from submissions.services.verification import (
    evaluate_submission,
    mark_not_publishing,
    unverify_submission,
    undo_not_publishing,
    verification_rows,
    verify_submission,
)
from submissions.application.selectors import dashboard_context


logger = logging.getLogger("submissions.views")

DEFAULT_FOLDER_SETTINGS = {
    "incoming_folder": "data/incoming",
    "active_final_folder": "data/active_final",
    "publication_pdf_debug_folder": "data/publication_pdf_debug",
    "old_versions_folder": "data/old_versions",
    "reports_folder": "data/reports",
    "extraction_results_folder": "data/extraction_results",
    "plagiarism_reports_folder": "data/plagiarism_reports",
}
TEMP_PATH_PREFIXES = ("/var/", "/private/var/", "/tmp/", "/private/tmp/")
def _issue_level(value, level="warning"):
    return level if value else "success"


def _issue_status(value, label="Review"):
    return label if value else "OK"


def _dashboard_metric_sections(counts):
    page_count_issues = counts["page_limit_errors"] + counts["page_minimum_errors"]
    title_author_review = (
        counts["title_author_pending"]
        + counts["title_author_red_flag"]
        + counts["unverified_extracted_title_match"]
    )
    plagiarism_review = (
        counts["missing_plagiarism_result"]
        + counts["plagiarism_over_threshold"]
        + counts["single_over_threshold"]
    )
    return [
        {
            "title": "Data Loaded",
            "description": "Core records currently available in the system.",
            "metrics": [
                {
                    "label": "Paper Master Records",
                    "value": counts["total_papers"],
                    "help": "Accepted paper records used as the source of truth.",
                    "level": "light",
                    "status_label": "Count",
                    "action_label": "Open",
                    "action_url": reverse("submissions:initial_paper_list"),
                },
                {
                    "label": "Final Submissions",
                    "value": counts["total_final_submissions"],
                    "help": "Uploaded final-submission metadata and files.",
                    "level": "light",
                    "status_label": "Count",
                    "action_label": "Open",
                    "action_url": reverse("submissions:final_submission_list"),
                },
                {
                    "label": "Active Final Versions",
                    "value": counts["active_final_versions"],
                    "help": "Current publication versions after duplicate handling.",
                    "level": "light",
                    "status_label": "Current",
                    "action_label": "Review",
                    "action_url": reverse("submissions:active_versions"),
                },
            ],
        },
        {
            "title": "Needs Review",
            "description": "Mapping and identity checks that affect paper matching.",
            "metrics": [
                {
                    "label": "Unverified Paper IDs",
                    "value": counts["unverified_paper_ids"],
                    "help": "Active submissions still waiting for Paper ID verification.",
                    "level": _issue_level(counts["unverified_paper_ids"]),
                    "status_label": _issue_status(counts["unverified_paper_ids"]),
                    "action_label": "Verify",
                    "action_url": reverse("submissions:verify_paper_ids"),
                },
                {
                    "label": "Title Mismatches",
                    "value": counts["title_mismatches"],
                    "help": "Verified Paper IDs where final and master titles differ.",
                    "level": _issue_level(counts["title_mismatches"]),
                    "status_label": _issue_status(counts["title_mismatches"]),
                    "action_label": "Review",
                    "action_url": reverse("submissions:verify_paper_ids"),
                },
                {
                    "label": "Paper ID Problems",
                    "value": counts["invalid_paper_ids"],
                    "help": "Publishable final submissions whose Paper ID is missing or not in the master list.",
                    "level": _issue_level(counts["invalid_paper_ids"], "danger"),
                    "status_label": _issue_status(counts["invalid_paper_ids"], "Fix"),
                    "action_label": "Fix",
                    "action_url": reverse("submissions:verify_paper_ids"),
                },
                {
                    "label": "Needs Not-Publishing Decision",
                    "value": counts["unclassified_not_in_master"],
                    "help": "Final submissions not in the Paper Master List that must be corrected or marked Not Publishing.",
                    "level": _issue_level(counts["unclassified_not_in_master"], "danger"),
                    "status_label": _issue_status(counts["unclassified_not_in_master"], "Decide"),
                    "action_label": "Review",
                    "action_url": reverse("submissions:not_publishing_list"),
                },
                {
                    "label": "Start2/Editor Conflicts",
                    "value": counts["start2_editor_conflicts"],
                    "help": "Papers where both Start2 and editor-uploaded versions exist and one side must be discarded.",
                    "level": _issue_level(counts["start2_editor_conflicts"], "warning"),
                    "status_label": _issue_status(counts["start2_editor_conflicts"], "Decide"),
                    "action_label": "Review",
                    "action_url": reverse("submissions:final_submission_list") + "?filter=version_conflicts",
                },
                {
                    "label": "Current Not Publishing",
                    "value": counts["excluded_from_publication"],
                    "help": "Current final submissions retained for tracking but excluded from the publication package.",
                    "level": "secondary" if counts["excluded_from_publication"] else "success",
                    "status_label": "Tracked" if counts["excluded_from_publication"] else "None",
                    "action_label": "Open",
                    "action_url": reverse("submissions:not_publishing_list"),
                },
                {
                    "label": "Missing Final Submissions",
                    "value": counts["missing_final_submissions"],
                    "help": "Master-list papers that do not have a matched final submission.",
                    "level": _issue_level(counts["missing_final_submissions"]),
                    "status_label": _issue_status(counts["missing_final_submissions"]),
                    "action_label": "Review",
                    "action_url": reverse("submissions:organized_list"),
                },
            ],
        },
        {
            "title": "Production Checks",
            "description": "File, extraction, plagiarism, and author-count readiness.",
            "metrics": [
                {
                    "label": "Page Count Issues",
                    "value": page_count_issues,
                    "help": "Active PDFs below the page minimum or above the page limit.",
                    "level": _issue_level(page_count_issues),
                    "status_label": _issue_status(page_count_issues),
                    "action_label": "Process",
                    "action_url": reverse("submissions:process"),
                },
                {
                    "label": "Active PDFs Need Process",
                    "value": counts["active_pdfs_need_processing"],
                    "help": "Active publication PDFs have not been processed or need page count/hash refresh.",
                    "level": _issue_level(counts["active_pdfs_need_processing"], "danger"),
                    "status_label": _issue_status(counts["active_pdfs_need_processing"], "Process"),
                    "action_label": "Process PDFs",
                    "action_url": reverse("submissions:process"),
                },
                {
                    "label": "Missing PDFs",
                    "value": counts["missing_pdfs"],
                    "help": "Active submissions without an available publication PDF.",
                    "level": _issue_level(counts["missing_pdfs"], "danger"),
                    "status_label": _issue_status(counts["missing_pdfs"], "Blocker"),
                    "action_label": "Process",
                    "action_url": reverse("submissions:process"),
                },
                {
                    "label": "Title/Author Review",
                    "value": title_author_review,
                    "help": "Missing extraction, Pending/Red Flag review states, or unverified title match checks.",
                    "level": _issue_level(title_author_review),
                    "status_label": _issue_status(title_author_review),
                    "action_label": "Review",
                    "action_url": reverse("submissions:title_author_extraction"),
                },
                {
                    "label": "Title/Author Red Flag",
                    "value": counts["title_author_red_flag"],
                    "help": "Papers marked because formatting may need fixing before extraction can be trusted.",
                    "level": _issue_level(counts["title_author_red_flag"], "danger"),
                    "status_label": _issue_status(counts["title_author_red_flag"], "Fix"),
                    "action_label": "Review",
                    "action_url": reverse("submissions:title_author_extraction") + "?filter=red_flag",
                },
                {
                    "label": "Missing Plagiarism Results",
                    "value": counts["missing_plagiarism_result"],
                    "help": "Active submissions missing plagiarism or single-source percentages.",
                    "level": _issue_level(counts["missing_plagiarism_result"]),
                    "status_label": _issue_status(counts["missing_plagiarism_result"]),
                    "action_label": "Review",
                    "action_url": reverse("submissions:organized_list") + "?filter=missing_plagiarism",
                },
                {
                    "label": "Plagiarism Threshold Issues",
                    "value": counts["plagiarism_over_threshold"] + counts["single_over_threshold"],
                    "help": "Active papers over the configured Plagiarism % or Single % thresholds.",
                    "level": _issue_level(counts["plagiarism_over_threshold"] + counts["single_over_threshold"], "danger"),
                    "status_label": _issue_status(counts["plagiarism_over_threshold"] + counts["single_over_threshold"], "Review"),
                    "action_label": "Review",
                    "action_url": reverse("submissions:organized_list") + "?filter=plagiarism_issues",
                },
                {
                    "label": "Plagiarism Total Review",
                    "value": plagiarism_review,
                    "help": "Missing or over-threshold plagiarism checks that need attention.",
                    "level": _issue_level(plagiarism_review),
                    "status_label": _issue_status(plagiarism_review),
                    "action_label": "Review",
                    "action_url": reverse("submissions:organized_list") + "?filter=plagiarism_issues",
                },
                {
                    "label": "Format Not OK",
                    "value": counts["format_not_ok"],
                    "help": "Active papers with formatting status Pending or Needs edit.",
                    "level": _issue_level(counts["format_not_ok"]),
                    "status_label": _issue_status(counts["format_not_ok"]),
                    "action_label": "Review",
                    "action_url": reverse("submissions:formatting"),
                },
                {
                    "label": "Authors Over Limit",
                    "value": counts["authors_over_limit"],
                    "help": "Authors exceeding the configured paper-count limit.",
                    "level": _issue_level(counts["authors_over_limit"]),
                    "status_label": _issue_status(counts["authors_over_limit"]),
                    "action_label": "Report",
                    "action_url": reverse("submissions:author_count"),
                },
            ],
        },
    ]


def dashboard(request):
    return render(
        request,
        "submissions/dashboard.html",
        dashboard_context(_dashboard_metric_sections),
    )
