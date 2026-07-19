import csv
import uuid
import logging
import shutil
from pathlib import Path

from django.contrib import messages
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
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
from submissions.models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    FinalSubmissionFileState,
    FinalSubmissionIdentityState,
    FinalSubmissionPlagiarismState,
    FinalSubmissionPublicationState,
    FinalSubmissionReviewState,
    InitialPaper,
    PaperAuthor,
)
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
from submissions.services.storage_inventory import (
    CLEANUP_CONFIRMATION_TEXT,
    apply_storage_cleanup,
    build_storage_inventory,
    preview_storage_cleanup,
    sync_publication_pdf_debug_folder,
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
from submissions.services.audit import (
    archive_and_clear_audit_log,
    audit_failure,
    audit_preview,
    audit_requested,
    audit_success,
)
from submissions.services.grobid_extractor import check_grobid_api
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
def _clear_folder_contents(folder):
    folder.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in folder.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


def _clear_data_files(settings_obj):
    folders = {
        Path(django_settings.MEDIA_ROOT),
        Path("data") / "media",
        Path("data") / "crosscheck_upload",
        Path("data") / "publication_pdf_debug",
        Path("data") / "import_previews",
        Path("data") / "storage_cleanup_previews",
        Path("data") / "system_state_backups",
        Path("data") / "system_state_restore_previews",
        Path("data") / "restored_external",
        Path("data") / "restored_external_folders",
        resolve_folder(settings_obj.incoming_folder),
        resolve_folder(settings_obj.active_final_folder),
        resolve_folder(settings_obj.publication_pdf_debug_folder),
        resolve_folder(settings_obj.old_versions_folder),
        resolve_folder(settings_obj.reports_folder),
        resolve_folder(settings_obj.extraction_results_folder),
        resolve_folder(settings_obj.plagiarism_reports_folder),
    }
    for default_folder in DEFAULT_FOLDER_SETTINGS.values():
        folders.add(Path(default_folder))
    folders = {folder if folder.is_absolute() else django_settings.BASE_DIR / folder for folder in folders}
    cleared = []
    for folder in sorted(folders, key=lambda path: str(path)):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
        removed = _clear_folder_contents(folder)
        cleared.append({"folder": str(folder), "removed": removed})
    return cleared
def app_settings(request):
    settings_obj = AppSetting.load()
    old_active_version_rule = settings_obj.active_version_rule
    active_version_change_report = request.session.pop("active_version_change_report", None)
    active_version_rule_preview = request.session.get("active_version_rule_preview")
    storage_cleanup_preview = None
    storage_cleanup_result = None
    storage_repair_result = None
    if request.method == "POST" and request.POST.get("action") == "reset_folders":
        for field_name, default_value in DEFAULT_FOLDER_SETTINGS.items():
            setattr(settings_obj, field_name, default_value)
        settings_obj.save(update_fields=list(DEFAULT_FOLDER_SETTINGS))
        audit_success(
            "settings_reset_folders",
            "Folder paths reset to defaults.",
            request=request,
            changed_fields=list(DEFAULT_FOLDER_SETTINGS),
        )
        messages.success(request, "Folder paths reset to data/... defaults.")
        return redirect("submissions:settings")

    if request.method == "POST" and request.POST.get("action") == "preview_storage_cleanup":
        cleanup_policy = request.POST.get("cleanup_policy", "generated_cache_or_orphan_output")
        storage_cleanup_preview = preview_storage_cleanup(cleanup_policy)
        messages.info(
            request,
            (
                f"{storage_cleanup_preview['policy_label']} preview created for {storage_cleanup_preview['file_count']} files "
                f"({storage_cleanup_preview['total_size_label']}). Nothing was deleted."
            ),
        )

    if request.method == "POST" and request.POST.get("action") == "apply_storage_cleanup":
        try:
            storage_cleanup_result = apply_storage_cleanup(
                request.POST.get("cleanup_token", ""),
                request.POST.get("cleanup_confirmation", "").strip(),
            )
            messages.success(
                request,
                (
                    f"Deleted {storage_cleanup_result['deleted_count']} files "
                    f"({storage_cleanup_result['deleted_size_label']})."
                ),
            )
            return redirect("submissions:settings")
        except ValueError as exc:
            messages.error(request, str(exc))

    if request.method == "POST" and request.POST.get("action") == "sync_publication_debug":
        try:
            storage_repair_result = sync_publication_pdf_debug_folder()
            messages.success(
                request,
                (
                    f"Publication PDF debug folder synced: {storage_repair_result['synced_count']} PDFs "
                    f"written, {storage_repair_result['skipped_count']} skipped."
                ),
            )
        except ValueError as exc:
            messages.error(request, str(exc))

    if request.method == "POST" and request.POST.get("action") == "cancel_active_rule_preview":
        request.session.pop("active_version_rule_preview", None)
        audit_success(
            "settings_active_version_rule_cancel",
            "Active version rule change preview cancelled.",
            request=request,
        )
        messages.info(request, "Active version rule change was cancelled. Settings were not changed.")
        return redirect("submissions:settings")

    if request.method == "POST" and request.POST.get("action") == "confirm_active_rule_change":
        preview = request.session.get("active_version_rule_preview")
        if not preview or request.POST.get("preview_token") != preview.get("token"):
            messages.error(request, "Active version rule preview expired. Preview the change again.")
            return redirect("submissions:settings")
        current_snapshot = _active_version_snapshot()
        if current_snapshot != preview.get("before_snapshot"):
            request.session.pop("active_version_rule_preview", None)
            messages.error(
                request,
                "Active versions changed after the preview was created. Preview the rule change again before applying.",
            )
            return redirect("submissions:settings")
        form = AppSettingForm(preview["form_data"], instance=settings_obj)
        if form.is_valid():
            saved_settings = form.save()
            from submissions.services.recompute import (
                recompute_active_and_duplicate_state,
            )

            recompute_active_and_duplicate_state()
            result_report = {
                **preview["report"],
                "applied": True,
            }
            request.session["active_version_change_report"] = result_report
            request.session.pop("active_version_rule_preview", None)
            audit_success(
                "settings_active_version_rule_apply",
                "Active final version rule change applied.",
                request=request,
                changed_fields=["active_version_rule"],
                before={"active_version_rule": preview["report"]["old_rule"]},
                after={"active_version_rule": preview["report"]["new_rule"]},
                result_counts={"changed_count": result_report["changed_count"]},
                extra={"changes": result_report["changes"][:50]},
            )
            messages.warning(
                request,
                (
                    f"Active final version rule changed. {result_report['changed_count']} "
                    "paper(s) changed active final version; review the report below."
                ),
            )
            return redirect("submissions:settings")
        messages.error(request, "Saved preview is no longer valid. Preview the settings change again.")
        request.session.pop("active_version_rule_preview", None)
        return redirect("submissions:settings")

    form_data = request.POST if request.POST.get("action") == "save_settings" else None
    form = AppSettingForm(form_data, instance=settings_obj)
    if request.method == "POST" and request.POST.get("action") == "save_settings" and form.is_valid():
        if form.cleaned_data["active_version_rule"] != old_active_version_rule:
            before_active_versions = _active_version_snapshot()
            after_active_versions = _active_version_snapshot_for_rule(
                form.cleaned_data["active_version_rule"]
            )
            changed_active_versions = _active_version_changes(
                before_active_versions,
                after_active_versions,
            )
            report = {
                "old_rule": old_active_version_rule,
                "new_rule": form.cleaned_data["active_version_rule"],
                "changed_count": len(changed_active_versions),
                "changes": changed_active_versions,
            }
            request.session["active_version_rule_preview"] = {
                "token": uuid.uuid4().hex,
                "form_data": request.POST.copy(),
                "before_snapshot": before_active_versions,
                "report": report,
            }
            audit_preview(
                "settings_active_version_rule_preview",
                "Active final version rule change preview created.",
                request=request,
                changed_fields=["active_version_rule"],
                before={"active_version_rule": old_active_version_rule},
                after={"active_version_rule": form.cleaned_data["active_version_rule"]},
                result_counts={"changed_count": len(changed_active_versions)},
                extra={"changes": changed_active_versions[:50]},
            )
            messages.warning(
                request,
                (
                    f"Preview active final version rule change: {len(changed_active_versions)} "
                    "paper(s) would change active final version. Confirm before applying."
                ),
            )
            return redirect("submissions:settings")
        form.save()
        audit_success(
            "settings_save",
            "Settings saved.",
            request=request,
            changed_fields=form.changed_data,
        )
        messages.success(request, "Settings saved.")
        return redirect("submissions:settings")
    active_version_rule_preview = request.session.get("active_version_rule_preview")
    folder_warnings = _folder_path_warnings(settings_obj)
    storage_inventory = build_storage_inventory()
    grobid_status = check_grobid_api(
        settings_obj.grobid_api_url,
        min(settings_obj.grobid_timeout_seconds or 2, 2),
    )
    return render(
        request,
        "submissions/settings.html",
        {
            "form": form,
            "folder_warnings": folder_warnings,
            "has_folder_warnings": any(folder_warnings.values()),
            "storage_inventory": storage_inventory,
            "storage_cleanup_preview": storage_cleanup_preview,
            "storage_cleanup_result": storage_cleanup_result,
            "storage_cleanup_confirmation_text": CLEANUP_CONFIRMATION_TEXT,
            "storage_repair_result": storage_repair_result,
            "active_version_change_report": active_version_change_report,
            "active_version_rule_preview": active_version_rule_preview,
            "grobid_status": grobid_status,
        },
    )


def grobid_health_check(request):
    api_url = request.GET.get("api_url", "").strip()
    try:
        timeout_seconds = int(request.GET.get("timeout", "2"))
    except (TypeError, ValueError):
        timeout_seconds = 2
    status = check_grobid_api(api_url, min(timeout_seconds or 2, 2))
    return JsonResponse(status)


def _active_version_snapshot():
    return {
        submission.paper_id_filled: {
            "final_submission_id": submission.final_submission_id,
            "submission_origin": submission.get_submission_origin_display(),
        }
        for submission in FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
        ).exclude(paper_id_filled="")
    }


