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
    apply_formatting_upload_preview,
    corrected_source_type_label,
    formatting_single_navigation,
    formatting_upload_confirmation,
    formatting_filter_counts,
    formatting_preview_info,
    formatting_rows,
    original_source_type_label,
    preview_formatting_upload,
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
    ManualOverrideError,
    apply_title_author_manual_override,
    extract_active_title_authors,
    extract_grobid_for_suspicious_rows,
    extract_title_author_for_submission,
    extract_title_author_with_grobid,
    extraction_overwrite_summary,
    filter_title_author_extraction_rows,
    grobid_availability_status,
    grobid_unavailable_message,
    set_title_author_review_status,
    title_author_extraction_rows,
    unverify_title_author,
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
from submissions.application.selectors import paper_note_summary


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
    return request.GET.get("q", "").strip()


def _worklist_return_url(request, fallback_name):
    candidate = request.POST.get("return_to", "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    if request.path == reverse(f"submissions:{fallback_name}"):
        return request.get_full_path()
    return reverse(f"submissions:{fallback_name}")


def organized_list(request):
    if request.method == "POST":
        submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
        action = request.POST.get("action")
        if action in {"approve_exception", "reapprove_exception", "remove_exception"}:
            rebuild_paper_authors()
            all_exception_rows, _ = exception_rows("all")
            exception_key = request.POST.get("exception_key", "")
            row = next((item for item in all_exception_rows if item["key"] == exception_key), None)
            if not row:
                messages.error(request, "Exception row was not found. Refresh and try again.")
            else:
                try:
                    if action in {"approve_exception", "reapprove_exception"}:
                        approve_exception(row, request.POST.get("reason", ""))
                        messages.success(request, f"{row['type_label']} exception allowed.")
                    else:
                        remove_exception(row)
                        messages.warning(request, f"{row['type_label']} exception removed.")
                except ValueError as exc:
                    messages.error(request, str(exc))
        elif action == "mark_duplicate_author_reviewed":
            submission.duplicate_author_review_status = "review_ok"
            submission.duplicate_author_review_notes = request.POST.get(
                "duplicate_author_review_notes", ""
            ).strip()
            submission.duplicate_author_reviewed_at = timezone.now()
            submission.save(
                update_fields=[
                    "duplicate_author_review_status",
                    "duplicate_author_review_notes",
                    "duplicate_author_reviewed_at",
                    "updated_at",
                ]
            )
            audit_success(
                "duplicate_author_review",
                "Duplicate author review marked OK.",
                request=request,
                submission=submission,
                after={
                    "duplicate_author_review_status": submission.duplicate_author_review_status,
                    "notes": submission.duplicate_author_review_notes,
                },
            )
            messages.success(
                request,
                f"Duplicate author review marked OK for {submission.final_submission_id}.",
            )
        elif action == "reset_duplicate_author_review":
            submission.duplicate_author_review_status = "pending"
            submission.duplicate_author_review_notes = ""
            submission.duplicate_author_reviewed_at = None
            submission.save(
                update_fields=[
                    "duplicate_author_review_status",
                    "duplicate_author_review_notes",
                    "duplicate_author_reviewed_at",
                    "updated_at",
                ]
            )
            audit_success(
                "duplicate_author_review_reset",
                "Duplicate author review moved back to pending.",
                request=request,
                submission=submission,
                reset_flags={"duplicate_author_review": True},
            )
            messages.warning(
                request,
                f"Duplicate author review moved back to pending for {submission.final_submission_id}.",
            )
        return redirect(request.get_full_path())

    q = _search_query(request)
    view_mode = request.GET.get("view", "checklist")
    if view_mode not in {"checklist", "compact"}:
        view_mode = "checklist"
    current_filter = request.GET.get("filter", "all") if view_mode == "checklist" else "all"
    current_sort = request.GET.get("sort", "needs_attention") if view_mode == "checklist" else "paper_id_asc"
    rows, summary, settings_obj, current_filter, current_sort = organized_list_rows(
        q, current_filter, current_sort
    )
    if view_mode == "compact":
        rows = [
            row
            for row in rows
            if row["row_type"] == "master" and row["submission"]
        ]
    note_summary = paper_note_summary()
    return render(
        request,
        "submissions/organized_list.html",
        {
            "rows": rows,
            "summary": summary,
            "settings_obj": settings_obj,
            "q": q,
            "current_filter": current_filter,
            "current_sort": current_sort,
            "view_mode": view_mode,
            "filter_options": ORGANIZED_LIST_FILTER_OPTIONS,
            "sort_options": ORGANIZED_LIST_SORT_OPTIONS,
            "note_summary": note_summary,
            "note_count": len(note_summary),
        },
    )
def verify_paper_ids(request):
    submissions = FinalSubmission.objects.all()
    q = _search_query(request)
    current_filter = request.GET.get("filter", "needs_verification")
    if q:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=q)
            | Q(start2_paper_id_raw__icontains=q)
            | Q(paper_id_filled__icontains=q)
            | Q(final_submission_title__icontains=q)
        )

    if request.method == "POST":
        submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
        if request.POST.get("action") == "unverify":
            unverify_submission(submission)
            messages.success(request, f"Final submission {submission.final_submission_id} moved back to unverified.")
            return redirect(_worklist_return_url(request, "verify_paper_ids"))
        if request.POST.get("action") == "mark_not_publishing":
            mark_not_publishing(
                submission,
                request.POST.get("publication_exclusion_reason", "unpaid"),
                request.POST.get("publication_exclusion_notes", ""),
            )
            messages.success(request, f"Final submission {submission.final_submission_id} marked Not Publishing.")
            return redirect(_worklist_return_url(request, "verify_paper_ids"))
        if request.POST.get("action") == "undo_not_publishing":
            undo_not_publishing(submission)
            messages.success(request, f"Final submission {submission.final_submission_id} moved back to publication review.")
            return redirect(_worklist_return_url(request, "verify_paper_ids"))

        corrected_paper_id = request.POST.get("corrected_paper_id", "").strip()
        if request.POST.get("action") == "use_suggestion":
            suggestion = evaluate_submission(submission).get("suggested_paper")
            corrected_paper_id = suggestion.paper_id if suggestion else corrected_paper_id
        try:
            verify_submission(submission, corrected_paper_id)
            messages.success(request, f"Final submission {submission.final_submission_id} verified.")
        except ValueError as exc:
            audit_failure("verify_paper_id", exc, "Paper ID verification failed.", request=request, submission=submission)
            messages.error(request, str(exc))
        return redirect(_worklist_return_url(request, "verify_paper_ids"))

    all_rows = verification_rows(submissions)
    counts = {
        "all": len(all_rows),
        "needs_verification": sum(1 for row in all_rows if row["needs_verification"]),
        "verified_with_diff": sum(1 for row in all_rows if row["verified_with_diff"]),
        "identical": sum(1 for row in all_rows if row["is_identical"]),
        "title_mismatch": sum(
            1
            for row in all_rows
            if row["status"] == "title_mismatch" and not row["is_verified"]
        ),
    }
    filter_options = [
        {"value": "needs_verification", "label": "Needs verification", "count": counts["needs_verification"]},
        {"value": "verified_with_diff", "label": "Verified with diff", "count": counts["verified_with_diff"]},
        {"value": "identical", "label": "Identical", "count": counts["identical"]},
        {"value": "title_mismatch", "label": "Title mismatch", "count": counts["title_mismatch"]},
        {"value": "all", "label": "All", "count": counts["all"]},
    ]
    if current_filter == "needs_verification":
        rows = [row for row in all_rows if row["needs_verification"]]
    elif current_filter == "verified_with_diff":
        rows = [row for row in all_rows if row["verified_with_diff"]]
    elif current_filter == "identical":
        rows = [row for row in all_rows if row["is_identical"]]
    elif current_filter == "title_mismatch":
        rows = [
            row
            for row in all_rows
            if row["status"] == "title_mismatch" and not row["is_verified"]
        ]
    else:
        current_filter = "all"
        rows = all_rows

    return render(
        request,
        "submissions/verify_paper_ids.html",
        {
            "rows": rows,
            "papers": InitialPaper.objects.all(),
            "q": q,
            "current_filter": current_filter,
            "filter_options": filter_options,
            "filter_counts": counts,
        },
    )
