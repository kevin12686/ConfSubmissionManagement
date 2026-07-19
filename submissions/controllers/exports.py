import csv
import logging
import shutil
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
    filter_error_report_rows,
    hydrate_author_count_rows,
    reset_author_number_exception,
    sort_error_report_rows,
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
from submissions.services.file_inspection import FileInspectionContext
from submissions.services.organized_list import (
    ORGANIZED_LIST_FILTER_OPTIONS,
    ORGANIZED_LIST_SORT_OPTIONS,
    organized_list_rows,
)
from submissions.services.pdf_processor import processed_pdf_rows, process_all_pdfs
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.publication_read import PublicationReadContext
from submissions.services.version_history import hydrate_old_version_rows
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
from submissions.application.selectors import old_versions_context
from submissions.application.pagination import paginate_worklist


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
def download_template(request, template_type):
    templates = {
        "master": ("paper_master_list_template.csv", INITIAL_PAPER_TEMPLATE_COLUMNS),
        "initial": ("paper_master_list_template.csv", INITIAL_PAPER_TEMPLATE_COLUMNS),
        "final": ("final_submissions_template.csv", FINAL_SUBMISSION_TEMPLATE_COLUMNS),
        "external": ("external_results_template.csv", EXTERNAL_RESULTS_TEMPLATE_COLUMNS),
        "plagiarism": ("plagiarism_results_template.csv", CROSSCHECK_RESULT_TEMPLATE_COLUMNS),
        "crosscheck": ("plagiarism_results_template.csv", CROSSCHECK_RESULT_TEMPLATE_COLUMNS),
    }
    if template_type not in templates:
        return redirect("submissions:dashboard")

    filename, columns = templates[template_type]
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(columns)
    if template_type in {"master", "initial"}:
        writer.writerow(["R003", "R-to-R", "Example Master Title", "Ada Lovelace; Alan Turing", "Optional editorial note"])
    elif template_type == "final":
        writer.writerow(["34", "R3", "Example Final Title", "Ada Lovelace and Alan Turing", "2026-05-07 09:00:00", "Submit_PDF, Submit_Source"])
    elif template_type in {"plagiarism", "crosscheck"}:
        writer.writerow(["R003_MAY082026.pdf", "12.35", "4.20"])
    else:
        writer.writerow(["9001", "R003", "Extracted Title", "Ada Lovelace; Alan Turing", "clear", "4.25", "1.50", "data/plagiarism_reports/R003.pdf"])
    return response
def active_versions(request):
    query = {"view": "compact"}
    q = request.GET.get("q", "").strip()
    if q:
        query["q"] = q
    return redirect(f"{reverse('submissions:organized_list')}?{urlencode(query)}")


def old_versions(request):
    context = old_versions_context(
        request.GET.get("filter", "all"),
        hydrate=False,
    )
    page = paginate_worklist(request, context["rows"])
    context["rows"] = hydrate_old_version_rows(
        page.items,
        inspection=FileInspectionContext(),
    )
    context["pagination"] = page
    return render(
        request,
        "submissions/old_versions.html",
        context,
    )


def error_report(request):
    publication_context = PublicationReadContext.load()
    author_rows = author_count_rows(
        context=publication_context,
        include_file_links=False,
    )
    rows = error_report_rows(
        context=publication_context,
        author_rows=author_rows,
        compact_duplicate_messages=True,
    )
    rows, current_area, area_label = filter_error_report_rows(
        rows, request.GET.get("area", "")
    )
    rows = sort_error_report_rows(rows)
    summary_sections = error_report_severity_sections(rows)
    page = paginate_worklist(request, rows)
    displayed_rows = list(page.items)
    severity_sections = error_report_severity_sections(displayed_rows)
    total_by_severity = {
        section["severity"]: section["count"]
        for section in summary_sections
    }
    for section in severity_sections:
        section["total_count"] = total_by_severity.get(
            section["severity"],
            section["count"],
        )
    return render(
        request,
        "submissions/error_report.html",
        {
            "rows": displayed_rows,
            "sections": error_report_sections(displayed_rows),
            "severity_sections": severity_sections,
            "severity_summary_sections": summary_sections,
            "current_area": current_area,
            "area_label": area_label,
            "pagination": page,
        },
    )


