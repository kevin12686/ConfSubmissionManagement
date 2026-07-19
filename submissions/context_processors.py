from django.db import OperationalError, ProgrammingError
from django.conf import settings as django_settings
from django.utils import timezone

from submissions.models import AppSetting


def global_workflow_alerts(request):
    try:
        conference_name = AppSetting.load().conference_name.strip() or "Local Conference"
    except (OperationalError, ProgrammingError):
        conference_name = "Local Conference"

    return {
        "current_conference_name": conference_name,
        "app_name": django_settings.APP_NAME,
        "app_version": django_settings.APP_VERSION,
        "state_archive_version": django_settings.STATE_ARCHIVE_VERSION,
        "copyright_year": timezone.localdate().year,
    }
