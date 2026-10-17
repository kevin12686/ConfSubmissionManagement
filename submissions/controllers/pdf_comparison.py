from io import BytesIO
import logging
from urllib.parse import urlencode

from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods

from submissions.models import FinalSubmission
from submissions.forms import PDFComparisonForm
from submissions.services.pdf_comparison import (
    PDFComparisonError, StaleUploadComparison, comparison_page_png, comparison_pdf_bytes,
    comparison_submission, create_pdf_comparison, load_pdf_comparison,
)
from submissions.services.file_manager import publication_pdf_info
from submissions.services.automatic_pdf_comparison import ComparisonInProgress, get_upload_comparison


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
        )
        if payload:
            result = {
                "state": "complete", "warning_count": payload["result"]["attention_count"],
                "info_count": payload["result"]["sequence_count"],
                "limited": payload["result"]["truncated"] or payload["result"]["pairing_limited"],
                "text_available": payload["result"]["text_available"],
                "url": reverse("submissions:formatting_pdf_comparison", args=[pk]) + "?" + urlencode({
                    "comparison": payload["token"], "baseline": "original", "next": _return_url(request, pk),
                }),
            }
        else:
            result = {"state": "pending", "message": "This saved PDF comparison has not started.", "retryable": True}
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
    form = PDFComparisonForm(submission, request.POST if request.method == "POST" else None, initial={"baseline": request.GET.get("baseline", "original")})
    try:
        comparison_submission(pk)
        if request.method == "POST" and form.is_valid():
            payload = create_pdf_comparison(pk, form.cleaned_data["baseline"], request=request)
            return redirect(reverse("submissions:formatting_pdf_comparison", args=[pk]) + "?" + urlencode({"comparison": payload["token"], "baseline": payload["baseline"]["key"], "next": back_url}))
        if request.method == "GET" and request.GET.get("comparison"):
            payload = load_pdf_comparison(pk, request.GET["comparison"])
            form = PDFComparisonForm(submission, initial={"baseline": payload["baseline"]["key"]})
    except (PDFComparisonError, OSError, ValueError) as exc:
        error, status = str(exc), 409
    comparison_data = None
    if payload:
        comparison_data = {
            **payload["result"], "created_at": payload["created_at"],
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
        }
    response = render(request, "submissions/pdf_comparison.html", {
        "submission": submission, "form": form, "back_url": back_url,
        "comparison_data": comparison_data, "comparison_error": error,
        "current_pdf": publication_pdf_info(submission),
    }, status=status)
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
def formatting_comparison_pdf(request, pk, token, side):
    try:
        response = FileResponse(BytesIO(comparison_pdf_bytes(pk, token, side)), content_type="application/pdf", filename=f"compared-{side}.pdf")
    except (PDFComparisonError, OSError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
