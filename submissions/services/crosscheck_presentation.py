"""Read-only CrossCheck summaries; never authorize an upload or result apply."""

WAITING_ITEM_STATES = ("prepared", "wait_timeout", "waiting", "unavailable", "download_failed")


def item_category(item, has_report):
    if item.status == "applied":
        return "applied"
    if item.status == "downloaded" and has_report:
        return "ready"
    if item.status in WAITING_ITEM_STATES:
        return "waiting"
    return "attention"


def batch_counts(items, has_report):
    counts = dict.fromkeys(("total", "applied", "ready", "waiting", "attention"), 0)
    for item in items:
        counts["total"] += 1
        counts[item_category(item, has_report(item))] += 1
    return counts


def batch_summary(job, counts, *, retained, active=None, latest=None):
    """Present retained report records separately from readiness and application.

    This is saved evidence, not a fresh archive/hash or remote-service check.
    An in-flight retry can coexist with already applied results.
    """
    total = counts["total"]
    missing = total - retained
    if active:
        if active.status in {"running", "pending_download"}:
            key, label, level, icon = "running", "In progress", "info", "spinner"
            description = "Downloading remaining reports." if active.kind == "retry_download" else "Checking and downloading reports."
        else:
            key, label, level, icon = "unconfirmed", "Connection needs confirmation", "warning", "attention"
            description = "Check progress before retrying an unconfirmed request."
    elif job.status == "preview":
        key, label, level, icon = "preview", "Upload preview", "neutral", "neutral"
        description = "Not submitted."
    elif job.status == "cancelled":
        key, label, level, icon = "cancelled", "Cancelled", "neutral", "neutral"
        description = "No execution is running."
    elif not total:
        key, label, level, icon = "empty", "No papers", "neutral", "neutral"
        description = "This batch contains no paper results."
    elif counts["applied"] == total and retained == total:
        key, label, level, icon = "completed", "All results applied", "success", "check"
        description = "All papers in this batch have applied results."
    elif counts["ready"] and not (counts["waiting"] or counts["attention"] or missing):
        key, label, level, icon = "awaiting_apply", "Awaiting application", "info", "review"
        description = "Reports retrieved; confirmation is still required."
    elif retained or counts["applied"] or counts["ready"]:
        key, label, level, icon = "partial", "Partially complete", "warning", "attention"
        description = "Some papers still need reports or attention."
    elif job.status == "failed" or (latest and latest.status in {"failed", "rejected"}):
        key, label, level, icon = "failed", "Execution failed", "danger", "attention"
        description = "Check the execution details before retrying."
    elif counts["attention"]:
        key, label, level, icon = "attention", "Needs attention", "warning", "attention"
        description = "Resolve result or upload issues before applying."
    else:
        key, label, level, icon = "missing", "Reports still missing", "warning", "attention"
        description = "No execution is running."
    return {"key": key, "label": label, "level": level, "icon": icon,
            "description": description, "total": total, "retained": retained,
            "missing": missing, "applied": counts["applied"], "ready": counts["ready"],
            "attention": counts["attention"]}