def title_author_extraction(request):
    q = _search_query(request)
    current_filter = request.GET.get("filter", "needs_verification")
    confirm_reextract_all = False
    allowed_filters = {
        "needs_verification",
        "pending",
        "red_flag",
        "review_ok",
        "reviewed_differences",
        "missing",
        "manual_override",
        "errors",
        "all",
    }
    legacy_filter_map = {
        "title_match": "review_ok",
        "title_needs_match": "needs_verification",
    }
    current_filter = legacy_filter_map.get(current_filter, current_filter)
    if current_filter not in allowed_filters:
        current_filter = "needs_verification"

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "extract_needs_review":
            result = extract_active_title_authors(mode="needs_review")
            audit_success(
                "title_author_extract_needs_review",
                "Title/author extraction completed for needs-review papers.",
                request=request,
                result_counts=result,
            )
            messages.success(
                request,
                f"Title/author extraction completed for needs-review papers: {result['extracted']} extracted, {result['errors']} errors, {result['skipped']} skipped.",
            )
            return redirect(_worklist_return_url(request, "title_author_extraction"))
        if action == "grobid_suspicious":
            if not AppSetting.load().grobid_enabled:
                messages.error(request, "GROBID fallback is disabled in Settings.")
                return redirect(_worklist_return_url(request, "title_author_extraction"))
            result = extract_grobid_for_suspicious_rows()
            if result.get("aborted"):
                messages.error(request, result["message"] + " No rows were processed.")
                return redirect(_worklist_return_url(request, "title_author_extraction"))
            if result.get("stopped"):
                messages.warning(
                    request,
                    (
                        "GROBID suspicious-row extraction stopped because the API became unavailable: "
                        f"{result['extracted']} extracted, {result['errors']} errors, {result['skipped']} skipped."
                    ),
                )
                return redirect(_worklist_return_url(request, "title_author_extraction"))
            messages.success(
                request,
                f"GROBID suspicious-row extraction completed: {result['extracted']} extracted, {result['errors']} errors, {result['skipped']} skipped.",
            )
            return redirect(_worklist_return_url(request, "title_author_extraction"))
        if action == "reextract_all_prompt":
            confirm_reextract_all = True
        elif action == "confirm_reextract_all":
            result = extract_active_title_authors(mode="all")
            audit_success(
                "title_author_reextract_all",
                "All active PDFs re-extracted.",
                request=request,
                result_counts=result,
            )
            messages.warning(
                request,
                f"All active PDFs re-extracted: {result['extracted']} extracted, {result['errors']} errors.",
            )
            return redirect(_worklist_return_url(request, "title_author_extraction"))
        elif action:
            submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
            if action == "extract_one":
                if extract_title_author_for_submission(submission):
                    messages.success(request, f"Extracted title/authors for {submission.final_submission_id}.")
                else:
                    messages.error(
                        request,
                        f"Extraction failed for {submission.final_submission_id}. Check the row message.",
                    )
            elif action == "grobid_one":
                if not AppSetting.load().grobid_enabled:
                    messages.error(request, "GROBID fallback is disabled in Settings.")
                elif extract_title_author_with_grobid(submission):
                    messages.success(request, f"GROBID extracted title/authors for {submission.final_submission_id}.")
                else:
                    error_detail = getattr(submission, "_last_grobid_error", "")
                    messages.error(
                        request,
                        f"GROBID extraction failed for {submission.final_submission_id}. Existing extraction was not changed."
                        + (f" Reason: {error_detail}" if error_detail else ""),
                    )
            elif action == "manual_override":
                try:
                    apply_title_author_manual_override(
                        submission,
                        request.POST.get("manual_extracted_title", ""),
                        request.POST.get("manual_extracted_authors", ""),
                        request.POST.get("manual_override_reason", ""),
                    )
                    messages.warning(
                        request,
                        f"Manual title/author override saved for {submission.final_submission_id}. Review OK is required again.",
                    )
                except ManualOverrideError as exc:
                    messages.error(request, str(exc))
            elif action == "verify":
                verify_title_author(submission)
                messages.success(request, f"Title/authors marked Review OK for {submission.final_submission_id}.")
            elif action == "unverify":
                unverify_title_author(submission)
                messages.success(request, f"Title/authors moved back to Pending for {submission.final_submission_id}.")
            elif action == "set_review_status":
                review_status = request.POST.get("review_status", "pending")
                set_title_author_review_status(submission, review_status)
                messages.success(
                    request,
                    f"Title/author review status updated to {submission.get_title_author_review_status_display()} for {submission.final_submission_id}.",
                )
            return redirect(_worklist_return_url(request, "title_author_extraction"))

    filter_options = [
        {"value": "needs_verification", "label": "Needs Review", "tab_label": "Needs Review", "badge_level": "warning", "group": "workflow"},
        {"value": "pending", "label": "Pending", "tab_label": "Pending", "badge_level": "warning", "group": "workflow"},
        {"value": "red_flag", "label": "Red Flag", "tab_label": "Red Flag", "badge_level": "danger", "group": "workflow"},
        {"value": "review_ok", "label": "Review OK", "tab_label": "Review OK", "badge_level": "success", "group": "workflow"},
        {"value": "all", "label": "All", "tab_label": "All", "badge_level": "secondary", "group": "workflow"},
        {"value": "reviewed_differences", "label": "Reviewed Differences", "tab_label": "Reviewed Differences", "badge_level": "info", "group": "tracked"},
        {"value": "missing", "label": "Missing Extraction", "tab_label": "Missing", "badge_level": "warning", "group": "tracked"},
        {"value": "errors", "label": "Extraction Errors", "tab_label": "Errors", "badge_level": "danger", "group": "tracked"},
        {"value": "manual_override", "label": "Manual Override", "tab_label": "Manual Override", "badge_level": "warning", "group": "tracked"},
    ]
    all_title_author_rows = title_author_extraction_rows(q, "all")
    rows = filter_title_author_extraction_rows(all_title_author_rows, current_filter)
    settings_obj = AppSetting.load()
    grobid_status = (
        grobid_availability_status(settings_obj)
        if settings_obj.grobid_enabled
        else {
            "available": False,
            "level": "secondary",
            "label": "Disabled",
            "message": "GROBID fallback is disabled in Settings.",
        }
    )
    tab_counts = {
        option["value"]: len(
            filter_title_author_extraction_rows(all_title_author_rows, option["value"])
        )
        for option in filter_options
    }
    for option in filter_options:
        option["count"] = tab_counts[option["value"]]
    return render(
        request,
        "submissions/title_author_extraction.html",
        {
            "rows": rows,
            "q": q,
            "current_filter": current_filter,
            "filter_options": filter_options,
            "tab_counts": tab_counts,
            "confirm_reextract_all": confirm_reextract_all,
            "overwrite_summary": extraction_overwrite_summary(),
            "settings_obj": settings_obj,
            "grobid_status": grobid_status,
            "grobid_unavailable_message": (
                grobid_unavailable_message(grobid_status)
                if settings_obj.grobid_enabled and not grobid_status["available"]
                else ""
            ),
        },
    )


