from urllib.parse import urlencode

from django.urls import reverse

from submissions.models import InitialPaper
from submissions.services.publication_read import PublicationReadContext


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


def search_process_papers(query):
    query = (query or "").strip()
    if not query:
        return []

    lowered = query.casefold()

    def priority(submission):
        paper_id = (submission.paper_id_filled or "").casefold()
        final_id = (submission.final_submission_id or "").casefold()
        title = (submission.final_submission_title or "").casefold()
        if lowered in {paper_id, final_id}:
            rank = 0
        elif paper_id.startswith(lowered) or final_id.startswith(lowered):
            rank = 1
        elif lowered in paper_id or lowered in final_id:
            rank = 2
        elif lowered in title:
            rank = 3
        else:
            rank = 4
        return rank, paper_id, final_id

    candidates = []
    for submission in PublicationReadContext.load().master_submissions:
        searchable = " ".join(
            (
                submission.paper_id_filled or "",
                submission.final_submission_id or "",
                submission.final_submission_title or "",
            )
        ).casefold()
        if lowered in searchable:
            candidates.append(submission)

    results = []
    for submission in sorted(candidates, key=priority)[:PAPER_PICKER_RESULT_LIMIT]:
        results.append(
            {
                "pk": submission.pk,
                "paper_id": submission.paper_id_filled or "",
                "final_id": submission.final_submission_id or "",
                "url": (
                    reverse("submissions:process")
                    + "?"
                    + urlencode({"submission": submission.pk})
                ),
            }
        )
    return results
