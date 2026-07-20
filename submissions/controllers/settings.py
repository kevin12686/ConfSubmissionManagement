import csv
import hashlib
import json
import uuid
import logging
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
    discard_staged_application_data,
    load_storage_cleanup_preview,
    preview_storage_cleanup,
    restore_staged_application_data,
    stage_application_data_clear,
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
from submissions.services.settings_workflow import (
    apply_app_settings_form,
    reset_app_setting_folders,
    validate_settings_evidence,
)
from submissions.services.verification import (
    evaluate_submission,
    mark_not_publishing,
    unverify_submission,
    undo_not_publishing,
    verification_rows,
    verify_submission,
)
from submissions.services.workflow_evidence import (
    app_setting_evidence,
    make_evidence_token,
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


def app_settings(request):
    settings_obj = AppSetting.read()
    old_active_version_rule = settings_obj.active_version_rule
    active_version_change_report = request.session.pop("active_version_change_report", None)
    active_version_rule_preview = request.session.get("active_version_rule_preview")
    storage_cleanup_preview_token = ""
    storage_repair_result = None
    if request.method == "POST" and request.POST.get("action") == "reset_folders":
        try:
            settings_obj, before = reset_app_setting_folders(
                DEFAULT_FOLDER_SETTINGS,
                expected_evidence_token=request.POST.get("evidence_token", ""),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("submissions:settings")
        audit_success(
            "settings_reset_folders",
            "Folder paths reset to defaults.",
            request=request,
            changed_fields=list(DEFAULT_FOLDER_SETTINGS),
            before=before,
            after=app_setting_evidence(settings_obj),
        )
        messages.success(request, "Folder paths reset to data/... defaults.")
        return redirect("submissions:settings")

    if request.method == "POST" and request.POST.get("action") == "preview_storage_cleanup":
        cleanup_policy = request.POST.get("cleanup_policy", "generated_cache_or_orphan_output")
        try:
            cleanup_preview = preview_storage_cleanup(cleanup_policy)
            storage_cleanup_preview_token = cleanup_preview["token"]
            messages.info(
                request,
                (
                    f"{cleanup_preview['policy_label']} preview created for {cleanup_preview['file_count']} files "
                    f"({cleanup_preview['total_size_label']}). Nothing was deleted."
                ),
            )
        except ValueError as exc:
            messages.error(request, str(exc))

    if request.method == "POST" and request.POST.get("action") == "apply_storage_cleanup":
        try:
            cleanup_result = apply_storage_cleanup(
                request.POST.get("cleanup_token", ""),
                request.POST.get("cleanup_confirmation", "").strip(),
            )
            summary = (
                f"Deleted {cleanup_result['deleted_count']} files "
                f"({cleanup_result['deleted_size_label']})."
            )
            if (
                cleanup_result["skipped_count"]
                or cleanup_result["maintenance_warning_count"]
            ):
                details = []
                if cleanup_result["skipped_count"]:
                    skipped_label = (
                        "file"
                        if cleanup_result["skipped_count"] == 1
                        else "files"
                    )
                    details.append(
                        f"Kept {cleanup_result['skipped_count']} candidate "
                        f"{skipped_label}."
                    )
                if cleanup_result["maintenance_warning_count"]:
                    details.append(
                        "Cleanup housekeeping could not fully finish."
                    )
                messages.warning(
                    request,
                    (
                        f"{summary} {' '.join(details)} "
                        "Review Audit Log and create a new "
                        "preview before retrying."
                    ),
                )
            else:
                messages.success(request, summary)
            return redirect("submissions:settings")
        except ValueError as exc:
            storage_cleanup_preview_token = request.POST.get(
                "cleanup_token",
                "",
            ).strip()
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
        form = AppSettingForm(preview["form_data"], instance=settings_obj)
        if form.is_valid():
            from submissions.services.recompute import (
                recompute_active_and_duplicate_state,
            )

            try:
                with transaction.atomic():
                    # The rule and all derived active flags must become visible
                    # together. This also keeps the preview candidate set stable.
                    list(
                        FinalSubmission.objects.select_for_update()
                        .order_by("pk")
                        .values_list("pk", flat=True)
                    )
                    current_snapshot = _active_version_snapshot()
                    current_candidate_fingerprint = (
                        _active_version_candidate_fingerprint()
                    )
                    if (
                        current_snapshot != preview.get("before_snapshot")
                        or current_candidate_fingerprint
                        != preview.get("candidate_fingerprint")
                    ):
                        raise ValueError(
                            "Active versions changed after the preview was created, "
                            "or an active-version candidate changed. Preview the rule "
                            "change again before applying."
                        )
                    saved_settings, settings_before = apply_app_settings_form(
                        form,
                        expected_evidence_token=preview.get(
                            "settings_evidence_token",
                            "",
                        ),
                    )
                    recompute_active_and_duplicate_state()
            except ValueError as exc:
                request.session.pop("active_version_rule_preview", None)
                messages.error(request, str(exc))
                return redirect("submissions:settings")
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
                before=settings_before,
                after=app_setting_evidence(saved_settings),
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
            try:
                validate_settings_evidence(request.POST.get("evidence_token", ""))
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("submissions:settings")
            candidate_fingerprint = _active_version_candidate_fingerprint()
            before_active_versions = _active_version_snapshot()
            after_active_versions = _active_version_snapshot_for_rule(
                form.cleaned_data["active_version_rule"]
            )
            if candidate_fingerprint != _active_version_candidate_fingerprint():
                messages.error(
                    request,
                    "Active-version candidates changed while the preview was being "
                    "built. No settings were changed; preview the rule change again.",
                )
                return redirect("submissions:settings")
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
                "candidate_fingerprint": candidate_fingerprint,
                "settings_evidence_token": request.POST.get(
                    "evidence_token",
                    "",
                ),
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
        try:
            saved_settings, settings_before = apply_app_settings_form(
                form,
                expected_evidence_token=request.POST.get("evidence_token", ""),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("submissions:settings")
        audit_success(
            "settings_save",
            "Settings saved.",
            request=request,
            changed_fields=form.changed_data,
            before=settings_before,
            after=app_setting_evidence(saved_settings),
        )
        messages.success(request, "Settings saved.")
        return redirect("submissions:settings")
    active_version_rule_preview = request.session.get("active_version_rule_preview")
    folder_warnings = _folder_path_warnings(settings_obj)
    grobid_status = {
        "available": None,
        "level": "secondary",
        "label": "Not checked",
        "message": "GROBID health is checked after Settings opens.",
    }
    return render(
        request,
        "submissions/settings.html",
        {
            "form": form,
            "folder_warnings": folder_warnings,
            "has_folder_warnings": any(folder_warnings.values()),
            "storage_cleanup_preview_token": storage_cleanup_preview_token,
            "storage_repair_result": storage_repair_result,
            "active_version_change_report": active_version_change_report,
            "active_version_rule_preview": active_version_rule_preview,
            "grobid_status": grobid_status,
            "settings_evidence_token": make_evidence_token(
                "app-settings-edit",
                app_setting_evidence(AppSetting.read()),
            ),
        },
    )


def storage_inventory_panel(request):
    preview = None
    preview_error = ""
    preview_token = request.GET.get("preview_token", "").strip()
    if preview_token:
        try:
            preview = load_storage_cleanup_preview(preview_token)
        except ValueError as exc:
            preview_error = str(exc)
    template_name = (
        "submissions/partials/storage_inventory.html"
        if request.headers.get("HX-Request") == "true"
        else "submissions/storage_inventory.html"
    )
    return render(
        request,
        template_name,
        {
            "storage_inventory": build_storage_inventory(),
            "storage_cleanup_preview": preview,
            "storage_cleanup_preview_error": preview_error,
            "storage_cleanup_confirmation_text": CLEANUP_CONFIRMATION_TEXT,
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


def _active_version_candidate_fingerprint():
    digest = hashlib.sha256()
    candidates = FinalSubmission.objects.order_by("pk").values_list(
        "pk",
        "final_submission_id",
        "paper_id_filled",
        "submission_origin",
        "upload_date",
        "discarded",
        "active_version",
    )
    for (
        primary_key,
        final_submission_id,
        paper_id,
        submission_origin,
        upload_date,
        discarded,
        active_version,
    ) in candidates.iterator(chunk_size=1000):
        encoded_row = json.dumps(
            [
                primary_key,
                final_submission_id,
                paper_id,
                submission_origin,
                upload_date.isoformat() if upload_date else "",
                discarded,
                active_version,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded_row)
        digest.update(b"\n")
    return digest.hexdigest()


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
    staged_result = None
    try:
        staged_result = stage_application_data_clear(
            settings_obj,
            DEFAULT_FOLDER_SETTINGS.values(),
        )
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
            audit_requested(
                "clear_database_apply",
                "Clear Database changes validated; database commit pending.",
                request=request,
                result_counts=counts,
                file_changes={
                    "staged_folders": [
                        str(row["quarantine"])
                        for row in staged_result["staged"]
                    ],
                },
            )
    except Exception as exc:
        restore_error = None
        if staged_result:
            try:
                restore_staged_application_data(
                    staged_result["staged"]
                )
            except Exception as restore_exc:
                restore_error = restore_exc
                logger.exception(
                    "Clear Database rollback could not restore staged files."
                )
        recovery_paths = (
            [
                str(row["quarantine"])
                for row in staged_result["staged"]
                if row["quarantine"].exists()
            ]
            if staged_result
            else []
        )
        audit_failure(
            "clear_database_apply",
            exc,
            (
                "Clear Database failed; database changes were rolled back "
                "but one or more staged folders require manual recovery."
                if restore_error
                else "Clear Database failed; database changes were rolled "
                "back and staged files were restored."
            ),
            request=request,
            file_changes={
                "recovery_paths": recovery_paths,
                "restore_error": (
                    str(restore_error) if restore_error else ""
                ),
            },
        )
        if restore_error:
            messages.error(
                request,
                "Clear Database failed and database changes were rolled "
                "back, but some staged files require manual recovery. Do "
                "not continue publication work; review Audit Log.",
            )
        else:
            messages.error(
                request,
                "Clear Database failed. Database changes were rolled back "
                "and managed files were restored. Review the Audit Log.",
            )
        return redirect("submissions:settings")

    retained_quarantines = discard_staged_application_data(
        staged_result["staged"]
    )
    removed_items = sum(
        row["removed"] for row in staged_result["staged"]
    )
    archived_audit_path = None
    audit_archive_error = ""
    if clear_audit_log:
        try:
            archived_audit_path = archive_and_clear_audit_log(
                "clear_database"
            )
        except Exception as exc:
            audit_archive_error = str(exc)
            logger.exception(
                "Clear Database succeeded but audit archival failed."
            )
            messages.warning(
                request,
                "Database and managed files were cleared, but the Audit Log "
                "could not be archived. Review the current log.",
            )

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
    if staged_result["skipped_external"]:
        messages.warning(
            request,
            (
                f"{len(staged_result['skipped_external'])} configured external "
                "folder(s) were preserved because Clear Database does not "
                "recursively delete shared external locations."
            ),
        )
    if retained_quarantines:
        messages.warning(
            request,
            (
                f"{len(retained_quarantines)} staged folder(s) could not be "
                "removed. Their recovery paths are recorded in Audit Log."
            ),
        )
    try:
        audit_success(
            "clear_database_applied",
            "System wiped clean.",
            request=request,
            result_counts={**counts, "removed_file_items": removed_items},
            file_changes={
                "audit_log_archived": bool(archived_audit_path),
                "audit_archive_path": (
                    str(archived_audit_path) if archived_audit_path else ""
                ),
                "preserved_external_folders": staged_result[
                    "skipped_external"
                ],
                "retained_quarantines": retained_quarantines,
                "audit_archive_error": audit_archive_error,
            },
        )
    except OSError:
        logger.exception(
            "Clear Database succeeded but the completion audit could not be written."
        )
        messages.warning(
            request,
            "Database and managed files were cleared, but the completion Audit "
            "Log entry could not be written. The pre-commit audit entry remains.",
        )
    return redirect("submissions:dashboard")
