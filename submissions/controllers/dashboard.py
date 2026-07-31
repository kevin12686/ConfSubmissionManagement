import csv
import logging
import shutil
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

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
DASHBOARD_WORKFLOW_GROUPS = [
    {
        "key": "mapping",
        "title": "Paper mapping and version decisions",
        "description": "Resolve missing finals, invalid or unverified IDs, Not Publishing decisions, and Start2/Editor conflicts.",
        "categories": {
            "Invalid Paper ID",
            "Multiple Active Final Submissions",
            "Start2/Editor Version Conflict",
            "Unclassified Final Not In Master",
            "Missing Final Submission",
            "Publication Decision Required",
            "Publication Decision Integrity Conflict",
            "Mixed Not Publishing Decision",
            "Unverified Paper ID",
            "Final Title / Paper Master Title Mismatch",
        },
        "breakdowns": (
            {
                "singular": "missing final",
                "plural": "missing finals",
                "categories": {"Missing Final Submission"},
            },
            {
                "singular": "publication decision",
                "plural": "publication decisions",
                "categories": {
                    "Unclassified Final Not In Master",
                    "Publication Decision Required",
                    "Publication Decision Integrity Conflict",
                    "Mixed Not Publishing Decision",
                },
            },
            {
                "singular": "Paper ID review",
                "plural": "Paper ID reviews",
                "categories": {
                    "Invalid Paper ID",
                    "Unverified Paper ID",
                    "Final Title / Paper Master Title Mismatch",
                },
            },
            {
                "singular": "version conflict",
                "plural": "version conflicts",
                "categories": {
                    "Multiple Active Final Submissions",
                    "Start2/Editor Version Conflict",
                },
            },
        ),
        "action_label": "Review mapping issues",
        "action_url_name": "submissions:error_report",
        "action_params": (("area", "mapping"),),
        "exact_category_filter": True,
    },
    {
        "key": "files",
        "title": "PDF, source, and page checks",
        "description": "Resolve missing files, processing status, and page limits. Corrected PDFs return here after formatting.",
        "categories": {
            "Missing PDF",
            "PDF Not Processed",
            "Corrected PDF Not Processed",
            "PDF Processing Error",
            "Missing Source File",
            "Missing Corrected PDF",
            "Missing Corrected Source",
            "Source Review Hash Missing",
            "Source Changed After Review",
            "Page Limit Exceeded",
            "Below Page Minimum",
        },
        "breakdowns": (
            {
                "singular": "paper with a missing file",
                "plural": "papers with missing files",
                "categories": {
                    "Missing PDF",
                    "Missing Corrected PDF",
                    "Missing Source File",
                    "Missing Corrected Source",
                },
            },
            {
                "singular": "paper needing PDF processing",
                "plural": "papers needing PDF processing",
                "categories": {
                    "PDF Not Processed",
                    "Corrected PDF Not Processed",
                    "PDF Processing Error",
                },
            },
            {
                "singular": "source review integrity issue",
                "plural": "source review integrity issues",
                "categories": {
                    "Source Review Hash Missing",
                    "Source Changed After Review",
                },
            },
            {
                "singular": "paper with a page issue",
                "plural": "papers with page issues",
                "categories": {"Page Limit Exceeded", "Below Page Minimum"},
            },
        ),
        "action_label": "Review file issues",
        "action_url_name": "submissions:error_report",
        "action_params": (("area", "files"),),
        "exact_category_filter": True,
    },
    {
        "key": "title_authors",
        "title": "Title and author review",
        "description": "Review extracted title, authors, and the title comparison together. Re-extract after a corrected PDF is processed.",
        "categories": {
            "Missing Extracted Title",
            "Missing Extracted Authors",
            "Title/Author Red Flag",
            "Unverified Title/Author Extraction",
        },
        "breakdowns": (
            {
                "singular": "paper missing extraction",
                "plural": "papers missing extraction",
                "categories": {
                    "Missing Extracted Title",
                    "Missing Extracted Authors",
                },
            },
            {
                "singular": "pending metadata review",
                "plural": "pending metadata reviews",
                "categories": {"Unverified Title/Author Extraction"},
            },
            {
                "singular": "Red Flag",
                "plural": "Red Flags",
                "categories": {"Title/Author Red Flag"},
            },
        ),
        "action_label": "Open Title/Author Review",
        "action_url_name": "submissions:title_author_extraction",
        "action_params": (("filter", "needs_verification"),),
    },
    {
        "key": "formatting",
        "title": "Formatting review",
        "description": "Review the current publication files. Uploading a corrected PDF resets processing and extraction checks.",
        "categories": {"Formatting Not Review OK"},
        "action_label": "Open Formatting Review",
        "action_url_name": "submissions:formatting",
        "action_params": (("filter", "needs_attention"),),
    },
    {
        "key": "plagiarism",
        "title": "Plagiarism results",
        "description": "Resolve missing P/S scores, stale reports or exceptions, and scores above configured thresholds.",
        "categories": {
            "Missing Plagiarism Result",
            "Stale Plagiarism Report",
            "Plagiarism % Over Threshold",
            "Single % Over Threshold",
            "Stale Plagiarism % Exception",
            "Stale Single % Exception",
        },
        "breakdowns": (
            {
                "singular": "paper missing P/S results",
                "plural": "papers missing P/S results",
                "categories": {"Missing Plagiarism Result"},
            },
            {
                "singular": "paper over a threshold",
                "plural": "papers over a threshold",
                "categories": {
                    "Plagiarism % Over Threshold",
                    "Single % Over Threshold",
                },
            },
            {
                "singular": "stale report",
                "plural": "stale reports",
                "categories": {"Stale Plagiarism Report"},
            },
            {
                "singular": "stale exception",
                "plural": "stale exceptions",
                "categories": {
                    "Stale Plagiarism % Exception",
                    "Stale Single % Exception",
                },
            },
        ),
        "action_label": "Review plagiarism issues",
        "action_url_name": "submissions:organized_list",
        "action_params": (("filter", "plagiarism_issues"),),
    },
    {
        "key": "authors",
        "title": "Author limits and duplicates",
        "description": "Resolve per-paper author limits, per-author paper limits, and duplicate author names.",
        "categories": {"Author Over Limit", "Duplicate Author In Paper"},
        "breakdowns": (
            {
                "singular": "paper affected by an author limit",
                "plural": "papers affected by author limits",
                "categories": {"Author Over Limit"},
            },
            {
                "singular": "duplicate-author review",
                "plural": "duplicate-author reviews",
                "categories": {"Duplicate Author In Paper"},
            },
        ),
        "action_label": "Review author issues",
        "action_url_name": "submissions:error_report",
        "action_params": (("area", "authors"),),
        "exact_category_filter": True,
    },
    {
        "key": "duplicates",
        "title": "Publication duplicates",
        "description": "Resolve duplicate publication titles, PDFs, or source files before final export.",
        "categories": {
            "Duplicate Publication Title",
            "Duplicate Publication PDF",
            "Duplicate Publication Source",
            "Duplicate Publication Filename",
        },
        "breakdowns": (
            {
                "singular": "paper with a duplicate title",
                "plural": "papers with duplicate titles",
                "categories": {"Duplicate Publication Title"},
            },
            {
                "singular": "paper with duplicate file content",
                "plural": "papers with duplicate file content",
                "categories": {
                    "Duplicate Publication PDF",
                    "Duplicate Publication Source",
                },
            },
            {
                "singular": "paper with a filename collision",
                "plural": "papers with filename collisions",
                "categories": {"Duplicate Publication Filename"},
            },
        ),
        "action_label": "Review duplicates",
        "action_url_name": "submissions:organized_list",
        "action_params": (("filter", "publication_duplicates"),),
    },
]


