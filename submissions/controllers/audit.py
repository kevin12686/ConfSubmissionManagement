from django.http import FileResponse, Http404
from django.shortcuts import render

from submissions.services.audit import audit_log_info, audit_log_path, read_audit_log
from submissions.services.audit_actions import (
    AUDIT_ACTIONS,
    AUDIT_CATEGORY_LABELS,
    audit_action_groups,
    audit_category_options,
    canonical_audit_action,
)


AUDIT_STATUS_OPTIONS = [
    {"value": "requested", "label": "Requested"},
    {"value": "previewed", "label": "Previewed"},
    {"value": "success", "label": "Success"},
    {"value": "blocked", "label": "Blocked"},
    {"value": "failed", "label": "Failed"},
]
AUDIT_ROW_LIMIT_OPTIONS = [50, 100, 300, 500, 1000, 2000]


def audit_log(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    if category not in AUDIT_CATEGORY_LABELS:
        category = ""
    action = canonical_audit_action(request.GET.get("action", ""))
    if action not in AUDIT_ACTIONS:
        action = ""
    elif category and AUDIT_ACTIONS[action].category != category:
        action = ""
    status = request.GET.get("status", "").strip()
    valid_statuses = {option["value"] for option in AUDIT_STATUS_OPTIONS}
    if status not in valid_statuses:
        status = ""
    try:
        limit = int(request.GET.get("limit", "300"))
    except ValueError:
        limit = 300
    if limit not in AUDIT_ROW_LIMIT_OPTIONS:
        limit = 300
    events = read_audit_log(
        query=query,
        limit=limit,
        category=category,
        action=action,
        status=status,
    )
    return render(
        request,
        "submissions/audit_log.html",
        {
            "events": events,
            "audit_info": audit_log_info(),
            "q": query,
            "current_category": category,
            "current_action": action,
            "current_status": status,
            "limit": limit,
            "category_options": audit_category_options(),
            "action_groups": audit_action_groups(category),
            "status_options": AUDIT_STATUS_OPTIONS,
            "row_limit_options": AUDIT_ROW_LIMIT_OPTIONS,
            "active_filter_count": sum(
                bool(value)
                for value in (query, category, action, status)
            ),
            "shown_count": len(events),
        },
    )


def download_audit_log(request):
    path = audit_log_path()
    if not path.exists():
        raise Http404("Audit log not found.")
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)
