from django.http import FileResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from submissions.application.pagination import paginate_worklist
from submissions.models import FinalSubmission, FormattingRevision
from submissions.services.audit import audit_failure, audit_success
from submissions.services.file_inspection import FileChangedDuringInspection
from submissions.services.formatting import source_file_type_label
from submissions.services.formatting_history import (
    current_file_fingerprints,
    open_revision_file,
    revision_audit_data,
    revision_matches_current,
)


@require_GET
def formatting_history(request, pk):
    submission = get_object_or_404(FinalSubmission, pk=pk)
    page = paginate_worklist(
        request, submission.formatting_revisions.all(),
        hx_target=f"#formatting-history-content-{pk}",
    )
    try:
        fingerprints = current_file_fingerprints(submission) if page.total_count else None
    except (OSError, ValueError, FileChangedDuringInspection):
        fingerprints = None
    rows = []
    for revision in page.items:
        files = []
        for kind in ("pdf", "source"):
            metadata = getattr(revision, f"{kind}_metadata")
            field = getattr(revision, f"{kind}_file")
            files.append({
                "kind": kind,
                "label": "PDF" if kind == "pdf" else "Source",
                "metadata": metadata,
                "type_label": "PDF" if kind == "pdf" else source_file_type_label(metadata.get("filename", "")),
                "available": bool(field and field.storage.exists(field.name)),
                "url": reverse(f"submissions:formatting_revision_{kind}", args=[pk, revision.pk]),
            })
        rows.append({
            "revision": revision,
            "current": bool(fingerprints and revision_matches_current(revision, submission, fingerprints=fingerprints)),
            "files": files,
        })
    context = {"submission": submission, "rows": rows, "pagination": page, "pagination_local": True}
    template = (
        "submissions/partials/formatting_history_list.html"
        if request.headers.get("HX-Request") == "true"
        else "submissions/formatting_history.html"
    )
    return render(request, template, context)


def _revision_download(request, pk, revision_id, kind):
    revision = get_object_or_404(FormattingRevision.objects.select_related("submission"), pk=revision_id, submission_id=pk)
    handle = None
    try:
        handle = open_revision_file(revision, kind)
        audit_success(
            "formatting_revision_download", "Formatting revision file opened.",
            request=request, submission=revision.submission,
            extra={**revision_audit_data(revision), "file_kind": kind},
        )
    except (OSError, ValueError) as exc:
        if handle:
            handle.close()
        audit_failure(
            "formatting_revision_download", exc, "Formatting revision file unavailable.",
            request=request, submission=revision.submission,
            extra={"revision_id": revision.pk, "file_kind": kind},
        )
        error = (
            "The stored revision file is unavailable. Check the history files or backup; "
            "no other version was substituted."
            if isinstance(exc, OSError) else str(exc)
        )
        return render(request, "submissions/formatting_history_error.html", {"submission": revision.submission, "error": error}, status=404)
    except BaseException:
        if handle:
            handle.close()
        raise
    metadata = getattr(revision, f"{kind}_metadata")
    response = FileResponse(
        handle, as_attachment=kind == "source",
        filename=metadata.get("filename") or f"revision-{revision.number}-{kind}",
        content_type="application/pdf" if kind == "pdf" else "application/octet-stream",
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def formatting_revision_pdf(request, pk, revision_id):
    return _revision_download(request, pk, revision_id, "pdf")


@require_GET
def formatting_revision_source(request, pk, revision_id):
    return _revision_download(request, pk, revision_id, "source")
