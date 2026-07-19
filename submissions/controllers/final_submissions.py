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
from django.utils.http import url_has_allowed_host_and_scheme
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
from submissions.services.manual_edit import (
    apply_final_submission_manual_edit,
    create_final_submission_manual,
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
    final_submission_display_pdf_info,
    final_submission_display_source_info,
    publication_debug_pdf_info,
    publication_pdf_info,
    publication_source_info,
    resolve_folder,
    sanitize_filename_part,
)
from submissions.services.editor_uploads import (
    apply_editor_upload_preview,
    cancel_editor_upload_preview,
    discard_submission,
    load_editor_upload_preview,
    preview_editor_upload,
    undo_discard_submission,
)
from submissions.services.organized_list import (
    ORGANIZED_LIST_FILTER_OPTIONS,
    ORGANIZED_LIST_SORT_OPTIONS,
    organized_list_rows,
)
from submissions.services.pdf_processor import processed_pdf_rows, process_all_pdfs
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
from submissions.services.audit import audit_failure, audit_success
from submissions.services.verification import (
    evaluate_submission,
    mark_not_publishing,
    unverify_submission,
    undo_not_publishing,
    verification_rows,
    verify_submission,
)
from submissions.application.commands import apply_final_submission_preview
from submissions.application.pagination import paginate_worklist
from submissions.application.selectors import final_submission_list_context, search_query


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
def publication_pdf(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    info = publication_pdf_info(submission)
    if not info["exists"]:
        raise Http404("Publication PDF not found.")
    path = Path(info["path"])
    return FileResponse(path.open("rb"), content_type="application/pdf", filename=path.name)


def publication_debug_pdf(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    info = publication_debug_pdf_info(submission)
    if not info["exists"]:
        raise Http404("Publication PDF debug copy not found.")
    path = Path(info["path"])
    return FileResponse(path.open("rb"), content_type="application/pdf", filename=path.name)


def publication_source(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    info = publication_source_info(submission)
    if not info["exists"]:
        raise Http404("Publication source file not found.")
    path = Path(info["path"])
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


def final_submission_display_pdf(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    info = final_submission_display_pdf_info(submission)
    if not info["exists"]:
        raise Http404("Final submission PDF not found.")
    path = Path(info["path"])
    return FileResponse(path.open("rb"), content_type="application/pdf", filename=path.name)


def final_submission_display_source(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    info = final_submission_display_source_info(submission)
    if not info["exists"]:
        raise Http404("Final submission source file not found.")
    path = Path(info["path"])
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


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
            audit_failure(
                "final_submission_list_action",
                exc,
                "Final submission list action failed.",
                request=request,
                submission=submission,
                extra={"action": action},
            )
            messages.error(request, str(exc))
        return redirect(request.get_full_path())
    q = _search_query(request)
    current_filter = request.GET.get("filter", "all")
    current_sort = request.GET.get("sort", "paper_id_asc")
    return render(
        request,
        "submissions/final_submission_list.html",
        final_submission_list_context(
            q,
            _score_badge_level,
            current_filter,
            current_sort,
            page_builder=lambda items: paginate_worklist(
                request,
                items,
                hx_target="#final-submission-worklist",
                indicator_id="final-submission-loading",
            ),
        ),
    )


def final_submission_form(request, pk=None):
    submission = get_object_or_404(FinalSubmission, pk=pk) if pk else None
    return_url = _safe_return_url(request)
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
            audit_failure(
                "final_submission_discard_action",
                exc,
                "Final submission discard action failed.",
                request=request,
                submission=submission,
                extra={"action": request.POST.get("action")},
            )
            messages.error(request, str(exc))
        edit_url = reverse("submissions:final_submission_edit", args=[submission.pk])
        if return_url:
            edit_url = f"{edit_url}?{urlencode({'next': return_url})}"
        return redirect(edit_url)
    form = FinalSubmissionForm(
        request.POST or None, request.FILES or None, instance=submission
    )
    if request.method == "POST" and form.is_valid():
        if submission is None:
            _obj, summary = create_final_submission_manual(
                form,
                form.cleaned_data.get("plagiarism_report_file"),
            )
            saved_action = "created"
        else:
            _obj, summary = apply_final_submission_manual_edit(
                submission,
                form,
                form.cleaned_data.get("plagiarism_report_file"),
            )
            saved_action = "saved"
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
        messages.success(request, f"Final submission {saved_action}{suffix}")
        if return_url:
            return redirect(return_url)
        return redirect("submissions:final_submission_list")
    context = {"form": form, "submission": submission, "return_url": return_url}
    if submission:
        context["publication_pdf"] = publication_pdf_info(submission)
        context["publication_source"] = publication_source_info(submission)
    return render(request, "submissions/final_submission_form.html", context)


def _safe_return_url(request):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    candidate = candidate.strip()
    if not candidate:
        return ""
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ""


def editor_upload_form(request):
    editor_upload_confirmation = None
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if action in {"replace_editor_upload_pdf", "cancel_editor_upload"}:
        try:
            payload = cancel_editor_upload_preview(
                request.POST.get("preview_token", ""),
                reason="replace_pdf" if action == "replace_editor_upload_pdf" else "canceled",
            )
        except Exception as exc:
            audit_failure(
                "editor_upload_preview_cancel",
                exc,
                "Editor upload preview could not be canceled.",
                request=request,
            )
            messages.error(request, str(exc))
            return redirect("submissions:editor_upload")
        if action == "cancel_editor_upload":
            messages.info(request, "Editor upload canceled. No submission or file was saved.")
            return redirect("submissions:final_submission_list")
        form = EditorUploadForm(
            initial={
                "paper": payload.get("paper_pk"),
                "final_submission_title": payload.get("final_submission_title", ""),
                "final_submission_authors": payload.get("final_submission_authors", ""),
                "notes": payload.get("notes", ""),
            },
            initial_paper_id=payload.get("paper_id", ""),
        )
        messages.info(request, "Choose the replacement PDF. The previous preview file was removed.")
        return render(
            request,
            "submissions/editor_upload_form.html",
            {"form": form, "editor_upload_confirmation": None},
        )
    if action == "confirm_editor_upload":
        form = EditorUploadForm(initial_paper_id=request.POST.get("paper_id", ""))
        try:
            submission = apply_editor_upload_preview(
                request.POST.get("preview_token", ""),
                confirmed=True,
            )
            messages.success(
                request,
                f"Editor upload {submission.final_submission_id} created. Run Process PDFs before publication.",
            )
            return redirect("submissions:final_submission_list")
        except Exception as exc:
            audit_failure("editor_upload_confirm", exc, "Editor upload confirmation failed.", request=request)
            messages.error(request, str(exc))
            return render(
                request,
                "submissions/editor_upload_form.html",
                {"form": form, "editor_upload_confirmation": None},
            )
    form = EditorUploadForm(
        request.POST or None,
        request.FILES or None,
        initial_paper_id=request.GET.get("paper_id", ""),
    )
    if request.method == "POST" and form.is_valid():
        try:
            preview = preview_editor_upload(form.cleaned_data)
            if preview["requires_confirmation"]:
                editor_upload_confirmation = preview
                return render(
                    request,
                    "submissions/editor_upload_form.html",
                    {"form": form, "editor_upload_confirmation": editor_upload_confirmation},
                )
            submission = apply_editor_upload_preview(preview["token"], confirmed=False)
            messages.success(
                request,
                f"Editor upload {submission.final_submission_id} created. Run Process PDFs before publication.",
            )
            return redirect("submissions:final_submission_list")
        except Exception as exc:
            audit_failure("editor_upload_preview", exc, "Editor upload preview failed.", request=request)
            messages.error(request, str(exc))
    return render(
        request,
        "submissions/editor_upload_form.html",
        {"form": form, "editor_upload_confirmation": editor_upload_confirmation},
    )


def editor_upload_preview_pdf(request, token):
    try:
        payload, _token_root = load_editor_upload_preview(token)
    except ValueError as exc:
        raise Http404(str(exc)) from exc
    path = Path(payload["pdf"]["path"])
    return FileResponse(
        path.open("rb"),
        content_type="application/pdf",
        filename=payload["pdf"].get("original_name") or path.name,
    )


def final_submission_delete(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    if request.method == "POST":
        final_id = submission.final_submission_id
        paper_id = submission.paper_id_filled
        submission.delete()
        audit_success(
            "final_submission_delete",
            "Final submission deleted.",
            request=request,
            object_type="FinalSubmission",
            paper_id=paper_id,
            final_submission_id=final_id,
        )
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
            audit_failure("final_submission_import_apply", exc, "Final submission import apply failed.", request=request)
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
            audit_failure("final_submission_import_preview", exc, "Final submission import preview failed.", request=request)
            messages.error(request, f"Preview failed: {exc}")
    return redirect("submissions:final_submission_list")
