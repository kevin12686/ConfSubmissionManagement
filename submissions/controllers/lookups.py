from django.http import JsonResponse
from django.views.decorators.http import require_GET

from submissions.services.paper_picker import (
    search_master_papers,
    search_process_papers,
)


@require_GET
def paper_picker_search(request):
    context = request.GET.get("context", "master").strip()
    query = request.GET.get("q", "")
    if context == "process":
        results = search_process_papers(query)
    else:
        results = search_master_papers(
            query,
            selected=request.GET.get("selected", ""),
            selected_field=request.GET.get("selected_field", ""),
        )
    return JsonResponse({"results": results})
