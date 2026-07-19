from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import OperationalError, ProgrammingError
from django.utils import timezone


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
