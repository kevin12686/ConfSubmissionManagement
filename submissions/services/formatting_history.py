"""Immutable formatting evidence; never an input to publication selection."""

import hashlib
import logging
import shutil
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import SpooledTemporaryFile

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from submissions.models import FinalSubmission, FormattingRevision
from submissions.services.audit import audit_failure, audit_success
from submissions.services.file_inspection import FileInspectionContext
from submissions.services.file_manager import publication_pdf_info, publication_source_info


logger = logging.getLogger(__name__)
_created_paths = ContextVar("formatting_history_created_paths", default=None)


@contextmanager
def revision_files_transaction():
    """Compensate filesystem writes when the owning workflow transaction fails."""
    paths = _created_paths.get()
    token = None
    if paths is None:
        paths = []
        token = _created_paths.set(paths)
    start = len(paths)
    try:
        with transaction.atomic():
            yield
    except BaseException as exc:
        created = list(paths[start:])
        for path in reversed(created):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove rolled-back formatting output: %s", path)
        del paths[start:]
        if created:
            try:
                audit_failure(
                    "formatting_revision_rollback", exc,
                    "Formatting file transaction rolled back; no new revision was applied.",
                    file_changes={"rolled_back_paths": created},
                )
            except Exception:
                logger.exception("Could not audit formatting revision rollback")
        raise
    finally:
        if token is not None:
            _created_paths.reset(token)


def track_created_file(path):
    paths = _created_paths.get()
    if paths is None:
        raise RuntimeError("Formatting files require a revision file transaction.")
    paths.append(Path(path))


def _selected_files(submission):
    inspection = FileInspectionContext()
    return {
        "pdf": publication_pdf_info(submission, inspection, include_url=False),
        "source": publication_source_info(submission, inspection, include_url=False),
    }


def _metadata(submission, kind, info):
    corrected = bool(getattr(submission, f"formatted_{kind}_file"))
    field = getattr(submission, f"formatted_{kind}_file" if corrected else f"{kind}_file")
    name = field.name if field else ""
    original_name = (
        submission.original_file_name if kind == "pdf" else submission.source_original_file_name
    )
    uploaded_at = getattr(submission, f"formatted_{kind}_uploaded_at") if corrected else submission.upload_date
    return {
        "origin": "corrected" if corrected else ("original" if name else "missing"),
        "live_name": name,
        "filename": Path(name).name if corrected else (original_name or Path(name).name),
        "uploaded_at": uploaded_at.isoformat() if uploaded_at else None,
        "exists": bool(info["exists"]),
        "size": None,
        "sha256": "",
    }


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_snapshot(submission, kind, info, directory):
    metadata = _metadata(submission, kind, info)
    if not info["exists"]:
        if metadata["live_name"]:
            field = getattr(submission, f"formatted_{kind}_file" if metadata["origin"] == "corrected" else f"{kind}_file")
            try:
                Path(field.path).stat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Selected formatting file changed during capture. Reload and retry.")
        return "", metadata
    source = Path(info["path"])
    before = source.stat()
    target = directory / (kind + source.suffix.lower())
    with source.open("rb") as src, target.open("xb") as dest:
        shutil.copyfileobj(src, dest)
    digest = _sha256(target)
    after = source.stat()
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or digest != _sha256(source)
    ):
        raise ValueError("Formatting file changed while its revision was being captured. Retry the upload.")
    metadata.update(size=target.stat().st_size, sha256=digest)
    return target.relative_to(Path(settings.MEDIA_ROOT)).as_posix(), metadata


