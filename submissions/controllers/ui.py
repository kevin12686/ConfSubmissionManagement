from django.http import Http404
from django.shortcuts import render

from submissions.services.checks import publication_duplicate_detail
from submissions.services.workflow_alerts import workflow_alert_counts


def workflow_alerts(request):
    return render(
        request,
        "submissions/partials/workflow_alerts.html",
        {"global_workflow_alerts": workflow_alert_counts()},
    )


def publication_duplicate_details(request):
    kind = request.GET.get("kind", "")
    key = request.GET.get("key", "")
    if kind not in {"title", "pdf", "source"} or not key or len(key) > 600:
        raise Http404("Duplicate publication record not found.")
    try:
        submission_id = int(request.GET.get("submission_id", ""))
    except (TypeError, ValueError) as exc:
        raise Http404("Duplicate publication record not found.") from exc
    detail = publication_duplicate_detail(
        kind,
        key,
        submission_id,
    )
    if detail is None:
        raise Http404("Duplicate publication record not found.")
    template_name = (
        "submissions/partials/publication_duplicate_details.html"
        if request.headers.get("HX-Request") == "true"
        else "submissions/publication_duplicate_details.html"
    )
    return render(request, template_name, {"detail": detail})
