import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from submissions.services.file_manager import sanitize_filename_part


PREVIEW_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


def purge_expired_preview_directories(root, ttl, *, now=None):
    root = Path(root)
    now = now or timezone.now()
    for token_root in root.iterdir():
        try:
            if (
                token_root.is_symlink()
                or not token_root.is_dir()
                or not PREVIEW_TOKEN_PATTERN.fullmatch(token_root.name)
            ):
                continue
            created_at = _preview_created_at(token_root)
            if created_at is not None and now - created_at > ttl:
                shutil.rmtree(token_root, ignore_errors=True)
        except OSError:
            continue


def save_preview_upload(file_obj, token_root, prefix):
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    original_name = Path(getattr(file_obj, "name", prefix)).name
    suffix = Path(original_name).suffix
    filename = (
        f"{prefix}-{sanitize_filename_part(Path(original_name).stem)}{suffix}"
    )
    path = Path(token_root) / filename
    digest = hashlib.sha256()
    size = 0
    with path.open("wb") as target:
        for chunk in file_obj.chunks():
            target.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    return {
        "path": str(path),
        "original_name": original_name,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _preview_created_at(token_root):
    payload_path = token_root / "payload.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if timezone.is_naive(created_at):
            created_at = timezone.make_aware(created_at)
        return created_at
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
