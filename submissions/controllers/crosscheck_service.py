import hashlib
import io
import zipfile
from pathlib import Path

from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from submissions.models import AppSetting, CrossCheckServiceJob, CrossCheckServiceResult
from submissions.services.audit import audit_failure, audit_success
from submissions.services.crosscheck_client import (
    CREDENTIAL_FIELDS, CrossCheckClient, read_service_secrets, save_service_secrets,
)
from submissions.services.crosscheck_service import (
    _result_archive, apply_service_results, cancel_service_preview, job_evidence_token,
    preview_service_job, refresh_service_job, start_service_job, stop_service_job,
    repreview_service_results,
    retry_service_downloads,
)
from submissions.services.crosscheck_service_history import (
    attempt_archive, batch_context, current_result, result_pdf_bytes, retry_candidates,
)


@require_POST
def crosscheck_service_connection(request):
    try:
        url = request.POST.get("url", AppSetting.read().crosscheck_service_url)
        result = CrossCheckClient(url, credentials=_entered_credentials(request)).folders()
        audit_success("crosscheck_service_connection", "CrossCheck service folders loaded.")
        return JsonResponse({"available": True, "label": "Ready", "folders": result["folders"], "username": result.get("username", "Service default")})
    except (ValueError, OSError) as exc:
        audit_failure("crosscheck_service_connection", exc)
        return JsonResponse({"available": False, "label": "Unavailable", "message": str(exc)}, status=400)


@require_POST
def crosscheck_service_credentials(request):
    try:
        # Settings evidence protects a credential update from targeting a host
        # changed in another browser while this form was open.
        from submissions.services.settings_workflow import validate_settings_evidence
        current = validate_settings_evidence(request.POST.get("evidence_token", ""))
        url = request.POST.get("url", current.crosscheck_service_url)
        save_service_secrets(url, _entered_credentials(request), remove=request.POST.get("remove") == "1")
        message = "Optional CrossCheck credentials updated for this service URL."
        if request.headers.get("Accept") == "application/json":
            secrets = read_service_secrets(url)
            return JsonResponse({"message": message,
                "account_label": "IEEE override configured" if secrets.get("ieee_username") else "Using service default IEEE account",
                "login_label": "Service login configured" if secrets.get("web_username") else "Service login not configured"})
        messages.success(request, message)
    except (ValueError, OSError) as exc:
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"message": str(exc)}, status=400)
        messages.error(request, str(exc))
    return redirect("submissions:settings")


def _entered_credentials(request):
    return {key: request.POST.get(key, "").strip() if key.endswith("username") else request.POST.get(key, "")
            for key in CREDENTIAL_FIELDS}


@require_POST
def crosscheck_service_preview(request):
    try:
        job = preview_service_job(request.POST.get("scope", "missing_results"))
        return redirect("submissions:crosscheck_service_job", pk=job.pk)
    except Exception as exc:
        messages.error(request, f"CrossCheck preview failed: {exc}")
        return redirect("submissions:integration")


def _job_page_context(request, job, *, retry_error="", show_retry=False):
    context = batch_context(job)
    context["poll_enabled"] = context["poll_enabled"] and AppSetting.read().crosscheck_service_enabled
    context["retry_form"] = None
    if show_retry or request.GET.get("retry") == "1":
        try:
            review = retry_candidates(job)
            review.update(wait_minutes=request.POST.get("wait_minutes", context["retry_wait_minutes"]),
                          error=retry_error, acknowledge_stale=request.POST.get("acknowledge_stale") == "1")
        except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
            review = {"error": str(exc), "paper_ids": [], "stale_paper_ids": [], "all_stale": False}
        context["retry_form"] = review
    return context


