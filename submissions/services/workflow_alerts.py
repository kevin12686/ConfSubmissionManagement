from django.core.cache import cache
from django.db import OperationalError, ProgrammingError

from submissions.services.editor_uploads import editor_conflict_count
from submissions.services.file_manager import active_pdfs_needing_processing


WORKFLOW_ALERT_CACHE_KEY = "submissions:workflow-alerts:v1"
WORKFLOW_ALERT_CACHE_SECONDS = 5


def workflow_alert_counts():
    cached = cache.get(WORKFLOW_ALERT_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        counts = {
            "active_pdfs_need_processing": len(active_pdfs_needing_processing()),
            "start2_editor_conflicts": editor_conflict_count(),
        }
    except (OperationalError, ProgrammingError):
        counts = {
            "active_pdfs_need_processing": 0,
            "start2_editor_conflicts": 0,
        }
    cache.set(
        WORKFLOW_ALERT_CACHE_KEY,
        counts,
        timeout=WORKFLOW_ALERT_CACHE_SECONDS,
    )
    return counts