def _active_version_snapshot_for_rule(rule):
    snapshot = {}
    paper_ids = (
        FinalSubmission.objects.filter(discarded=False)
        .exclude(paper_id_filled="")
        .order_by()
        .values_list("paper_id_filled", flat=True)
        .distinct()
    )
    for paper_id in paper_ids:
        submissions = list(
            FinalSubmission.objects.filter(paper_id_filled=paper_id, discarded=False)
        )
        editor_submissions = [
            submission
            for submission in submissions
            if submission.submission_origin == "editor_upload"
        ]
        candidate_submissions = editor_submissions or submissions
        selected = _select_active_candidate_for_rule(candidate_submissions, rule)
        if selected:
            snapshot[paper_id] = {
                "final_submission_id": selected.final_submission_id,
                "submission_origin": selected.get_submission_origin_display(),
            }
    return snapshot


def _select_active_candidate_for_rule(submissions, rule):
    from submissions.services.pdf_processor import final_submission_sort_key

    if not submissions:
        return None
    if rule == "upload_date":
        return max(
            submissions,
            key=lambda submission: (
                submission.upload_date,
                final_submission_sort_key(submission),
            ),
        )
    return max(submissions, key=final_submission_sort_key)


def _active_version_changes(before, after):
    changes = []
    for paper_id in sorted(set(before) | set(after)):
        old = before.get(paper_id)
        new = after.get(paper_id)
        old_final_id = old["final_submission_id"] if old else ""
        new_final_id = new["final_submission_id"] if new else ""
        if old_final_id == new_final_id:
            continue
        changes.append(
            {
                "paper_id": paper_id,
                "old_final_id": old_final_id or "--",
                "old_origin": old["submission_origin"] if old else "--",
                "new_final_id": new_final_id or "--",
                "new_origin": new["submission_origin"] if new else "--",
            }
        )
    return changes


