from django.http import FileResponse, Http404
from django.shortcuts import render

from submissions.services.audit import audit_log_info, audit_log_path, read_audit_log


def audit_log(request):
    query = request.GET.get("q", "").strip()
    try:
        limit = int(request.GET.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(50, min(limit, 2000))
    return render(
        request,
        "submissions/audit_log.html",
        {
            "events": read_audit_log(query=query, limit=limit),
            "audit_info": audit_log_info(),
            "q": query,
            "limit": limit,
        },
    )


def download_audit_log(request):
    path = audit_log_path()
    if not path.exists():
        raise Http404("Audit log not found.")
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)
