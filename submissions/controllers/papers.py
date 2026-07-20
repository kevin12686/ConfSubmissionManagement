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
from submissions.services.audit import audit_failure, audit_success
from submissions.services.verification import (
    evaluate_submission,
    mark_not_publishing,
    unverify_submission,
    undo_not_publishing,
    verification_rows,
    verify_submission,
)
from submissions.application.commands import apply_paper_master_preview
from submissions.application.pagination import paginate_worklist
from submissions.application.selectors import paper_master_list_context, search_query


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
def _search_query(request):
    return search_query(request)


def initial_paper_list(request):
    q = _search_query(request)
    context = paper_master_list_context(
        q,
        request.GET.get("sort", "paper_id_asc"),
        page_builder=lambda items: paginate_worklist(
            request,
            items,
            scroll_anchor="paper-master-worklist",
        ),
    )
    return render(
        request,
        "submissions/initial_paper_list.html",
        context,
    )


def initial_paper_form(request, pk=None):
    paper = get_object_or_404(InitialPaper, pk=pk) if pk else None
    form = InitialPaperForm(request.POST or None, instance=paper)
    if request.method == "POST" and form.is_valid():
        paper = form.save()
        audit_success(
            "paper_master_save",
            "Paper master record saved.",
            request=request,
            object_type="InitialPaper",
            paper_id=paper.paper_id,
            changed_fields=form.changed_data,
        )
        messages.success(request, "Paper master record saved.")
        return redirect("submissions:initial_paper_list")
    return render(request, "submissions/initial_paper_form.html", {"form": form, "paper": paper})


def initial_paper_delete(request, pk):
    paper = get_object_or_404(InitialPaper, pk=pk)
    if request.method == "POST":
        paper_id = paper.paper_id
        paper.delete()
        audit_success(
            "paper_master_delete",
            "Paper master record deleted.",
            request=request,
            object_type="InitialPaper",
            paper_id=paper_id,
        )
        messages.success(request, "Paper master record deleted.")
        return redirect("submissions:initial_paper_list")
    return render(request, "submissions/confirm_delete.html", {"object": paper, "type": "paper master record"})


def import_initial_papers_view(request):
    if request.method == "POST" and request.POST.get("action") == "apply_preview":
        try:
            result = apply_paper_master_preview(
                request.POST.get("preview_token", ""),
                notes_policy=request.POST.get("notes_policy", "preserve_existing_notes"),
            )
            messages.success(request, result.message)
        except Exception as exc:
            logger.exception("Paper master apply failed")
            audit_failure("paper_master_import_apply", exc, "Paper master import apply failed.", request=request)
            messages.error(request, f"Apply failed: {exc}")
        return redirect("submissions:initial_paper_list")

    form = ImportFileForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = preview_initial_import(form.cleaned_data["file"])
            return render(
                request,
                "submissions/import_preview.html",
                {
                    "preview": preview,
                    "apply_url": reverse("submissions:import_initial_papers"),
                    "cancel_url": reverse("submissions:initial_paper_list"),
                    "title": "Paper Master Import Preview",
                },
            )
        except Exception as exc:
            logger.exception("Paper master import failed")
            audit_failure("paper_master_import_preview", exc, "Paper master import preview failed.", request=request)
            messages.error(request, f"Preview failed: {exc}")
    return redirect("submissions:initial_paper_list")
