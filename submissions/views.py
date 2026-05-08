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

from .forms import (
    AppSettingForm,
    CrossCheckExportForm,
    CrossCheckReportUploadForm,
    FinalSubmissionForm,
    FinalSubmissionImportForm,
    FormattingUploadForm,
    ImportFileForm,
    InitialPaperForm,
)
from .models import AppSetting, FinalSubmission, InitialPaper, PaperAuthor
from .services.checks import (
    author_count_rows,
    dashboard_counts,
    error_report_rows,
    error_report_sections,
)
from .services.crosscheck import (
    CROSSCHECK_RESULT_TEMPLATE_COLUMNS,
    crosscheck_export_root,
    import_crosscheck_results,
    prepare_crosscheck_upload,
    upload_crosscheck_reports,
    validate_token,
)
from .services.import_export import (
    EXTERNAL_RESULTS_TEMPLATE_COLUMNS,
    FINAL_SUBMISSION_TEMPLATE_COLUMNS,
    INITIAL_PAPER_TEMPLATE_COLUMNS,
    _mark_duplicate_submissions,
)
from .services.import_preview import (
    apply_import_preview,
    preview_final_import,
    preview_initial_import,
)
from .services.integrations import import_external_results
from .services.formatting import formatting_preview_info, formatting_rows, update_formatting_submission
from .services.file_manager import corrected_pdf_needs_processing, publication_pdf_info, resolve_folder
from .services.organized_list import (
    ORGANIZED_LIST_FILTER_OPTIONS,
    ORGANIZED_LIST_SORT_OPTIONS,
    organized_list_rows,
)
from .services.pdf_processor import processed_pdf_rows, process_all_pdfs, scan_incoming_folder
from .services.pdf_processor import determine_active_versions
from .services.title_author_extraction import (
    extract_active_title_authors,
    extract_title_author_for_submission,
    title_author_extraction_rows,
    unverify_title_author,
    unverify_extracted_title,
    verify_extracted_title,
    verify_title_author,
)
from .services import reports
from .services.verification import (
    evaluate_submission,
    unverify_submission,
    verification_rows,
    verify_submission,
)


logger = logging.getLogger(__name__)


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
        Path("data") / "media",
        Path("data") / "crosscheck_upload",
        Path("data") / "import_previews",
        resolve_folder(settings_obj.incoming_folder),
        resolve_folder(settings_obj.active_final_folder),
        resolve_folder(settings_obj.old_versions_folder),
        resolve_folder(settings_obj.reports_folder),
        resolve_folder(settings_obj.extraction_results_folder),
        resolve_folder(settings_obj.plagiarism_reports_folder),
    }
    folders = {folder if folder.is_absolute() else django_settings.BASE_DIR / folder for folder in folders}
    cleared = []
    for folder in sorted(folders, key=lambda path: str(path)):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
        removed = _clear_folder_contents(folder)
        cleared.append({"folder": str(folder), "removed": removed})
    return cleared


def dashboard(request):
    return render(request, "submissions/dashboard.html", {"counts": dashboard_counts()})


def _search_query(request):
    return request.GET.get("q", "").strip()


def initial_paper_list(request):
    papers = InitialPaper.objects.all()
    q = _search_query(request)
    if q:
        papers = papers.filter(
            Q(paper_id__icontains=q)
            | Q(acceptance_status__icontains=q)
            | Q(title__icontains=q)
            | Q(authors__icontains=q)
        )
    return render(
        request,
        "submissions/initial_paper_list.html",
        {"papers": papers, "q": q, "import_form": ImportFileForm()},
    )


def initial_paper_form(request, pk=None):
    paper = get_object_or_404(InitialPaper, pk=pk) if pk else None
    form = InitialPaperForm(request.POST or None, instance=paper)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Paper master record saved.")
        return redirect("submissions:initial_paper_list")
    return render(request, "submissions/initial_paper_form.html", {"form": form, "paper": paper})


def initial_paper_delete(request, pk):
    paper = get_object_or_404(InitialPaper, pk=pk)
    if request.method == "POST":
        paper.delete()
        messages.success(request, "Paper master record deleted.")
        return redirect("submissions:initial_paper_list")
    return render(request, "submissions/confirm_delete.html", {"object": paper, "type": "paper master record"})


def import_initial_papers_view(request):
    if request.method == "POST" and request.POST.get("action") == "apply_preview":
        try:
            result = apply_import_preview(request.POST.get("preview_token", ""))
            messages.success(
                request,
                "Paper Master preview applied: "
                f"{result['new']} new, "
                f"{result['metadata_updated']} changed, "
                f"{result['paper_id_review_reset']} Paper ID reviews reset.",
            )
        except Exception as exc:
            logger.exception("Paper master apply failed")
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
            messages.error(request, f"Preview failed: {exc}")
    return redirect("submissions:initial_paper_list")