def _folder_path_warnings(settings_obj):
    warnings = {}
    for field_name in DEFAULT_FOLDER_SETTINGS:
        raw_value = str(getattr(settings_obj, field_name) or "")
        path = Path(raw_value).expanduser()
        resolved = path if path.is_absolute() else django_settings.BASE_DIR / path
        resolved_text = str(resolved)
        if raw_value.startswith(TEMP_PATH_PREFIXES) or resolved_text.startswith(TEMP_PATH_PREFIXES):
            warnings[field_name] = (
                "This points to a macOS temporary folder. It can disappear after reboot or cleanup; "
                "use a data/... project folder unless this is intentional."
            )
        elif path.is_absolute() and not resolved_text.startswith(str(django_settings.BASE_DIR)):
            warnings[field_name] = (
                "This is outside the project folder. That is allowed, but make sure the folder is stable and backed up."
            )
        else:
            warnings[field_name] = ""
    return warnings


def clear_database(request):
    if request.method != "POST":
        return redirect("submissions:settings")

    confirmation = request.POST.get("confirmation", "").strip()
    clear_audit_log = request.POST.get("clear_audit_log") == "on"
    audit_requested(
        "clear_database_requested",
        "Clear Database requested.",
        request=request,
        result_counts={"clear_audit_log": clear_audit_log},
    )
    if confirmation != "CLEAR DATABASE":
        audit_failure(
            "clear_database_requested",
            "Confirmation text did not match.",
            "Clear Database was not applied.",
            request=request,
        )
        messages.error(request, 'Database was not cleared. Type "CLEAR DATABASE" to confirm.')
        return redirect("submissions:settings")

    counts = {
        "settings": AppSetting.objects.count(),
        "author_limit_waivers": AuthorLimitWaiver.objects.count(),
        "identity_state": FinalSubmissionIdentityState.objects.count(),
        "file_state": FinalSubmissionFileState.objects.count(),
        "review_state": FinalSubmissionReviewState.objects.count(),
        "publication_state": FinalSubmissionPublicationState.objects.count(),
        "plagiarism_state": FinalSubmissionPlagiarismState.objects.count(),
        "paper_authors": PaperAuthor.objects.count(),
        "final_submissions": FinalSubmission.objects.count(),
        "papers": InitialPaper.objects.count(),
    }
    settings_obj = AppSetting.load()
    archived_audit_path = None
    if clear_audit_log:
        archived_audit_path = archive_and_clear_audit_log("clear_database")
    cleared_folders = _clear_data_files(settings_obj)
    removed_items = sum(row["removed"] for row in cleared_folders)
    with transaction.atomic():
        PaperAuthor.objects.all().delete()
        AuthorLimitWaiver.objects.all().delete()
        FinalSubmissionIdentityState.objects.all().delete()
        FinalSubmissionFileState.objects.all().delete()
        FinalSubmissionReviewState.objects.all().delete()
        FinalSubmissionPublicationState.objects.all().delete()
        FinalSubmissionPlagiarismState.objects.all().delete()
        FinalSubmission.objects.all().delete()
        InitialPaper.objects.all().delete()
        AppSetting.objects.all().delete()
        AppSetting.load()

    messages.success(
        request,
        "System wiped clean: "
        f"{counts['papers']} papers, "
        f"{counts['final_submissions']} final submissions, "
        f"{counts['paper_authors']} author rows, "
        f"{counts['author_limit_waivers']} author-limit waivers, "
        f"{counts['settings']} settings row, "
        f"{removed_items} file/folder items removed. "
        "Settings and conference name were reset to defaults.",
    )
    audit_success(
        "clear_database_applied",
        "System wiped clean.",
        request=request,
        result_counts={**counts, "removed_file_items": removed_items},
        file_changes={
            "audit_log_archived": bool(archived_audit_path),
            "audit_archive_path": str(archived_audit_path) if archived_audit_path else "",
        },
    )
    return redirect("submissions:dashboard")