def capture_revision(submission, *, event_type, changed_files=(), uploaded_names=None):
    if _created_paths.get() is None:
        raise RuntimeError("Revision capture requires a revision file transaction.")
    key = uuid.uuid4().hex
    directory = Path(settings.MEDIA_ROOT) / "formatting_revisions" / str(submission.pk) / key
    # Disallow a symlinked history root from escaping the managed media tree.
    directory.resolve().relative_to(Path(settings.MEDIA_ROOT).resolve())
    directory.mkdir(parents=True, exist_ok=False)
    track_created_file(directory)
    selected = _selected_files(submission)
    pdf_name, pdf_metadata = _copy_snapshot(submission, "pdf", selected["pdf"], directory)
    source_name, source_metadata = _copy_snapshot(submission, "source", selected["source"], directory)
    previous = submission.formatting_revisions.first()
    for kind, metadata in (("pdf", pdf_metadata), ("source", source_metadata)):
        if (uploaded_names or {}).get(kind):
            metadata["filename"] = Path(uploaded_names[kind]).name
        elif previous:
            old_metadata = getattr(previous, f"{kind}_metadata")
            if old_metadata.get("live_name") == metadata["live_name"]:
                metadata["filename"] = old_metadata.get("filename") or metadata["filename"]
    number = (submission.formatting_revisions.aggregate(value=Max("number"))["value"] or 0) + 1
    revision = FormattingRevision.objects.create(
        submission=submission,
        number=number,
        revision_key=key,
        event_type=event_type,
        paper_id=submission.paper_id_filled,
        final_id=submission.final_submission_id,
        format_status=submission.format_status,
        format_notes=submission.format_notes,
        changed_files=list(changed_files),
        pdf_file=pdf_name,
        source_file=source_name,
        pdf_metadata=pdf_metadata,
        source_metadata=source_metadata,
    )
    return revision


def current_file_fingerprints(submission):
    fingerprints = {}
    inspection = FileInspectionContext()
    for kind, info in _selected_files(submission).items():
        metadata = _metadata(submission, kind, info)
        if info["exists"]:
            digest = inspection.sha256(info["path"], fresh=True)
            metadata.update(size=inspection.status(info["path"]).signature[2], sha256=digest)
        fingerprints[kind] = metadata
    return fingerprints


def revision_matches_current(revision, submission, *, fingerprints=None):
    fingerprints = fingerprints or current_file_fingerprints(submission)
    return all(
        all(
            getattr(revision, f"{kind}_metadata").get(key) == current.get(key)
            for key in ("origin", "live_name", "exists", "size", "sha256")
        )
        for kind, current in fingerprints.items()
    )


def ensure_formatting_baseline(submission):
    if not submission.has_corrected_files:
        return None
    latest = submission.formatting_revisions.first()
    if latest and revision_matches_current(latest, submission):
        return None
    revision = capture_revision(submission, event_type="baseline")
    audit_success(
        "formatting_revision_capture",
        "Existing formatting files captured for revision history.",
        submission=submission,
        extra=revision_audit_data(revision),
    )
    return revision


@revision_files_transaction()
def capture_existing_formatting(submission_id):
    submission = FinalSubmission.objects.select_for_update().get(pk=submission_id)
    return ensure_formatting_baseline(submission)


def revision_audit_data(revision):
    return {
        "revision_id": revision.pk,
        "revision_number": revision.number,
        "pdf": revision.pdf_metadata,
        "source": revision.source_metadata,
    }


def open_revision_file(revision, kind):
    """Serve verified snapshot bytes, never resolve through a live file helper."""
    field = getattr(revision, f"{kind}_file")
    metadata = getattr(revision, f"{kind}_metadata")
    if not field or not metadata.get("exists"):
        raise ValueError(f"This revision has no {kind} file.")
    root = Path(settings.MEDIA_ROOT).resolve()
    expected = root / "formatting_revisions" / str(revision.submission_id) / revision.revision_key
    path = Path(field.path).resolve()
    if path.parent != expected.resolve() or not path.is_relative_to(root):
        raise ValueError("Revision file is outside its managed revision folder.")
    snapshot = SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                snapshot.write(chunk)
        if snapshot.tell() != metadata.get("size") or digest.hexdigest() != metadata.get("sha256"):
            raise ValueError("Revision file failed its integrity check. No other version was substituted.")
        snapshot.seek(0)
        return snapshot
    except BaseException:
        snapshot.close()
        raise