def _affected_paper_ids(rows):
    identifiers = set()
    for row in rows:
        raw_paper_ids = str(row.get("paper_id") or "").strip()
        if raw_paper_ids:
            identifiers.update(
                part.strip() for part in raw_paper_ids.split(",") if part.strip()
            )
        elif row.get("final_submission_id"):
            identifiers.add(f"Final {row['final_submission_id']}")
    return identifiers


def _paper_count_label(count, singular="paper", plural="papers"):
    return f"{count} {singular if count == 1 else plural}"


def _dashboard_breakdowns(group, group_rows):
    breakdowns = []
    for config in group.get("breakdowns", ()):
        rows = [
            row
            for row in group_rows
            if row["category"] in config["categories"]
        ]
        if not rows:
            continue
        count = len(_affected_paper_ids(rows)) or len(rows)
        breakdowns.append(
            {
                "count": count,
                "label": config["singular"] if count == 1 else config["plural"],
            }
        )
    return breakdowns


def _dashboard_action_url(group, group_rows):
    params = list(group.get("action_params", ()))
    if group.get("exact_category_filter"):
        params.extend(
            ("category", category)
            for category in sorted({row["category"] for row in group_rows})
        )
    query = urlencode(params)
    url = reverse(group["action_url_name"])
    return f"{url}?{query}" if query else url


