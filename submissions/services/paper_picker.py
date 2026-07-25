from submissions.models import InitialPaper


PAPER_PICKER_RESULT_LIMIT = 20


def _paper_payload(paper):
    return {
        "pk": paper.pk,
        "paper_id": paper.paper_id,
        "title": paper.title or "",
        "authors": paper.authors or "",
    }


def _append_unique(results, seen, papers):
    for paper in papers:
        if paper.pk in seen:
            continue
        seen.add(paper.pk)
        results.append(_paper_payload(paper))
        if len(results) >= PAPER_PICKER_RESULT_LIMIT:
            return True
    return False


def search_master_papers(query="", *, selected="", selected_field=""):
    query = (query or "").strip()
    selected = (selected or "").strip()
    if selected:
        if selected_field == "paper_id":
            paper = InitialPaper.objects.filter(paper_id=selected).first()
        else:
            try:
                selected_pk = int(selected)
            except (TypeError, ValueError):
                paper = None
            else:
                paper = InitialPaper.objects.filter(pk=selected_pk).first()
        return [_paper_payload(paper)] if paper else []
    if not query:
        return []

    results = []
    seen = set()
    ordered_queries = (
        InitialPaper.objects.filter(paper_id__iexact=query),
        InitialPaper.objects.filter(paper_id__istartswith=query),
        InitialPaper.objects.filter(paper_id__icontains=query),
        InitialPaper.objects.filter(title__icontains=query),
        InitialPaper.objects.filter(authors__icontains=query),
    )
    for queryset in ordered_queries:
        if _append_unique(
            results,
            seen,
            queryset.order_by("paper_id")[:PAPER_PICKER_RESULT_LIMIT],
        ):
            break
    return results