def formatting(request):
    q = _search_query(request)
    current_filter = request.POST.get("filter") or request.GET.get("filter", "needs_attention")
    mode = request.POST.get("mode") or request.GET.get("mode", "list")
    valid_filters = {option["value"] for option in FORMAT_FILTER_OPTIONS}
    if current_filter not in valid_filters:
        current_filter = "needs_attention"
    if mode != "single":
        mode = "list"
    formatting_confirmation = None
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "confirm_formatting_upload":
            token = request.POST.get("preview_token", "")
            try:
                submission = apply_formatting_upload_preview(token)
                messages.success(request, _formatting_success_message(submission, mode))
                return _formatting_redirect_after_save(request, current_filter, q, mode)
            except Exception as exc:
                audit_failure("formatting_upload_confirm", exc, "Formatting upload confirmation failed.", request=request)
                messages.error(request, str(exc))
        elif action == "cancel_formatting_upload":
            audit_success(
                "formatting_upload_cancel",
                "Corrected file upload canceled.",
                request=request,
            )
            messages.info(request, "Corrected file upload canceled. No formatting files were changed.")
            return _formatting_redirect_after_save(request, current_filter, q, mode, stay_on_current=True)
        else:
            submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
            form = FormattingUploadForm(request.POST, request.FILES, submission=submission)
            if form.is_valid():
                try:
                    preview = preview_formatting_upload(submission, form.cleaned_data)
                    if preview.get("token") and preview.get("requires_confirmation"):
                        formatting_confirmation = {
                            **preview,
                            "submission": submission,
                            "mode": mode,
                            "filter": current_filter,
                            "q": q,
                        }
                        messages.warning(
                            request,
                            "Corrected PDF title does not match. Confirm before saving corrected files.",
                        )
                    elif preview.get("token"):
                        apply_formatting_upload_preview(preview["token"])
                        messages.success(request, _formatting_success_message(submission, mode))
                        return _formatting_redirect_after_save(request, current_filter, q, mode)
                    else:
                        update_formatting_submission(submission, form.cleaned_data)
                        messages.success(request, _formatting_success_message(submission, mode))
                        return _formatting_redirect_after_save(request, current_filter, q, mode)
                except Exception as exc:
                    audit_failure("formatting_update", exc, "Formatting update failed.", request=request, submission=submission)
                    messages.error(request, f"Formatting update failed: {exc}")
            else:
                messages.error(request, "Formatting update failed. Check the uploaded files and status.")

    all_submissions = list(formatting_rows(q, current_filter))
    single_navigation = None
    if mode == "single":
        requested_id = request.POST.get("submission_id") or request.GET.get("submission")
        current_submission = next(
            (submission for submission in all_submissions if str(submission.pk) == str(requested_id)),
            all_submissions[0] if all_submissions else None,
        )
        all_submissions = [current_submission] if current_submission else []
        single_navigation = formatting_single_navigation(current_submission, q, current_filter)
    rows = [
        {
            "submission": submission,
            "form": FormattingUploadForm(submission=submission),
            "publication_pdf": publication_pdf_info(submission),
            "publication_source": publication_source_info(submission),
            "preview": formatting_preview_info(submission),
            "needs_processing_after_formatting": corrected_pdf_needs_processing(submission),
            "original_source_type_label": original_source_type_label(submission),
            "corrected_source_type_label": corrected_source_type_label(submission),
        }
        for submission in all_submissions
    ]
    filter_counts = formatting_filter_counts(q)
    filter_options = [
        {**option, "count": filter_counts.get(option["value"], 0)}
        for option in FORMAT_FILTER_OPTIONS
    ]
    return render(
        request,
        "submissions/formatting.html",
        {
            "rows": rows,
            "q": q,
            "current_filter": current_filter,
            "filter_options": filter_options,
            "mode": mode,
            "single_navigation": single_navigation,
            "formatting_confirmation": formatting_confirmation,
        },
    )


