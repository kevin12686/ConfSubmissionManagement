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
    CROSSCHECK_EXPORT_ALL,
    CROSSCHECK_EXPORT_MISSING_RESULTS,
    CROSSCHECK_RESULT_TEMPLATE_COLUMNS,
    crosscheck_zip_path,
    import_crosscheck_results,
    prepare_crosscheck_upload,
    upload_crosscheck_reports,
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
def integration(request):
    crosscheck_export_result = None
    crosscheck_result = None
    report_upload_result = None
    system_state_preview = None
    system_state_restore_result = None
    crosscheck_result_form = ImportFileForm()
    crosscheck_export_form = CrossCheckExportForm()
    report_upload_form = CrossCheckReportUploadForm()
    system_state_restore_form = SystemStateRestoreForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action in {"prepare_crosscheck", "prepare_crosscheck_missing"}:
            crosscheck_export_form = CrossCheckExportForm(request.POST)
            if crosscheck_export_form.is_valid():
                try:
                    scope = (
                        CROSSCHECK_EXPORT_MISSING_RESULTS
                        if action == "prepare_crosscheck_missing"
                        else CROSSCHECK_EXPORT_ALL
                    )
                    crosscheck_export_result = prepare_crosscheck_upload(
                        crosscheck_export_form.cleaned_data["token"],
                        scope=scope,
                    )
                    crosscheck_export_result["download_url"] = reverse(
                        "submissions:download_crosscheck_zip"
                        if scope == CROSSCHECK_EXPORT_ALL
                        else "submissions:download_crosscheck_zip_scoped",
                        args=[crosscheck_export_result["token"]]
                        if scope == CROSSCHECK_EXPORT_ALL
                        else [crosscheck_export_result["token"], scope],
                    )
                    messages.success(request, "CrossCheck ZIP prepared.")
                except Exception as exc:
                    logger.exception("CrossCheck export failed")
                    messages.error(request, f"CrossCheck export failed: {exc}")
        elif action == "import_crosscheck":
            crosscheck_result_form = ImportFileForm(request.POST, request.FILES)
            if crosscheck_result_form.is_valid():
                try:
                    crosscheck_result = import_crosscheck_results(
                        crosscheck_result_form.cleaned_data["file"]
                    )
                    messages.success(request, "CrossCheck result CSV imported.")
                except Exception as exc:
                    logger.exception("CrossCheck result import failed")
                    messages.error(request, f"CrossCheck result import failed: {exc}")
        elif action == "upload_crosscheck_reports":
            report_upload_form = CrossCheckReportUploadForm(request.POST, request.FILES)
            if report_upload_form.is_valid():
                try:
                    report_upload_result = upload_crosscheck_reports(
                        report_upload_form.cleaned_data["report_files"]
                    )
                    messages.success(request, "CrossCheck report PDFs uploaded.")
                except Exception as exc:
                    logger.exception("CrossCheck report upload failed")
                    messages.error(request, f"CrossCheck report upload failed: {exc}")
        elif action == "preview_system_restore":
            system_state_restore_form = SystemStateRestoreForm(request.POST, request.FILES)
            if system_state_restore_form.is_valid():
                try:
                    system_state_preview = preview_system_state_restore(
                        system_state_restore_form.cleaned_data["snapshot"]
                    )
                    messages.warning(
                        request,
                        "Restore preview created. Review it carefully before applying.",
                    )
                except SystemStateError as exc:
                    messages.error(request, str(exc))
                except Exception as exc:
                    logger.exception("System state restore preview failed")
                    messages.error(request, f"Restore preview failed: {exc}")
        elif action == "apply_system_restore":
            token = request.POST.get("token", "")
            try:
                system_state_restore_result = apply_system_state_restore(
                    token,
                    request.POST.get("confirmation", ""),
                )
                messages.success(
                    request,
                    "System state restored. A pre-restore backup was saved before changes were applied.",
                )
                return redirect("submissions:dashboard")
            except SystemStateError as exc:
                messages.error(request, str(exc))
                try:
                    system_state_preview = load_restore_preview(token)
                except SystemStateError:
                    system_state_preview = None
            except Exception as exc:
                logger.exception("System state restore failed")
                messages.error(request, f"System state restore failed: {exc}")
    return render(
        request,
        "submissions/integration.html",
        {
            "crosscheck_export_form": crosscheck_export_form,
            "crosscheck_export_result": crosscheck_export_result,
            "crosscheck_result_form": crosscheck_result_form,
            "crosscheck_result": crosscheck_result,
            "report_upload_form": report_upload_form,
            "report_upload_result": report_upload_result,
            "system_state_restore_form": system_state_restore_form,
            "system_state_preview": system_state_preview,
            "system_state_restore_result": system_state_restore_result,
            "system_state_confirmation_text": CONFIRMATION_TEXT,
        },
    )


def download_crosscheck_zip(request, token):
    path = crosscheck_zip_path(token)
    if not path.exists():
        raise Http404("CrossCheck ZIP not found.")
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


def download_crosscheck_zip_scoped(request, token, scope):
    path = crosscheck_zip_path(token, scope=scope)
    if not path.exists():
        raise Http404("CrossCheck ZIP not found.")
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


def download_system_state(request):
    try:
        result = export_system_state()
    except Exception as exc:
        logger.exception("System state export failed")
        messages.error(request, f"System state export failed: {exc}")
        return redirect("submissions:integration")
    return FileResponse(
        result["path"].open("rb"),
        as_attachment=True,
        filename=result["filename"],
    )
