from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import OperationalError, ProgrammingError
from django.middleware.gzip import GZipMiddleware
from django.utils import timezone


GZIP_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/css",
        "text/csv",
        "text/javascript",
        "application/json",
        "application/ld+json",
        "application/javascript",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
    }
)


class SelectiveGZipMiddleware(GZipMiddleware):
    def process_response(self, request, response):
        content_type = response.get("Content-Type", "").partition(";")[0]
        if content_type.strip().lower() not in GZIP_CONTENT_TYPES:
            return response
        return super().process_response(request, response)


class AppTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            from submissions.models import AppSetting

            zone_name = AppSetting.read().time_zone
            timezone.activate(ZoneInfo(zone_name))
        except (OperationalError, ProgrammingError, ZoneInfoNotFoundError):
            timezone.activate(ZoneInfo("America/Chicago"))
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