def _formatting_success_message(submission, mode):
    if mode == "single":
        return (
            f"Formatting saved for {submission.final_submission_id}. "
            "Review this paper, then go next when ready."
        )
    return f"Formatting record updated for {submission.final_submission_id}."


def _formatting_redirect_after_save(request, current_filter, q, mode, stay_on_current=False):
    query = {"filter": current_filter}
    if q:
        query["q"] = q
    if mode == "single":
        current_submission = request.POST.get("submission_id", "")
        if current_submission:
            query["mode"] = "single"
            query["submission"] = current_submission
            return redirect(f"{reverse('submissions:formatting')}?{urlencode(query)}")
        messages.success(request, "Single Paper Mode complete for the current filter.")
    return redirect(f"{reverse('submissions:formatting')}?{urlencode(query)}")


def not_publishing_list(request):
    q = _search_query(request)
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))

    if request.method == "POST":
        submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
        action = request.POST.get("action")
        if action == "mark_not_publishing":
            mark_not_publishing(
                submission,
                request.POST.get("publication_exclusion_reason", "unpaid"),
                request.POST.get("publication_exclusion_notes", ""),
            )
            messages.success(request, f"Final submission {submission.final_submission_id} marked Not Publishing.")
        elif action == "undo_not_publishing":
            undo_not_publishing(submission)
            messages.success(request, f"Final submission {submission.final_submission_id} moved back to publication review.")
        return redirect("submissions:not_publishing_list")

    needs_decision = FinalSubmission.objects.filter(
        active_version=True,
        excluded_from_publication=False,
        discarded=False,
    ).exclude(paper_id_filled__in=valid_ids)
    excluded = FinalSubmission.objects.filter(excluded_from_publication=True, discarded=False)

    if q:
        search_filter = (
            Q(final_submission_id__icontains=q)
            | Q(start2_paper_id_raw__icontains=q)
            | Q(paper_id_filled__icontains=q)
            | Q(final_submission_title__icontains=q)
            | Q(final_submission_authors__icontains=q)
            | Q(publication_exclusion_notes__icontains=q)
        )
        needs_decision = needs_decision.filter(search_filter)
        excluded = excluded.filter(search_filter)

    needs_decision = needs_decision.order_by("paper_id_filled", "final_submission_id")
    excluded = list(
        excluded.order_by("-active_version", "-publication_excluded_at", "paper_id_filled", "final_submission_id")
    )
    active_by_paper = {
        submission.paper_id_filled: submission
        for submission in FinalSubmission.objects.filter(
            active_version=True, discarded=False
        ).exclude(paper_id_filled="")
    }
    for submission in excluded:
        replacement = active_by_paper.get(submission.paper_id_filled)
        if replacement and replacement.pk == submission.pk:
            replacement = None
        submission.version_state_label = (
            "Current final" if submission.active_version else "Inactive old version"
        )
        submission.version_state_level = "secondary" if submission.active_version else "light text-dark"
        submission.origin_label = submission.get_submission_origin_display()
        submission.active_replacement = replacement
    return render(
        request,
        "submissions/not_publishing_list.html",
        {
            "q": q,
            "needs_decision": needs_decision,
            "excluded": excluded,
            "needs_decision_count": needs_decision.count(),
            "excluded_count": len(excluded),
            "active_excluded_count": sum(1 for item in excluded if item.active_version),
        },
    )
