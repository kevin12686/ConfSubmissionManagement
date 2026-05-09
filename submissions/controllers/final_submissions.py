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
    EditorUploadForm,
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
    rebuild_paper_authors,
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
from submissions.services.manual_edit import apply_final_submission_manual_edit
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
from submissions.services.editor_uploads import (
    create_editor_submission,
    discard_submission,
    undo_discard_submission,
)
from submissions.services.organized_list import (
    ORGANIZED_LIST_FILTER_OPTIONS,
    ORGANIZED_LIST_SORT_OPTIONS,
    organized_list_rows,
)
from submissions.services.pdf_processor import processed_pdf_rows, process_all_pdfs, scan_incoming_folder
from submissions.services.pdf_processor import determine_active_versions
from submissions.services.title_author_extraction import (
    evaluate_extracted_title_match,
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
from submissions.application.commands import apply_final_submission_preview
from submissions.application.selectors import final_submission_list_context, search_query


logger = logging.getLogger("submissions.views")

DEFAULT_FOLDER_SETTINGS = {
    "incoming_folder": "data/incoming",
    "active_final_folder": "data/active_final",
    "old_versions_folder": "data/old_versions",
    "reports_folder": "data/reports",
    "extraction_results_folder": "data/extraction_results",
    "plagiarism_reports_folder": "data/plagiarism_reports",
}
TEMP_PATH_PREFIXES = ("/var/", "/private/var/", "/tmp/", "/private/tmp/")
def _search_query(request):
    return search_query(request)
def publication_pdf(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    info = publication_pdf_info(submission)
    if not info["exists"]:
        raise Http404("Publication PDF not found.")
    path = Path(info["path"])
    return FileResponse(path.open("rb"), content_type="application/pdf", filename=path.name)


def plagiarism_report(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    if not submission.plagiarism_report_path:
        raise Http404("Plagiarism report not found.")
    path = Path(submission.plagiarism_report_path)
    if not path.exists():
        raise Http404("Plagiarism report not found.")
    return FileResponse(path.open("rb"), content_type="application/pdf", filename=path.name)


def _score_badge_level(value, threshold):
    if value is None:
        return "secondary"
    return "danger" if value > threshold else "light"


def final_submission_list(request):
    if request.method == "POST":
        submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
        action = request.POST.get("action")
        try:
            if action == "discard_submission":
                discard_submission(submission, request.POST.get("discard_notes", ""))
                messages.success(request, f"Final submission {submission.final_submission_id} discarded.")
            elif action == "undo_discard_submission":
                undo_discard_submission(submission)
                messages.success(request, f"Final submission {submission.final_submission_id} restored.")
            else:
                messages.error(request, "Unknown final submission action.")
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect("submissions:final_submission_list")
    q = _search_query(request)
    current_filter = request.GET.get("filter", "all")
    return render(
        request,
        "submissions/final_submission_list.html",
        final_submission_list_context(q, _score_badge_level, current_filter),
    )


def final_submission_form(request, pk=None):
    submission = get_object_or_404(FinalSubmission, pk=pk) if pk else None
    if submission and request.method == "POST" and request.POST.get("action") in {
        "discard_submission",
        "undo_discard_submission",
    }:
        try:
            if request.POST.get("action") == "discard_submission":
                discard_submission(submission, request.POST.get("discard_notes", ""))
                messages.success(request, f"Final submission {submission.final_submission_id} discarded.")
            else:
                undo_discard_submission(submission)
                messages.success(request, f"Final submission {submission.final_submission_id} restored.")
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect("submissions:final_submission_edit", pk=submission.pk)
    form = FinalSubmissionForm(
        request.POST or None, request.FILES or None, instance=submission
    )
    if request.method == "POST" and form.is_valid():
        _obj, summary = apply_final_submission_manual_edit(
            submission,
            form,
            form.cleaned_data.get("plagiarism_report_file"),
        )
        details = [
            label
            for key, label in [
                ("identity_recalculated", "identity/review recalculated"),
                ("pdf_reset", "PDF-dependent checks reset"),
                ("source_reset", "source-dependent checks reset"),
                ("plagiarism_stale", "plagiarism report marked stale"),
                ("active_versions_recalculated", "active versions recalculated"),
                ("corrected_files_archived", "corrected files invalidated"),
                ("review_status_guarded", "inconsistent review status blocked"),
                ("not_publishing_changed", "publishing decision updated"),
            ]
            if summary.get(key)
        ]
        suffix = f" ({'; '.join(details)})." if details else "."
        messages.success(request, f"Final submission saved{suffix}")
        return redirect("submissions:final_submission_list")
    context = {"form": form, "submission": submission}
    if submission:
        context["publication_pdf"] = publication_pdf_info(submission)
        context["publication_source"] = publication_source_info(submission)
    return render(request, "submissions/final_submission_form.html", context)


def editor_upload_form(request):
    form = EditorUploadForm(
        request.POST or None,
        request.FILES or None,
        initial_paper_id=request.GET.get("paper_id", ""),
    )
    if request.method == "POST" and form.is_valid():
        try:
            submission = create_editor_submission(
                paper=form.cleaned_data["paper"],
                pdf_file=form.cleaned_data["pdf_file"],
                source_file=form.cleaned_data.get("source_file"),
                notes=form.cleaned_data["notes"],
                final_submission_title=form.cleaned_data.get("final_submission_title", ""),
                final_submission_authors=form.cleaned_data.get("final_submission_authors", ""),
            )
            messages.success(
                request,
                f"Editor upload {submission.final_submission_id} created. Run Process PDFs before publication.",
            )
            return redirect("submissions:final_submission_list")
        except Exception as exc:
            messages.error(request, str(exc))
    return render(request, "submissions/editor_upload_form.html", {"form": form})


def final_submission_delete(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    if request.method == "POST":
        submission.delete()
        messages.success(request, "Final submission deleted.")
        return redirect("submissions:final_submission_list")
    return render(
        request, "submissions/confirm_delete.html", {"object": submission, "type": "final submission"}
    )


def import_final_submissions_view(request):
    if request.method == "POST" and request.POST.get("action") == "apply_preview":
        try:
            result = apply_final_submission_preview(request.POST.get("preview_token", ""))
            messages.success(request, result.message)
        except Exception as exc:
            logger.exception("Final submission apply failed")
            messages.error(request, f"Apply failed: {exc}")
        return redirect("submissions:final_submission_list")

    form = FinalSubmissionImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = preview_final_import(
                form.cleaned_data["file"], form.cleaned_data.get("submission_files") or []
            )
            return render(
                request,
                "submissions/import_preview.html",
                {
                    "preview": preview,
                    "apply_url": reverse("submissions:import_final_submissions"),
                    "cancel_url": reverse("submissions:final_submission_list"),
                    "title": "Final Submission Import Preview",
                },
            )
        except Exception as exc:
            logger.exception("Final submission import failed")
            messages.error(request, f"Preview failed: {exc}")
    return redirect("submissions:final_submission_list")
