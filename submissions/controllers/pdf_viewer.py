from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_GET

from submissions.services.pdf_viewer import pdf_viewer_source


@require_GET
def pdf_viewer(request):
    try:
        source = pdf_viewer_source(request.GET.get("file", ""))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    response = render(request, "submissions/pdf_viewer.html", {"pdf_source": source})
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    return response
