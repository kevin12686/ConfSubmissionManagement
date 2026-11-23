from io import BytesIO
import logging
from threading import BoundedSemaphore
from urllib.parse import unquote, urlencode

from django.core import signing
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods

from submissions.models import FinalSubmission
from submissions.forms import PDFComparisonForm
from submissions.services.pdf_comparison import (
    PDFComparisonError, StaleUploadComparison, comparison_page_png, comparison_pdf_bytes, comparison_detail_png,
    comparison_submission, create_pdf_comparison, load_pdf_comparison,
)
from submissions.services.file_manager import publication_pdf_info
from submissions.services.automatic_pdf_comparison import ComparisonInProgress, get_upload_comparison
from submissions.services.pdf_comparison_errors import LocalComparisonConfirmationRequired
from submissions.services.comparison_execution_choice import execution_display
from submissions.services.comparison_protocol import MAX_PDF_BYTES
from submissions.services.ephemeral_pdf_comparison import BASELINE_SALT, compare_once, read_baseline


_one_shot_slot = BoundedSemaphore(1)


def _memory_response(response):
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _comparison_error(message, status=409):
    return _memory_response(JsonResponse({"error": message}, status=status))


@require_GET
def formatting_compare_once(request, pk):
    try:
        submission = comparison_submission(pk)
    except PDFComparisonError as exc:
        return _memory_response(render(request, "submissions/pdf_comparison.html", {
            "ephemeral": True, "comparison_error": str(exc), "back_url": _return_url(request, pk),
        }, status=409))
    return _memory_response(render(request, "submissions/pdf_comparison.html", {
        "ephemeral": True, "submission": submission, "back_url": _return_url(request, pk),
        "max_pdf_bytes": MAX_PDF_BYTES,
    }))


@csrf_exempt
@require_http_methods(["GET", "POST"])
def formatting_compare_once_input(request, pk):
    if request.method == "GET":
        try:
            data, binding = read_baseline(pk, request.GET.get("baseline", "original"))
        except (PDFComparisonError, OSError):
            return _comparison_error("The selected baseline is missing, changed, or not available for this submission. No other PDF was selected.")
        response = HttpResponse(data, content_type="application/octet-stream")
        response["X-CFM-Baseline"] = signing.dumps(binding, salt=BASELINE_SALT, compress=True)
        return _memory_response(response)

    # Reject multipart before CSRF can trigger Django's disk upload handler.
    # The only route into computation enforces normal CSRF on the raw PDF POST.
    if request.content_type != "application/pdf":
        return _comparison_error("Send one PDF as the request body, not a form upload.", 415)
    return _compare_once_post(request, pk)


@csrf_protect
def _compare_once_post(request, pk):
    length = request.META.get("CONTENT_LENGTH", "")
    if not length.isascii() or not length.isdecimal() or len(length) > 10:
        return _comparison_error("A PDF upload length is required.", 411)
    length = int(length)
    if not 0 < length <= MAX_PDF_BYTES:
        return _comparison_error("Choose a non-empty PDF of at most 64 MiB.", 413)
    if not _one_shot_slot.acquire(blocking=False):
        return _comparison_error("A local one-time comparison is already running. Try again when it finishes.", 429)
    try:
        draft = request.read(length)
        if len(draft) != length:
            return _comparison_error("The PDF upload was incomplete. Select the draft again.", 400)
        name = unquote(request.headers.get("X-CFM-Draft-Name", "")[:4096]).replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(c for c in name if c.isprintable())[:255] or "Draft PDF"
        token = request.headers.get("X-CFM-Baseline", "")
        data = compare_once(pk, token, draft, filename)
        html = render_to_string("submissions/partials/pdf_comparison_workspace.html", {
            "comparison_data": data, "ephemeral": True,
        })
        return _memory_response(JsonResponse({"data": data, "workspace": html}))
    except PDFComparisonError as exc:
        return _comparison_error(str(exc))
    except OSError:
        return _comparison_error("A comparison input could not be read. No files were changed.")
    finally:
        _one_shot_slot.release()


def _return_url(request, pk):
    value = request.POST.get("next", "") if request.method == "POST" else request.GET.get("next", "")
    if value and url_has_allowed_host_and_scheme(value, {request.get_host()}, require_https=request.is_secure()):
        return value
    return reverse("submissions:formatting") + "?" + urlencode({"mode": "focus", "submission": pk})


