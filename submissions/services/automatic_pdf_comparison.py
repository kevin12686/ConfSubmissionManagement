"""Post-save comparison requests; never part of the Formatting transaction."""

import json
import logging
import uuid
from contextlib import contextmanager

from django.core import signing
from django.core.files import locks
from django.utils import timezone

from submissions.services.audit import audit_failure
from submissions.services.pdf_comparison import (
    PDFComparisonError, StaleUploadComparison, TTL, comparison_identity,
    comparison_root, comparison_submission, create_pdf_comparison,
    load_pdf_comparison, validate_upload_binding,
)
from submissions.services.preview_storage import PREVIEW_TOKEN_PATTERN, purge_expired_preview_directories


logger = logging.getLogger(__name__)
SESSION_KEY = "formatting_upload_comparisons"
REQUEST_SALT = "submissions.formatting-upload-comparison.v1"
CACHE_SALT = "submissions.formatting-upload-comparison-cache.v1"


class ComparisonInProgress(PDFComparisonError):
    pass


def remember_upload_comparison(session, result):
    revision = result.revision
    if not revision or "pdf" not in revision.changed_files:
        return None
    binding = {
        "revision_id": revision.pk, "revision_key": revision.revision_key,
        "revision_number": revision.number, "pdf_sha256": revision.pdf_metadata["sha256"],
        "identity": comparison_identity(result.submission),
        "created_at": timezone.now().isoformat(),
    }
    entry = {
        "submission_id": result.submission.pk, "paper_id": revision.paper_id,
        "final_id": revision.final_id, "revision_number": revision.number,
        "token": signing.dumps(binding, salt=REQUEST_SALT, compress=True),
    }
    entries = [item for item in session.get(SESSION_KEY, []) if item.get("submission_id") != result.submission.pk]
    session[SESSION_KEY] = [entry, *entries][:20]
    return entry


def decode_upload_comparison(submission_id, token):
    try:
        if not isinstance(token, str) or len(token) > 8192:
            raise ValueError
        binding = signing.loads(token, salt=REQUEST_SALT, max_age=int(TTL.total_seconds()))
        if (
            binding["identity"]["submission_id"] != submission_id
            or not PREVIEW_TOKEN_PATTERN.fullmatch(binding["revision_key"])
            or not isinstance(binding["revision_id"], int)
            or len(binding["pdf_sha256"]) != 64
        ):
            raise ValueError
        return binding
    except (signing.BadSignature, ValueError, KeyError, TypeError) as exc:
        raise StaleUploadComparison("This upload comparison request expired or is invalid. The saved files are unchanged; use Compare PDF for the current files.") from exc


def upload_comparison_entries(session):
    entries = []
    for item in session.get(SESSION_KEY, []):
        try:
            decode_upload_comparison(item["submission_id"], item["token"])
        except (PDFComparisonError, KeyError):
            continue
        entries.append({**item, "autostart": not entries})
    return entries


def _request_directory(binding, *, create=False):
    root = comparison_root(create=create) / "requests"
    directory = root / binding["revision_key"]
    if root.is_symlink() or directory.is_symlink():
        raise PDFComparisonError("Invalid upload comparison request folder.")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        purge_expired_preview_directories(root, TTL)
        directory.mkdir(exist_ok=True)
    return directory


@contextmanager
def _request_lock(directory):
    path = directory / "request.lock"
    if path.is_symlink():
        raise PDFComparisonError("Invalid comparison lock.")
    with path.open("a+b") as handle:
        if not locks.lock(handle, locks.LOCK_EX | locks.LOCK_NB):
            raise ComparisonInProgress("This upload comparison is already running. Use Retry to check it again; no duplicate comparison was started.")
        try:
            yield
        finally:
            locks.unlock(handle)


def _write_cache(directory, binding, result_token=""):
    wrapper = {
        "created_at": binding["created_at"],
        "signed": signing.dumps({"binding": binding, "result_token": result_token}, salt=CACHE_SALT, compress=True),
    }
    temporary = directory / (uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(wrapper), encoding="utf-8")
        temporary.replace(directory / "payload.json")
    finally:
        temporary.unlink(missing_ok=True)


def _cached_result(directory, binding):
    path = directory / "payload.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 65536:
        raise PDFComparisonError("The comparison request cache is invalid. Retry to rebuild it.")
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        cached = signing.loads(wrapper["signed"], salt=CACHE_SALT, max_age=int(TTL.total_seconds()))
        if cached["binding"] != binding:
            raise ValueError
        if not cached["result_token"]:
            return None
        payload = load_pdf_comparison(binding["identity"]["submission_id"], cached["result_token"])
        if payload.get("upload_binding") != binding or payload["baseline"]["key"] != "original":
            raise ValueError
        return payload
    except StaleUploadComparison:
        raise
    except (OSError, ValueError, KeyError, signing.BadSignature) as exc:
        raise PDFComparisonError("The saved comparison is unavailable. Retry to rebuild this upload comparison.") from exc


def _current_upload(submission_id, binding):
    try:
        submission = comparison_submission(submission_id)
    except PDFComparisonError as exc:
        raise StaleUploadComparison(str(exc)) from exc
    validate_upload_binding(submission, binding)
    return submission


def get_upload_comparison(submission_id, token, *, build=False, retry=False, request=None):
    """GET reads only; POST may build once under a portable OS file lock."""
    submission = None
    creating = False
    created = False
    try:
        binding = decode_upload_comparison(submission_id, token)
        submission = _current_upload(submission_id, binding)
        directory = _request_directory(binding, create=build)
        if not build:
            return _cached_result(directory, binding)
        with _request_lock(directory):
            _current_upload(submission_id, binding)
            try:
                payload = _cached_result(directory, binding)
            except StaleUploadComparison:
                raise
            except PDFComparisonError:
                if not retry:
                    raise
                payload = None
            if payload:
                return payload
            _write_cache(directory, binding)
            creating = True
            payload = create_pdf_comparison(submission_id, "original", request=request, expected_upload=binding)
            created = True
            _write_cache(directory, binding, payload["token"])
            return _cached_result(directory, binding)
    except Exception as exc:
        if build and not isinstance(exc, ComparisonInProgress) and (not creating or created):
            try:
                audit_failure("formatting_pdf_compare", exc, "Automatic comparison unavailable; the Formatting upload remains saved.", request=request, submission=submission, extra={"automatic": True})
            except Exception:
                logger.exception("Could not audit automatic comparison failure")
        raise
