from django.db import OperationalError, ProgrammingError
from django.conf import settings as django_settings
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.file_manager import corrected_pdf_needs_processing


def global_workflow_alerts(request):
    try:
        active_with_corrected_pdf = (
            FinalSubmission.objects.filter(
                active_version=True,
                excluded_from_publication=False,
            )
            .exclude(formatted_pdf_file="")
            .exclude(formatted_pdf_file__isnull=True)
        )
        corrected_pdf_count = sum(
            1
            for submission in active_with_corrected_pdf
            if corrected_pdf_needs_processing(submission)
        )
    except (OperationalError, ProgrammingError):
        corrected_pdf_count = 0

    try:
        conference_name = AppSetting.load().conference_name.strip() or "Local Conference"
    except (OperationalError, ProgrammingError):
        conference_name = "Local Conference"

    return {
        "global_workflow_alerts": {
            "corrected_pdf_needs_processing": corrected_pdf_count,
        },
        "current_conference_name": conference_name,
        "app_name": django_settings.APP_NAME,
        "app_version": django_settings.APP_VERSION,
        "state_archive_version": django_settings.STATE_ARCHIVE_VERSION,
        "copyright_year": timezone.localdate().year,
    }