@require_http_methods(["GET", "POST"])
def formatting_upload_comparison(request, pk):
    params = request.POST if request.method == "POST" else request.GET
    status = 200
    try:
        payload = get_upload_comparison(
            pk, params.get("token", ""), build=request.method == "POST",
            retry=params.get("retry") == "1", request=request,
            local_confirmation=request.POST.get("local_confirmation", "") if request.method == "POST" else "",
        )
        if payload:
            result = {
                "state": "complete", "warning_count": payload["result"]["total_attention_count"],
                "info_count": payload["result"]["info_count"],
                "uncertain_count": payload["result"]["uncertain_count"],
                "limited": payload["result"].get("coverage_status") == "limited" or payload["result"]["truncated"] or payload["result"]["pairing_limited"],
                "text_available": payload["result"]["text_available"],
                "execution": execution_display(payload.get("execution")),
                "url": reverse("submissions:formatting_pdf_comparison", args=[pk]) + "?" + urlencode({
                    "comparison": payload["token"], "baseline": "original", "next": _return_url(request, pk),
                }),
            }
        else:
            result = {"state": "pending", "message": "This saved PDF comparison has not started.", "retryable": True}
    except LocalComparisonConfirmationRequired as exc:
        result, status = {"state": "awaiting_choice", "message": str(exc), "retryable": True,
                          "local_confirmation": exc.confirmation_token, "existing_local": exc.existing_local}, 200
    except StaleUploadComparison as exc:
        result, status = {"state": "unavailable", "message": str(exc), "retryable": False}, 409
    except ComparisonInProgress as exc:
        result, status = {"state": "busy", "message": str(exc), "retryable": True}, 409
    except (PDFComparisonError, OSError, ValueError, RuntimeError) as exc:
        result, status = {"state": "failed", "message": str(exc), "retryable": True}, 422
    except Exception:
        logging.getLogger(__name__).exception("Automatic PDF comparison request failed")
        result, status = {"state": "failed", "message": "Comparison is temporarily unavailable. Formatting files remain saved. Retry or use Compare PDF.", "retryable": True}, 503
    response = JsonResponse(result, status=status)
    response["Cache-Control"] = "no-store"
    return response


@require_http_methods(["GET", "POST"])
def formatting_pdf_comparison(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    back_url = _return_url(request, pk)
    payload, error = None, ""
    status = 200
    execution_choice = None
    form = PDFComparisonForm(submission, request.POST if request.method == "POST" else None, initial={"baseline": request.GET.get("baseline", "original")})
    try:
        comparison_submission(pk)
        if request.method == "POST" and form.is_valid():
            payload = create_pdf_comparison(pk, form.cleaned_data["baseline"], request=request, retry_failed=True,
                                            local_confirmation=request.POST.get("local_confirmation", ""))
            return redirect(reverse("submissions:formatting_pdf_comparison", args=[pk]) + "?" + urlencode({"comparison": payload["token"], "baseline": payload["baseline"]["key"], "next": back_url}))
        if request.method == "GET" and request.GET.get("comparison"):
            payload = load_pdf_comparison(pk, request.GET["comparison"])
            form = PDFComparisonForm(submission, initial={"baseline": payload["baseline"]["key"]})
    except LocalComparisonConfirmationRequired as exc:
        execution_choice = {"message": str(exc), "token": exc.confirmation_token, "existing_local": exc.existing_local}
    except (PDFComparisonError, OSError, ValueError) as exc:
        error, status = str(exc), 409
    comparison_data = None
    if payload:
        comparison_data = {
            **payload["result"], "created_at": payload["created_at"],
            "execution": execution_display(payload.get("execution")),
            "before": {**payload["baseline"], "sha256": payload["before_sha256"]},
            "after": {**payload["target"], "sha256": payload["after_sha256"]},
            "page_urls": {
                side: reverse("submissions:formatting_comparison_page", args=[pk, payload["token"], side, 0])
                for side in ("before", "after")
            },
            "pdf_urls": {
                side: reverse("submissions:formatting_comparison_pdf", args=[pk, payload["token"], side])
                for side in ("before", "after")
            },
            "detail_urls": {
                side: reverse("submissions:formatting_comparison_detail", args=[pk, payload["token"], side, 0])
                for side in ("before", "after")
            },
        }
    response = render(request, "submissions/pdf_comparison.html", {
        "submission": submission, "form": form, "back_url": back_url,
        "comparison_data": comparison_data, "comparison_error": error,
        "current_pdf": publication_pdf_info(submission),
        "execution_choice": execution_choice,
    }, status=status)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def formatting_comparison_services(request):
    from submissions.services.comparison_health import comparison_service_status
    response = JsonResponse(comparison_service_status())
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def formatting_comparison_page(request, pk, token, side, page_number):
    try:
        response = HttpResponse(comparison_page_png(pk, token, side, page_number), content_type="image/png")
    except (PDFComparisonError, OSError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def formatting_comparison_detail(request, pk, token, side, page_number):
    try:
        box = request.GET.get("box", "").split(",")
        response = HttpResponse(comparison_detail_png(
            pk, token, side, page_number, box=box,
            width=request.GET.get("width"), height=request.GET.get("height")), content_type="image/png")
    except (PDFComparisonError, OSError, ValueError) as exc:
        response = JsonResponse({"error": str(exc)}, status=409)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def formatting_comparison_pdf(request, pk, token, side):
    try:
        response = FileResponse(BytesIO(comparison_pdf_bytes(pk, token, side)), content_type="application/pdf", filename=f"compared-{side}.pdf")
    except (PDFComparisonError, OSError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
