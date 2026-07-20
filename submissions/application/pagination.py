from dataclasses import dataclass

from django.core.paginator import Paginator


DEFAULT_PAGE_SIZE = 25
PAGE_SIZE_OPTIONS = (25, 50, 100, 200)
ALL_PAGE_SIZE = "all"


@dataclass(frozen=True)
class WorklistPage:
    items: object
    total_count: int
    start_index: int
    end_index: int
    page_number: int
    page_size: object
    is_all: bool
    has_previous: bool
    has_next: bool
    previous_url: str
    next_url: str
    page_links: tuple
    page_size_links: tuple
    hx_target: str
    indicator_id: str
    scroll_anchor: str


def paginate_worklist(
    request,
    items,
    *,
    default_page_size=DEFAULT_PAGE_SIZE,
    hx_target="",
    indicator_id="",
    scroll_anchor="",
    force_all=False,
):
    if not scroll_anchor and hx_target.startswith("#"):
        candidate = hx_target[1:]
        if candidate and all(character.isalnum() or character in "-_" for character in candidate):
            scroll_anchor = candidate

    requested_size = request.GET.get("page_size", str(default_page_size)).lower()
    valid_sizes = {str(size) for size in PAGE_SIZE_OPTIONS} | {ALL_PAGE_SIZE}
    if requested_size not in valid_sizes:
        requested_size = str(default_page_size)
    if force_all:
        requested_size = ALL_PAGE_SIZE

    total_count = _item_count(items)
    size_links = tuple(
        {
            "value": str(size),
            "label": "All" if size == ALL_PAGE_SIZE else str(size),
            "url": _query_url(request, page=1, page_size=size),
            "active": requested_size == str(size),
        }
        for size in (*PAGE_SIZE_OPTIONS, ALL_PAGE_SIZE)
    )

    if requested_size == ALL_PAGE_SIZE:
        return WorklistPage(
            items=items,
            total_count=total_count,
            start_index=1 if total_count else 0,
            end_index=total_count,
            page_number=1,
            page_size=ALL_PAGE_SIZE,
            is_all=True,
            has_previous=False,
            has_next=False,
            previous_url="",
            next_url="",
            page_links=(),
            page_size_links=size_links,
            hx_target=hx_target,
            indicator_id=indicator_id,
            scroll_anchor=scroll_anchor,
        )

    page_size = int(requested_size)
    paginator = Paginator(items, page_size)
    page = paginator.get_page(request.GET.get("page", 1))
    page_links = tuple(
        {
            "number": number,
            "url": _query_url(request, page=number, page_size=page_size),
            "active": number == page.number,
        }
        for number in paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)
        if number != Paginator.ELLIPSIS
    )
    return WorklistPage(
        items=page.object_list,
        total_count=total_count,
        start_index=page.start_index() if total_count else 0,
        end_index=page.end_index() if total_count else 0,
        page_number=page.number,
        page_size=page_size,
        is_all=False,
        has_previous=page.has_previous(),
        has_next=page.has_next(),
        previous_url=(
            _query_url(request, page=page.previous_page_number(), page_size=page_size)
            if page.has_previous()
            else ""
        ),
        next_url=(
            _query_url(request, page=page.next_page_number(), page_size=page_size)
            if page.has_next()
            else ""
        ),
        page_links=page_links,
        page_size_links=size_links,
        hx_target=hx_target,
        indicator_id=indicator_id,
        scroll_anchor=scroll_anchor,
    )


def _item_count(items):
    count_method = getattr(items, "count", None)
    if callable(count_method) and not isinstance(items, (list, tuple)):
        return count_method()
    return len(items)


def _query_url(request, **changes):
    query = request.GET.copy()
    for key, value in changes.items():
        query[key] = str(value)
    encoded = query.urlencode()
    return f"{request.path}?{encoded}" if encoded else request.path
