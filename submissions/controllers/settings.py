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
from submissions.services.storage_inventory import (
    CLEANUP_CONFIRMATION_TEXT,
    apply_storage_cleanup,
    build_storage_inventory,
    preview_storage_cleanup,
    repair_publication_paths,
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
from submissions.services.pdf_processor import processed_pdf_rows, process_all_pdfs, scan_incoming_folder
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
        Path("data") / "import_previews",
        Path("data") / "storage_cleanup_previews",
        Path("data") / "system_state_backups",
        Path("data") / "system_state_restore_previews",
        Path("data") / "restored_external",
        Path("data") / "restored_external_folders",
        resolve_folder(settings_obj.incoming_folder),
        resolve_folder(settings_obj.active_final_folder),
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
    storage_cleanup_preview = None
    storage_cleanup_result = None
    storage_repair_result = None
    if request.method == "POST" and request.POST.get("action") == "reset_folders":
        for field_name, default_value in DEFAULT_FOLDER_SETTINGS.items():
            setattr(settings_obj, field_name, default_value)
        settings_obj.save(update_fields=list(DEFAULT_FOLDER_SETTINGS))
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

    if request.method == "POST" and request.POST.get("action") == "repair_storage_paths":
        storage_repair_result = repair_publication_paths(
            force=bool(request.POST.get("force_repair"))
        )
        messages.success(
            request,
            (
                f"Storage path repair completed: {storage_repair_result['pdf_repaired_count']} PDF paths "
                f"and {storage_repair_result['source_repaired_count']} source paths refreshed."
            ),
        )

    form_data = request.POST if request.POST.get("action") == "save_settings" else None
    form = AppSettingForm(form_data, instance=settings_obj)
    if request.method == "POST" and request.POST.get("action") == "save_settings" and form.is_valid():
        saved_settings = form.save()
        if saved_settings.active_version_rule != old_active_version_rule:
            determine_active_versions()
            _mark_duplicate_submissions()
        messages.success(request, "Settings saved.")
        return redirect("submissions:settings")
    folder_warnings = _folder_path_warnings(settings_obj)
    storage_inventory = build_storage_inventory()
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
        },
    )


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
    if confirmation != "CLEAR DATABASE":
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
    return redirect("submissions:dashboard")
