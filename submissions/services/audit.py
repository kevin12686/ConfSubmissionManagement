import json
import shutil
import uuid
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings as django_settings
from django.utils import timezone


AUDIT_FILENAME = "audit.log"
AUDIT_ARCHIVE_DIRNAME = "archive"
DEFAULT_ACTOR = "local_user"
SENSITIVE_PATH_PREFIXES = ("/var/", "/private/var/", "/tmp/", "/private/tmp/")


def audit_log_root():
    root = django_settings.BASE_DIR / "data" / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def audit_log_path():
    return audit_log_root() / AUDIT_FILENAME


def _portable_path(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return raw
    for root in [django_settings.BASE_DIR, django_settings.MEDIA_ROOT]:
        try:
            resolved_root = Path(root).resolve()
            resolved_path = path.resolve()
            relative = resolved_path.relative_to(resolved_root)
            prefix = "media:" if resolved_root == Path(django_settings.MEDIA_ROOT).resolve() else "project:"
            return f"{prefix}{relative.as_posix()}"
        except (OSError, RuntimeError, ValueError):
            continue
    return path.name


def _clean_value(value):
    if isinstance(value, Path):
        return _portable_path(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_value(item) for item in value]
    if hasattr(value, "pk") and value.__class__.__module__.startswith("submissions."):
        return _object_identity(value)
    if isinstance(value, str) and (
        value.startswith(SENSITIVE_PATH_PREFIXES) or "/" in value or "\\" in value
    ):
        maybe_path = Path(value).expanduser()
        if maybe_path.is_absolute():
            return _portable_path(value)
    return value


def _object_identity(obj):
    return {
        "model": obj.__class__.__name__,
        "pk": getattr(obj, "pk", None),
        "paper_id": getattr(obj, "paper_id", getattr(obj, "paper_id_filled", "")),
        "final_submission_id": getattr(obj, "final_submission_id", ""),
    }


def _request_path(request):
    if not request:
        return ""
    try:
        return request.get_full_path()
    except Exception:
        return ""


def _request_actor(_request):
    return DEFAULT_ACTOR


def write_audit_event(
    *,
    action,
    status,
    message="",
    request=None,
    object_type="",
    paper_id="",
    final_submission_id="",
    submission=None,
    changed_fields=None,
    before=None,
    after=None,
    reset_flags=None,
    file_changes=None,
    file_hashes=None,
    result_counts=None,
    error="",
    extra=None,
):
    if submission is not None:
        paper_id = paper_id or getattr(submission, "paper_id_filled", "")
        final_submission_id = final_submission_id or getattr(submission, "final_submission_id", "")
        object_type = object_type or submission.__class__.__name__
    event = {
        "timestamp": timezone.localtime(timezone.now()).isoformat(),
        "event_id": str(uuid.uuid4()),
        "app_version": getattr(django_settings, "APP_VERSION", ""),
        "state_archive_version": getattr(django_settings, "STATE_ARCHIVE_VERSION", ""),
        "actor": _request_actor(request),
        "action": action,
        "status": status,
        "message": message,
        "request_path": _request_path(request),
        "object_type": object_type,
        "paper_id": paper_id or "",
        "final_submission_id": final_submission_id or "",
        "changed_fields": list(changed_fields or []),
        "before": before or {},
        "after": after or {},
        "reset_flags": reset_flags or {},
        "file_changes": file_changes or {},
        "file_hashes": file_hashes or {},
        "result_counts": result_counts or {},
        "error": str(error or ""),
    }
    if extra:
        event["extra"] = extra
    event = _clean_value(event)
    path = audit_log_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def audit_success(action, message="", **kwargs):
    return write_audit_event(action=action, status="success", message=message, **kwargs)


def audit_failure(action, error, message="", **kwargs):
    return write_audit_event(
        action=action,
        status="failed",
        message=message,
        error=error,
        **kwargs,
    )


def audit_blocked(action, message="", **kwargs):
    return write_audit_event(action=action, status="blocked", message=message, **kwargs)


def audit_preview(action, message="", **kwargs):
    return write_audit_event(action=action, status="previewed", message=message, **kwargs)


def audit_requested(action, message="", **kwargs):
    return write_audit_event(action=action, status="requested", message=message, **kwargs)


def read_audit_log(query="", limit=300):
    path = audit_log_path()
    if not path.exists():
        return []
    query = (query or "").strip().lower()
    limit = max(1, int(limit))
    if query:
        with path.open("r", encoding="utf-8") as handle:
            lines = deque(
                (
                    line.strip()
                    for line in handle
                    if line.strip() and query in line.lower()
                ),
                maxlen=limit,
            )
    else:
        lines = _tail_utf8_lines(path, limit)
    rows = [_parse_audit_line(line) for line in lines]
    rows.reverse()
    return rows


def _tail_utf8_lines(path, limit, *, chunk_size=64 * 1024):
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        while position > 0 and buffer.count(b"\n") <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
    if position > 0 and b"\n" in buffer:
        buffer = buffer.split(b"\n", 1)[1]
    return [
        line
        for line in buffer.decode("utf-8").splitlines()[-limit:]
        if line.strip()
    ]


def _parse_audit_line(line):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        event = {
            "timestamp": "",
            "action": "unparseable_log_line",
            "status": "error",
            "message": line[:500],
            "raw": line,
        }
    event["raw_json"] = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return event


def audit_log_info():
    path = audit_log_path()
    if not path.exists():
        return {"path": path, "exists": False, "size": 0, "size_label": "0 B"}
    size = path.stat().st_size
    return {"path": path, "exists": True, "size": size, "size_label": _size_label(size)}


def archive_and_clear_audit_log(reason="clear_database"):
    path = audit_log_path()
    root = audit_log_root()
    archive_dir = root / AUDIT_ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_path = None
    if path.exists() and path.stat().st_size:
        stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M%S_%f")
        archived_path = (
            archive_dir
            / f"audit_before_{reason}_{stamp}_{uuid.uuid4().hex}.log"
        )
        shutil.move(str(path), archived_path)
    write_audit_event(
        action="audit_log_archived_and_cleared",
        status="success",
        message="Audit log archived and cleared.",
        file_changes={
            "archived_path": str(archived_path) if archived_path else "",
            "new_log_path": str(path),
        },
    )
    return archived_path


def _size_label(size):
    size = float(size or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
