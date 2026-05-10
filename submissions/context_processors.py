from django.db import OperationalError, ProgrammingError
from django.conf import settings as django_settings
from django.utils import timezone

from submissions.models import AppSetting
from submissions.services.file_manager import active_pdfs_needing_processing
from submissions.services.editor_uploads import editor_conflict_count


def global_workflow_alerts(request):
    try:
        process_pdf_count = len(active_pdfs_needing_processing())
    except (OperationalError, ProgrammingError):
        process_pdf_count = 0
    try:
        start2_editor_conflicts = editor_conflict_count()
    except (OperationalError, ProgrammingError):
        start2_editor_conflicts = 0

    try:
        conference_name = AppSetting.load().conference_name.strip() or "Local Conference"
    except (OperationalError, ProgrammingError):
        conference_name = "Local Conference"

    return {
        "global_workflow_alerts": {
            "active_pdfs_need_processing": process_pdf_count,
            "start2_editor_conflicts": start2_editor_conflicts,
        },
        "current_conference_name": conference_name,
        "app_name": django_settings.APP_NAME,
        "app_version": django_settings.APP_VERSION,
        "state_archive_version": django_settings.STATE_ARCHIVE_VERSION,
        "copyright_year": timezone.localdate().year,
    }