def organized_list(request):
    q = _search_query(request)
    current_filter = request.GET.get("filter", "needs_attention")
    current_sort = request.GET.get("sort", "needs_attention")
    rows, summary, settings_obj, current_filter, current_sort = organized_list_rows(
        q, current_filter, current_sort
    )
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
            "filter_options": ORGANIZED_LIST_FILTER_OPTIONS,
            "sort_options": ORGANIZED_LIST_SORT_OPTIONS,
        },
    )


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


def final_submission_list(request):
    submissions = FinalSubmission.objects.all()
    q = _search_query(request)
    if q:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=q)
            | Q(paper_id_filled__icontains=q)
            | Q(start2_paper_id_raw__icontains=q)
            | Q(final_submission_title__icontains=q)
            | Q(final_submission_authors__icontains=q)
            | Q(extracted_title__icontains=q)
            | Q(extracted_authors__icontains=q)
            | Q(processing_status__icontains=q)
        )
    return render(
        request,
        "submissions/final_submission_list.html",
        {"submissions": submissions, "q": q, "import_form": FinalSubmissionImportForm()},
    )


def final_submission_form(request, pk=None):
    submission = get_object_or_404(FinalSubmission, pk=pk) if pk else None
    form = FinalSubmissionForm(
        request.POST or None, request.FILES or None, instance=submission
    )
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if "extracted_title" in form.changed_data or "extracted_authors" in form.changed_data:
            obj.title_author_source = "manual"
            obj.title_author_imported_at = timezone.now()
            obj.title_author_extraction_status = "extracted"
            obj.title_author_extraction_message = "Manually edited."
            obj.title_author_verified = False
            obj.title_author_verified_at = None
            obj.extracted_title_verified = False
            obj.extracted_title_verified_at = None
            obj.extracted_title_auto_verify_blocked = False
        if "final_submission_title" in form.changed_data:
            obj.extracted_title_verified = False
            obj.extracted_title_verified_at = None
            obj.extracted_title_auto_verify_blocked = False
        if (
            "similarity_score" in form.changed_data
            or "single_similarity_score" in form.changed_data
            or "plagiarism_report_path" in form.changed_data
        ):
            obj.plagiarism_imported_at = timezone.now()
        obj.save()
        messages.success(request, "Final submission saved.")
        return redirect("submissions:final_submission_list")
    return render(
        request,
        "submissions/final_submission_form.html",
        {"form": form, "submission": submission},
    )


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
            result = apply_import_preview(request.POST.get("preview_token", ""))
            messages.success(
                request,
                "Final Submission preview applied: "
                f"{result['new']} new, "
                f"{result['metadata_updated']} changed, "
                f"{result['pdf_reset']} PDF resets, "
                f"{result['source_reset']} source resets, "
                f"{result['corrected_files_archived']} corrected file sets archived.",
            )
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
            return redirect("submissions:verify_paper_ids")

        corrected_paper_id = request.POST.get("corrected_paper_id", "").strip()
        if request.POST.get("action") == "use_suggestion":
            suggestion = evaluate_submission(submission).get("suggested_paper")
            corrected_paper_id = suggestion.paper_id if suggestion else corrected_paper_id
        verify_submission(submission, corrected_paper_id)
        messages.success(request, f"Final submission {submission.final_submission_id} verified.")
        return redirect("submissions:verify_paper_ids")

    all_rows = verification_rows(submissions)
    counts = {
        "all": len(all_rows),
        "needs_verification": sum(1 for row in all_rows if row["needs_verification"]),
        "verified_with_diff": sum(1 for row in all_rows if row["verified_with_diff"]),
        "identical": sum(1 for row in all_rows if row["is_identical"]),
        "title_mismatch": sum(1 for row in all_rows if row["status"] == "title_mismatch"),
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
        rows = [row for row in all_rows if row["status"] == "title_mismatch"]
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


def download_template(request, template_type):
    templates = {
        "initial": ("initial_papers_template.csv", INITIAL_PAPER_TEMPLATE_COLUMNS),
        "final": ("final_submissions_template.csv", FINAL_SUBMISSION_TEMPLATE_COLUMNS),
        "external": ("external_results_template.csv", EXTERNAL_RESULTS_TEMPLATE_COLUMNS),
        "crosscheck": ("crosscheck_results_template.csv", CROSSCHECK_RESULT_TEMPLATE_COLUMNS),
    }
    if template_type not in templates:
        return redirect("submissions:dashboard")

    filename, columns = templates[template_type]
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(columns)
    if template_type == "initial":
        writer.writerow(["R003", "R-to-R", "Example Master Title", "Ada Lovelace; Alan Turing"])
    elif template_type == "final":
        writer.writerow(["34", "R3", "Example Final Title", "Ada Lovelace and Alan Turing", "2026-05-07 09:00:00", "Submit_PDF, Submit_Source"])
    elif template_type == "crosscheck":
        writer.writerow(["R003_MAY082026.pdf", "12.35", "4.20", "TRUE", ""])
    else:
        writer.writerow(["9001", "R003", "Extracted Title", "Ada Lovelace; Alan Turing", "clear", "4.25", "1.50", "data/plagiarism_reports/R003.pdf"])
    return response


def process_pdfs_view(request):
    context = {"scan_result": None, "process_result": None}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "scan":
            context["scan_result"] = scan_incoming_folder()
            messages.success(request, "Incoming folder scanned.")
        elif action == "process":
            context["process_result"] = process_all_pdfs()
            messages.success(request, "PDF processing completed.")
    context["processed_rows"] = processed_pdf_rows()
    context["settings_obj"] = AppSetting.load()
    return render(request, "submissions/process.html", context)


def title_author_extraction(request):
    q = _search_query(request)
    current_filter = request.GET.get("filter", "needs_verification")
    allowed_filters = {"needs_verification", "missing", "title_mismatch", "verified", "errors", "all"}
    if current_filter not in allowed_filters:
        current_filter = "needs_verification"

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "extract_all":
            result = extract_active_title_authors()
            messages.success(
                request,
                f"Title/author extraction completed: {result['extracted']} extracted, {result['errors']} errors.",
            )
            return redirect("submissions:title_author_extraction")

        submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
        if action == "extract_one":
            if extract_title_author_for_submission(submission):
                messages.success(request, f"Extracted title/authors for {submission.final_submission_id}.")
            else:
                messages.error(
                    request,
                    f"Extraction failed for {submission.final_submission_id}. Check the row message.",
                )
        elif action == "verify":
            verify_title_author(submission)
            messages.success(request, f"Title/authors verified for {submission.final_submission_id}.")
        elif action == "unverify":
            unverify_title_author(submission)
            messages.success(request, f"Title/authors moved back to unverified for {submission.final_submission_id}.")
        elif action == "verify_extracted_title":
            verify_extracted_title(submission)
            messages.success(request, f"Extracted title match verified for {submission.final_submission_id}.")
        elif action == "unverify_extracted_title":
            unverify_extracted_title(submission)
            messages.success(request, f"Extracted title match moved back to unverified for {submission.final_submission_id}.")
        return redirect("submissions:title_author_extraction")

    filter_options = [
        {"value": "needs_verification", "label": "Needs review"},
        {"value": "verified", "label": "Reviewed"},
        {"value": "missing", "label": "Missing title/author"},
        {"value": "title_mismatch", "label": "Title mismatch"},
        {"value": "errors", "label": "Errors"},
        {"value": "all", "label": "All"},
    ]
    rows = title_author_extraction_rows(q, current_filter)
    tab_counts = {
        "needs_verification": len(title_author_extraction_rows(q, "needs_verification")),
        "verified": len(title_author_extraction_rows(q, "verified")),
    }
    return render(
        request,
        "submissions/title_author_extraction.html",
        {
            "rows": rows,
            "q": q,
            "current_filter": current_filter,
            "filter_options": filter_options,
            "tab_counts": tab_counts,
            "settings_obj": AppSetting.load(),
        },
    )


def formatting(request):
    q = _search_query(request)
    if request.method == "POST":
        submission = get_object_or_404(FinalSubmission, pk=request.POST.get("submission_id"))
        form = FormattingUploadForm(request.POST, request.FILES, submission=submission)
        if form.is_valid():
            update_formatting_submission(submission, form.cleaned_data)
            messages.success(request, f"Formatting record updated for {submission.final_submission_id}.")
            return redirect("submissions:formatting")
        messages.error(request, "Formatting update failed. Check the uploaded files and status.")

    rows = [
        {
            "submission": submission,
            "form": FormattingUploadForm(submission=submission),
            "publication_pdf": publication_pdf_info(submission),
            "preview": formatting_preview_info(submission),
            "needs_processing_after_formatting": corrected_pdf_needs_processing(submission),
        }
        for submission in formatting_rows(q)
    ]
    return render(request, "submissions/formatting.html", {"rows": rows, "q": q})


def active_versions(request):
    rows = [
        {"submission": submission, "publication_pdf": publication_pdf_info(submission)}
        for submission in FinalSubmission.objects.filter(active_version=True)
    ]
    return render(request, "submissions/active_versions.html", {"rows": rows})


def old_versions(request):
    submissions = FinalSubmission.objects.filter(active_version=False)
    return render(request, "submissions/old_versions.html", {"submissions": submissions})


def error_report(request):
    rows = error_report_rows()
    return render(
        request,
        "submissions/error_report.html",
        {"rows": rows, "sections": error_report_sections(rows)},
    )


def author_count(request):
    return render(request, "submissions/author_count.html", {"rows": author_count_rows()})


def integration(request):
    result = None
    crosscheck_export_result = None
    crosscheck_result = None
    report_upload_result = None
    form = ImportFileForm()
    crosscheck_result_form = ImportFileForm()
    crosscheck_export_form = CrossCheckExportForm()
    report_upload_form = CrossCheckReportUploadForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "import_external":
            form = ImportFileForm(request.POST, request.FILES)
            if form.is_valid():
                try:
                    result = import_external_results(form.cleaned_data["file"])
                    messages.success(request, "External results imported.")
                except Exception as exc:
                    logger.exception("External result import failed")
                    messages.error(request, f"Import failed: {exc}")
        elif action == "prepare_crosscheck":
            crosscheck_export_form = CrossCheckExportForm(request.POST)
            if crosscheck_export_form.is_valid():
                try:
                    crosscheck_export_result = prepare_crosscheck_upload(
                        crosscheck_export_form.cleaned_data["token"]
                    )
                    crosscheck_export_result["download_url"] = reverse(
                        "submissions:download_crosscheck_zip",
                        args=[crosscheck_export_result["token"]],
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
    return render(
        request,
        "submissions/integration.html",
        {
            "form": form,
            "result": result,
            "crosscheck_export_form": crosscheck_export_form,
            "crosscheck_export_result": crosscheck_export_result,
            "crosscheck_result_form": crosscheck_result_form,
            "crosscheck_result": crosscheck_result,
            "report_upload_form": report_upload_form,
            "report_upload_result": report_upload_result,
        },
    )


def download_crosscheck_zip(request, token):
    token = validate_token(token)
    path = crosscheck_export_root() / token / f"crosscheck_upload_{token}.zip"
    if not path.exists():
        raise Http404("CrossCheck ZIP not found.")
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


def app_settings(request):
    settings_obj = AppSetting.load()
    old_active_version_rule = settings_obj.active_version_rule
    form_data = request.POST if request.POST.get("action") == "save_settings" else None
    form = AppSettingForm(form_data, instance=settings_obj)
    if request.method == "POST" and request.POST.get("action") == "save_settings" and form.is_valid():
        saved_settings = form.save()
        if saved_settings.active_version_rule != old_active_version_rule:
            determine_active_versions()
            _mark_duplicate_submissions()
        messages.success(request, "Settings saved.")
        return redirect("submissions:settings")
    return render(request, "submissions/settings.html", {"form": form})


def clear_database(request):
    if request.method != "POST":
        return redirect("submissions:settings")

    confirmation = request.POST.get("confirmation", "").strip()
    if confirmation != "CLEAR DATABASE":
        messages.error(request, 'Database was not cleared. Type "CLEAR DATABASE" to confirm.')
        return redirect("submissions:settings")

    counts = {
        "paper_authors": PaperAuthor.objects.count(),
        "final_submissions": FinalSubmission.objects.count(),
        "papers": InitialPaper.objects.count(),
    }
    settings_obj = AppSetting.load()
    with transaction.atomic():
        PaperAuthor.objects.all().delete()
        FinalSubmission.objects.all().delete()
        InitialPaper.objects.all().delete()
    cleared_folders = _clear_data_files(settings_obj)
    removed_items = sum(row["removed"] for row in cleared_folders)

    messages.success(
        request,
        "Database and files cleared: "
        f"{counts['papers']} papers, "
        f"{counts['final_submissions']} final submissions, "
        f"{counts['paper_authors']} author rows removed, "
        f"{removed_items} file/folder items removed. Settings were kept.",
    )
    return redirect("submissions:dashboard")


def export_reports(request):
    if request.method == "POST":
        action = request.POST.get("action")
        exporters = {
            "active": reports.export_active_versions,
            "old": reports.export_old_versions,
            "errors": reports.export_error_report,
            "authors": reports.export_author_count,
            "publication_package": reports.export_publication_package,
            "all": reports.export_all_reports,
        }
        if action in exporters:
            try:
                exported_path = Path(exporters[action]())
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
