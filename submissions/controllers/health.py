from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def readiness(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    response = JsonResponse({"status": "ok"})
    response["Cache-Control"] = "no-store"
    return response