def _dashboard_context(counts, readiness_rows):
    readiness_categories = Counter(row["category"] for row in readiness_rows)
    blocking_identifiers = _affected_paper_ids(readiness_rows)
    no_master_records = counts["total_papers"] == 0
    blocking_issue_count = len(readiness_rows) + (1 if no_master_records else 0)
    if no_master_records:
        readiness_categories["Paper Master List Empty"] += 1

    action_items = []
    completed_checks = []
    if no_master_records:
        action_items.append(
            {
                "key": "setup",
                "title": "Paper Master List setup",
                "description": "Import the Paper Master List before publication checks or final export.",
                "paper_count": 0,
                "count_label": "Setup needed",
                "action_label": "Open Paper Master List",
                "action_url": reverse("submissions:initial_paper_list"),
            }
        )

    registered_categories = set()
    for group in DASHBOARD_WORKFLOW_GROUPS:
        registered_categories.update(group["categories"])
        group_rows = [
            row for row in readiness_rows if row["category"] in group["categories"]
        ]
        if group_rows:
            paper_count = len(_affected_paper_ids(group_rows)) or len(group_rows)
            item = {
                **group,
                "paper_count": paper_count,
                "count_label": _paper_count_label(paper_count),
                "breakdowns": _dashboard_breakdowns(group, group_rows),
                "action_url": _dashboard_action_url(group, group_rows),
            }
            action_items.append(item)
        else:
            completed_checks.append(group["title"])

    unassigned_rows = [
        row
        for row in readiness_rows
        if row["category"] not in registered_categories
    ]
    if unassigned_rows:
        paper_count = (
            len(_affected_paper_ids(unassigned_rows)) or len(unassigned_rows)
        )
        action_items.append(
            {
                "key": "other",
                "title": "Other publication blockers",
                "description": (
                    "Review blocking categories that are not assigned to a standard "
                    "editorial workflow. Final export remains blocked."
                ),
                "paper_count": paper_count,
                "count_label": _paper_count_label(paper_count),
                "breakdowns": [
                    {
                        "count": count,
                        "label": category,
                    }
                    for category, count in Counter(
                        row["category"] for row in unassigned_rows
                    ).most_common()
                ],
                "action_label": "Open Error Report",
                "action_url": reverse("submissions:error_report"),
            }
        )
        completed_checks = []

    return {
        "readiness": {
            "ready": blocking_issue_count == 0,
            "blocking_paper_count": len(blocking_identifiers),
            "blocking_issue_count": blocking_issue_count,
            "top_categories": [
                {"label": category, "count": count}
                for category, count in readiness_categories.most_common(5)
            ],
        },
        "action_items": action_items,
        "completed_checks": completed_checks,
        "conference_totals": [
            {
                "label": "Paper Master scope",
                "value": counts["publication_scope_papers"],
                "url": reverse("submissions:initial_paper_list"),
            },
            {
                "label": "Publication candidates",
                "value": counts["publication_candidates"],
                "url": reverse("submissions:organized_list"),
            },
            {
                "label": "All final records",
                "value": counts["total_final_submissions"],
                "url": reverse("submissions:final_submission_list"),
            },
            {
                "label": "Not Publishing Papers",
                "value": counts["excluded_from_publication"],
                "url": reverse("submissions:not_publishing_list"),
            },
        ],
        "tracking_items": [
            {
                "label": "Verified title differences",
                "value": counts["verified_title_differences"],
                "help": "Paper IDs are verified; title differences remain visible for editorial awareness.",
                "url": reverse("submissions:organized_list")
                + "?filter=verified_title_differences",
            },
            {
                "label": "Reviewed extracted-title differences",
                "value": counts["reviewed_extracted_title_differences"],
                "help": "Title/Author Review is complete; Final and extracted title wording still differs for reference.",
                "url": reverse("submissions:title_author_extraction")
                + "?filter=reviewed_differences",
            },
            {
                "label": "Papers with allowed P/S exceptions",
                "value": counts["allowed_plagiarism_exception_papers"],
                "value_label": _paper_count_label(
                    counts["allowed_plagiarism_exception_papers"]
                ),
                "help": (
                    f"{counts['allowed_plagiarism_exception_approvals']} active "
                    "P/S approval"
                    f"{'s' if counts['allowed_plagiarism_exception_approvals'] != 1 else ''} "
                    "currently in effect."
                ),
                "url": reverse("submissions:exceptions_center")
                + "?filter=allowed&type=plagiarism",
            },
        ],
    }


def dashboard(request):
    return render(request, "submissions/dashboard.html")


def dashboard_summary(request):
    return render(
        request,
        "submissions/partials/dashboard_summary.html",
        dashboard_context(_dashboard_context),
    )