def exceptions_center(request):
    current_filter = request.GET.get("filter", "not_allowed")
    q = request.GET.get("q", "").strip()
    current_type = request.GET.get("type", "all")
    all_rows, _resolved_filter = exception_rows("all")
    valid_filters = {option["value"] for option in EXCEPTION_FILTER_OPTIONS}
    if current_filter not in valid_filters:
        current_filter = "not_allowed"
    status_rows = (
        all_rows
        if current_filter == "all"
        else [row for row in all_rows if row["status"] == current_filter]
    )
    type_options = [
        {"value": "all", "label": "All exception types"},
        {"value": "page", "label": "Page count"},
        {"value": "author_number", "label": "Authors in paper"},
        {"value": "author_limit", "label": "Author paper count"},
        {"value": "plagiarism_percent", "label": "Plagiarism %"},
        {"value": "single_percent", "label": "Single %"},
    ]
    valid_types = {option["value"] for option in type_options}
    if current_type not in valid_types:
        current_type = "all"
    rows = status_rows
    if current_type != "all":
        rows = [row for row in rows if row["type"] == current_type]
    if q:
        lowered_query = q.casefold()
        rows = [
            row
            for row in rows
            if lowered_query
            in " ".join(
                [
                    row.get("subject", ""),
                    row.get("paper_ids", ""),
                    row.get("final_submission_id", ""),
                    row.get("type_label", ""),
                    row.get("reason", ""),
                ]
            ).casefold()
        ]

    if request.method == "POST":
        rebuild_paper_authors()
        all_rows, _ = exception_rows("all")
        exception_key = request.POST.get("exception_key", "")
        action = request.POST.get("action")
        row = next((item for item in all_rows if item["key"] == exception_key), None)
        if not row:
            messages.error(request, "Exception row was not found. Refresh and try again.")
            return redirect(_worklist_return_url(request, "exceptions_center"))
        try:
            if action in {"approve_exception", "reapprove_exception"}:
                approve_exception(row, request.POST.get("reason", ""))
                messages.success(request, f"{row['type_label']} exception allowed.")
            elif action == "remove_exception":
                remove_exception(row)
                messages.warning(request, f"{row['type_label']} exception removed.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect(_worklist_return_url(request, "exceptions_center"))

    counts = exception_counts(all_rows)
    filter_options = [
        {**option, "count": counts.get(option["value"], 0)}
        for option in EXCEPTION_FILTER_OPTIONS
    ]
    return render(
        request,
        "submissions/exceptions.html",
        {
            "rows": rows,
            "current_filter": current_filter,
            "filter_options": filter_options,
            "counts": counts,
            "q": q,
            "current_type": current_type,
            "type_options": type_options,
        },
    )
