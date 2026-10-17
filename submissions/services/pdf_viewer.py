"""Presentation URLs only: the existing PDF endpoint still owns file selection."""

from urllib.parse import unquote, urlencode, urlsplit

from django.conf import settings
from django.urls import Resolver404, resolve, reverse


PDF_ROUTES = {
    "publication_pdf": "Publication PDF",
    "publication_debug_pdf": "Publication debug PDF",
    "final_submission_display_pdf": "Submission PDF",
    "plagiarism_report": "Plagiarism report",
    "formatting_revision_pdf": "Formatting revision PDF",
    "editor_upload_preview_pdf": "Uploaded PDF preview",
    "formatting_comparison_pdf": "Compared PDF",
}


def pdf_viewer_source(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Choose a PDF from this system.")
    parsed = urlsplit(value)
    path = unquote(parsed.path)
    if (
        parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
        or not path.startswith("/") or path.startswith("//")
        or any(ord(char) < 32 or ord(char) == 127 for char in value + path)
        or "\\" in path or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError("Only local system PDF links are supported.")
    media_prefix = urlsplit(settings.MEDIA_URL).path
    if media_prefix.startswith("/") and path.startswith(media_prefix.rstrip("/") + "/") and path.lower().endswith(".pdf"):
        return {"url": value, "label": "Uploaded PDF", "detail": path.rsplit("/", 1)[-1]}
    try:
        match = resolve(path)
    except Resolver404 as exc:
        raise ValueError("This link is not a supported PDF endpoint.") from exc
    if match.namespace != "submissions" or match.url_name not in PDF_ROUTES:
        raise ValueError("This link is not a supported PDF endpoint.")
    label = PDF_ROUTES[match.url_name]
    detail = f"Submission record {match.kwargs['pk']}" if "pk" in match.kwargs else "Pending Editor Upload"
    if match.url_name == "formatting_revision_pdf":
        detail += f" · Revision record {match.kwargs['revision_id']}"
    elif match.url_name == "formatting_comparison_pdf":
        side = match.kwargs["side"]
        if side not in {"before", "after"}:
            raise ValueError("Unknown comparison side.")
        detail += " · " + ("Before" if side == "before" else "After")
    return {"url": value, "label": label, "detail": detail}


def pdf_viewer_url(value):
    if not value:
        return ""
    # Validation belongs to the reader request, not worklist hydration.
    return reverse("submissions:pdf_viewer") + "?" + urlencode({"file": value})