def author_count(request):
    q = request.GET.get("q", "").strip()
    current_filter = request.GET.get("filter", "all")
    current_sort = request.GET.get("sort", "attention")
    valid_filters = {"all", "attention", "over_limit", "duplicates", "allowed"}
    valid_sorts = {"attention", "paper_count_desc", "paper_count_asc", "name"}
    if current_filter not in valid_filters:
        current_filter = "all"
    if current_sort not in valid_sorts:
        current_sort = "attention"
    publication_context = PublicationReadContext.load()
    all_rows = author_count_rows(
        context=publication_context,
        include_file_links=False,
    )
    if q:
        lowered_query = q.casefold()
        all_rows = [
            row
            for row in all_rows
            if lowered_query
            in " ".join(
                [
                    row["normalized_author_name"],
                    row["display_author_name"],
                    row["paper_ids"],
                ]
            ).casefold()
        ]
    predicates = {
        "attention": lambda row: row["over_limit"] or bool(row["duplicate_author_papers"]),
        "over_limit": lambda row: row["over_limit"],
        "duplicates": lambda row: bool(row["duplicate_author_papers"]),
        "allowed": lambda row: row["over_limit"] and row["waiver_valid"],
    }
    rows = (
        [row for row in all_rows if predicates[current_filter](row)]
        if current_filter in predicates
        else all_rows
    )
    if current_sort == "paper_count_desc":
        rows = sorted(
            rows,
            key=lambda row: (-row["publication_paper_count"], row["normalized_author_name"]),
        )
    elif current_sort == "paper_count_asc":
        rows = sorted(
            rows,
            key=lambda row: (row["publication_paper_count"], row["normalized_author_name"]),
        )
    elif current_sort == "name":
        rows = sorted(rows, key=lambda row: row["normalized_author_name"])
    else:
        rows = sorted(
            rows,
            key=lambda row: (
                0 if row["over_limit"] and not row["waiver_valid"] else 1,
                0 if row["duplicate_author_papers"] else 1,
                -row["publication_paper_count"],
                row["normalized_author_name"],
            ),
        )
    counts = {
        "all": len(all_rows),
        **{
            key: sum(1 for row in all_rows if predicate(row))
            for key, predicate in predicates.items()
        },
    }
    filter_options = [
        {"value": "all", "label": "All"},
        {"value": "attention", "label": "Needs attention"},
        {"value": "over_limit", "label": "Over limit"},
        {"value": "duplicates", "label": "Duplicate in paper"},
        {"value": "allowed", "label": "Allowed exceptions"},
    ]
    page = paginate_worklist(
        request,
        rows,
        hx_target="#author-count-worklist",
        indicator_id="author-count-loading",
    )
    displayed_rows = hydrate_author_count_rows(
        page.items,
        context=publication_context,
    )
    return render(
        request,
        "submissions/author_count.html",
        {
            "rows": displayed_rows,
            "q": q,
            "current_filter": current_filter,
            "current_sort": current_sort,
            "sort_options": [
                {"value": "attention", "label": "Needs attention first"},
                {"value": "paper_count_desc", "label": "Publication paper count descending"},
                {"value": "paper_count_asc", "label": "Publication paper count ascending"},
                {"value": "name", "label": "Author name"},
            ],
            "filter_options": [
                {**option, "count": counts[option["value"]]}
                for option in filter_options
            ],
            "pagination": page,
        },
    )
def export_reports(request):
    if request.method == "POST":
        action = request.POST.get("action")
        exporters = {
            "active": reports.export_active_versions,
            "old": reports.export_old_versions,
            "errors": reports.export_error_report,
            "authors": reports.export_author_count,
            "publication_package": reports.export_publication_package,
            "publication_package_force": lambda: reports.export_publication_package(force=True),
            "all": reports.export_all_reports,
        }
        if action in exporters:
            try:
                exported_path = Path(exporters[action]())
            except reports.PublicationPackageBlocked as exc:
                logger.warning("Publication package blocked: %s", exc)
                if action == "publication_package_force":
                    messages.error(request, f"Draft package could not be created: {exc}")
                    return redirect("submissions:export_reports")
                blockers = exc.blockers
                return render(
                    request,
                    "submissions/export_reports.html",
                    {
                        "export_error": {
                            "title": "Publication package is not ready",
                            "message": (
                                "Fix these blockers before downloading the final ZIP. "
                                "If you need an intermediate copy, download a draft package; "
                                "it may skip missing files and is not final-ready."
                            ),
                            "detail": str(exc),
                            "blockers": blockers[:20],
                            "total_blockers": len(blockers),
                            "remaining_blockers": max(len(blockers) - 20, 0),
                            "allow_force_download": True,
                        }
                    },
                    status=200,
                )
            except Exception as exc:
                logger.exception("Export failed")
                messages.error(request, f"Export failed: {exc}")
                return redirect("submissions:export_reports")
            if not exported_path.exists():
                messages.error(request, f"Export failed. File was not created: {exported_path}")
                return redirect("submissions:export_reports")
            return FileResponse(
                exported_path.open("rb"),
                as_attachment=True,
                filename=exported_path.name,
            )
        messages.error(request, "Unknown export type.")
        return redirect("submissions:export_reports")
    return render(request, "submissions/export_reports.html")