def crosscheck_service_job(request, pk):
    job = get_object_or_404(CrossCheckServiceJob, pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "start":
                job = start_service_job(pk, request.POST.get("evidence_token", ""))
            elif action == "refresh":
                job = refresh_service_job(pk)
            elif action == "apply":
                job = apply_service_results(pk, request.POST.get("evidence_token", ""))
            elif action == "retry":
                job = retry_service_downloads(pk, request.POST.get("evidence_token", ""),
                    request.POST.get("wait_minutes"), acknowledge_stale=request.POST.get("acknowledge_stale") == "1")
            elif action == "cancel":
                job = cancel_service_preview(pk, request.POST.get("evidence_token", ""))
            elif action == "stop":
                stop_service_job(pk)
                job.refresh_from_db()
            elif action == "repreview":
                job = repreview_service_results(pk)
            else:
                raise ValueError("Unknown CrossCheck action.")
            if request.headers.get("Accept") == "application/json":
                context = _job_page_context(request, job)
                message = context["active_attempt"].message if context["active_attempt"] else job.message
                return JsonResponse({"status": job.status, "progress": job.progress, "message": message,
                    "poll_enabled": context["poll_enabled"],
                    "panel_html": render_to_string("submissions/partials/crosscheck_batch_panel.html", context, request=request)})
        except Exception as exc:
            actions = {"start": "submit", "refresh": "progress_check", "apply": "result_apply",
                       "retry": "retry_download", "cancel": "preview_cancelled", "stop": "stop", "repreview": "result_preview"}
            audit_failure("crosscheck_service_" + actions.get(action, "unknown"), exc,
                          extra={"job_id": str(pk), "token": job.token})
            if request.headers.get("Accept") == "application/json":
                return JsonResponse({"error": str(exc)}, status=400)
            if action == "retry":
                job.refresh_from_db()
                return render(request, "submissions/crosscheck_service_job.html",
                              _job_page_context(request, job, retry_error=str(exc), show_retry=True))
            messages.error(request, str(exc))
        return redirect("submissions:crosscheck_service_job", pk=pk)
    return render(request, "submissions/crosscheck_service_job.html", _job_page_context(request, job))


def crosscheck_service_archive(request, pk):
    job = get_object_or_404(CrossCheckServiceJob, pk=pk)
    try:
        path = _result_archive(job)
    except ValueError as exc:
        raise Http404(str(exc)) from None
    return FileResponse(Path(path).open("rb"), as_attachment=True, filename=f"CrossCheck_{job.token}.zip")


def crosscheck_service_report(request, pk, item_id):
    job = get_object_or_404(CrossCheckServiceJob, pk=pk)
    item = get_object_or_404(job.items, pk=item_id)
    try:
        report = result_pdf_bytes(job, current_result(job, item))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise Http404("Report evidence is unavailable.") from exc
    return FileResponse(io.BytesIO(report), content_type="application/pdf", filename=f"{item.paper_id}_report.pdf")


def crosscheck_service_attempt_archive(request, pk, attempt_id):
    job = get_object_or_404(CrossCheckServiceJob, pk=pk)
    execution = get_object_or_404(job.attempts, pk=attempt_id)
    try:
        path = attempt_archive(job, execution)
    except (ValueError, OSError) as exc:
        raise Http404("Execution evidence is unavailable.") from exc
    return FileResponse(path.open("rb"), as_attachment=True, filename=f"CrossCheck_{job.token}_run_{execution.number}.zip")


def crosscheck_service_result_report(request, pk, result_id):
    job = get_object_or_404(CrossCheckServiceJob, pk=pk)
    result = get_object_or_404(CrossCheckServiceResult.objects.select_related("attempt", "item"),
                               pk=result_id, attempt__job=job, item__job=job)
    try:
        report = result_pdf_bytes(job, result)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise Http404("Execution report is unavailable.") from exc
    return FileResponse(io.BytesIO(report), content_type="application/pdf", filename=f"{result.item.paper_id}_report.pdf")


def crosscheck_service_input_pdf(request, pk, item_id):
    from submissions.services.crosscheck_service import job_root
    from submissions.services.crosscheck import _sha256_path

    job = get_object_or_404(CrossCheckServiceJob, pk=pk)
    item = get_object_or_404(job.items, pk=item_id)
    path = job_root(job) / "inputs" / f"{item.paper_id}.pdf"
    if not path.is_file() or _sha256_path(path) != item.pdf_sha256:
        raise Http404("Prepared PDF evidence is missing or changed.")
    return FileResponse(path.open("rb"), content_type="application/pdf", filename=f"{item.paper_id}.pdf")
