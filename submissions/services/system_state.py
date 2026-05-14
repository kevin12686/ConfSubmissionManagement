import hashlib
import json
import shutil
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from submissions.models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    InitialPaper,
    PaperAuthor,
    sync_final_submission_state_records,
)
from submissions.services.file_manager import resolve_folder


STATE_ARCHIVE_VERSION = django_settings.STATE_ARCHIVE_VERSION
CONFIRMATION_TEXT = "RESTORE SYSTEM STATE"
RESTORE_PREVIEW_TTL_SECONDS = 2 * 60 * 60

DEFAULT_FOLDER_SETTINGS = {
    "incoming_folder": "data/incoming",
    "active_final_folder": "data/active_final",
    "old_versions_folder": "data/old_versions",
    "reports_folder": "data/reports",
    "extraction_results_folder": "data/extraction_results",
    "plagiarism_reports_folder": "data/plagiarism_reports",
}
TEMP_PATH_PREFIXES = ("/var/", "/private/var/", "/tmp/", "/private/tmp/")
FOLDER_SETTING_FIELDS = set(DEFAULT_FOLDER_SETTINGS)
PATH_TEXT_FIELDS = {
    "current_file_path",
    "source_current_file_path",
    "thumbnail_folder",
    "title_author_verification_image",
    "plagiarism_report_path",
}
TEMP_SNAPSHOT_EXCLUDED_DIRS = {
    "formatting_upload_previews",
    "import_previews",
    "storage_cleanup_previews",
    "system_state_restore_previews",
    "system_state_backups",
}
MODEL_SPECS = [
    ("settings", AppSetting),
    ("initial_papers", InitialPaper),
    ("final_submissions", FinalSubmission),
    ("paper_authors", PaperAuthor),
    ("author_limit_waivers", AuthorLimitWaiver),
]
RESTORE_MODEL_ORDER = [
    ("settings", AppSetting),
    ("initial_papers", InitialPaper),
    ("final_submissions", FinalSubmission),
    ("paper_authors", PaperAuthor),
    ("author_limit_waivers", AuthorLimitWaiver),
]


class SystemStateError(Exception):
    pass


def system_state_reports_root():
    root = django_settings.BASE_DIR / "data" / "system_state_backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def restore_preview_root():
    root = django_settings.BASE_DIR / "data" / "system_state_restore_previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_stamp():
    return timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M%S")


def _field_value(obj, field, path_aliases=None, root_aliases=None):
    value = field.value_from_object(obj)
    if isinstance(field, models.FileField):
        return value.name if value else ""
    if field.name in FOLDER_SETTING_FIELDS:
        return _portable_folder_setting(field.name, value)
    if field.name in PATH_TEXT_FIELDS and isinstance(value, str):
        return _portable_path_value(value, path_aliases or {}, root_aliases or {})
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _serialize_queryset(model, path_aliases=None, root_aliases=None):
    rows = []
    concrete_fields = [field for field in model._meta.fields if field.concrete]
    for obj in model.objects.order_by("pk"):
        row = {}
        for field in concrete_fields:
            row[field.attname] = _field_value(obj, field, path_aliases, root_aliases)
        rows.append(row)
    return rows


def _model_counts():
    return {key: model.objects.count() for key, model in MODEL_SPECS}


def database_signature():
    parts = []
    for key, model in MODEL_SPECS:
        count = model.objects.count()
        updated_values = []
        if any(field.name == "updated_at" for field in model._meta.fields):
            updated_values = list(
                model.objects.exclude(updated_at__isnull=True)
                .order_by("updated_at")
                .values_list("updated_at", flat=True)
            )
        latest = updated_values[-1].isoformat() if updated_values else ""
        parts.append(f"{key}:{count}:{latest}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _is_temp_path(path_text):
    return any(str(path_text).startswith(prefix) for prefix in TEMP_PATH_PREFIXES)


def _managed_roots(settings_obj):
    roots = []

    def add(kind, path, label):
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = django_settings.BASE_DIR / path
        roots.append((kind, path, label))

    add("media", django_settings.MEDIA_ROOT, "media")
    add("project", django_settings.BASE_DIR / "data" / "crosscheck_upload", "crosscheck_upload")
    for field_name in DEFAULT_FOLDER_SETTINGS:
        add("project", resolve_folder(getattr(settings_obj, field_name)), field_name)
    return roots


def _is_excluded_file(path):
    return bool(TEMP_SNAPSHOT_EXCLUDED_DIRS & set(path.parts))


def _path_candidates(value):
    candidates = []
    raw = str(value).strip()
    if not raw:
        return candidates

    def add(path):
        try:
            resolved = str(Path(path).expanduser().resolve())
        except (OSError, RuntimeError):
            resolved = str(path)
        if resolved not in candidates:
            candidates.append(resolved)

    add(raw)
    media_url = str(django_settings.MEDIA_URL or "")
    if media_url and raw.startswith(media_url):
        add(Path(django_settings.MEDIA_ROOT) / raw[len(media_url) :].lstrip("/"))
    raw_path = Path(raw).expanduser()
    if not raw_path.is_absolute():
        add(Path(django_settings.MEDIA_ROOT) / raw)
        add(django_settings.BASE_DIR / raw)
    return candidates


def _collect_file_entries(settings_obj):
    entries = []
    seen = set()
    root_maps = []
    path_aliases = {}
    root_aliases = {}
    root_index = 1
    file_index = 1
    for kind, root, label in _managed_roots(settings_obj):
        if not root.exists():
            continue
        resolved_root = root.resolve()
        root_id = f"root-{root_index:04}"
        root_index += 1
        if kind == "media":
            root_restore_kind = "media"
            root_restore_rel = ""
        elif _is_relative_to(resolved_root, django_settings.BASE_DIR):
            root_restore_kind = "project"
            root_restore_rel = resolved_root.relative_to(django_settings.BASE_DIR).as_posix()
        else:
            root_restore_kind = "project"
            root_restore_rel = f"data/restored_external/{label}"
        root_aliases[str(resolved_root)] = {
            "token": f"snapshot-root:{root_id}",
            "restore_kind": root_restore_kind,
            "restore_rel": root_restore_rel,
        }
        root_maps.append(
            {
                "root_id": root_id,
                "label": label,
                "restore_kind": root_restore_kind,
                "restore_rel": root_restore_rel,
            }
        )
        if root.is_file():
            files = [root]
        else:
            files = [path for path in root.rglob("*") if path.is_file()]
        for path in files:
            path = path.resolve()
            if path in seen or _is_excluded_file(path):
                continue
            seen.add(path)
            if kind == "media":
                rel = path.relative_to(Path(django_settings.MEDIA_ROOT).resolve()).as_posix()
                zip_path = f"files/media/{rel}"
                restore_rel = rel
            elif _is_relative_to(path, django_settings.BASE_DIR):
                rel = path.relative_to(django_settings.BASE_DIR).as_posix()
                zip_path = f"files/project/{rel}"
                restore_rel = rel
            else:
                rel = f"{label}/{path.relative_to(root.resolve()).as_posix()}"
                zip_path = f"files/external/{rel}"
                restore_rel = f"data/restored_external/{rel}"
            file_id = f"file-{file_index:06}"
            file_index += 1
            path_aliases[str(path)] = {
                "token": f"snapshot-file:{file_id}",
                "restore_kind": kind if kind == "media" else "project",
                "restore_rel": restore_rel,
            }
            entries.append(
                {
                    "file_id": file_id,
                    "zip_path": zip_path,
                    "source_path": str(path),
                    "restore_kind": kind if kind == "media" else "project",
                    "restore_rel": restore_rel,
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return entries, root_maps, path_aliases, root_aliases


def _is_relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def export_system_state(reason="manual"):
    settings_obj = AppSetting.load()
    file_entries, root_maps, path_aliases, root_aliases = _collect_file_entries(settings_obj)
    exported_at = timezone.now()
    snapshot_name = f"system_state_{_now_stamp()}.zip"
    target = system_state_reports_root() / snapshot_name
    state = {
        "snapshot_version": STATE_ARCHIVE_VERSION,
        "state_archive_version": STATE_ARCHIVE_VERSION,
        "app_name": django_settings.APP_NAME,
        "app_version": django_settings.APP_VERSION,
        "exported_at": exported_at.isoformat(),
        "conference_name": settings_obj.conference_name,
        "database_signature": database_signature(),
        "models": {
            key: _serialize_queryset(model, path_aliases, root_aliases)
            for key, model in MODEL_SPECS
        },
    }
    manifest_files = [
        {key: value for key, value in entry.items() if key != "source_path"}
        for entry in file_entries
    ]
    manifest = {
        "snapshot_version": STATE_ARCHIVE_VERSION,
        "state_archive_version": STATE_ARCHIVE_VERSION,
        "app_name": django_settings.APP_NAME,
        "app_version": django_settings.APP_VERSION,
        "reason": reason,
        "exported_at": exported_at.isoformat(),
        "conference_name": settings_obj.conference_name,
        "record_counts": _model_counts(),
        "database_signature": state["database_signature"],
        "file_count": len(file_entries),
        "artifact_counts": _artifact_counts(manifest_files),
        "files": manifest_files,
        "root_maps": root_maps,
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        archive.writestr("state.json", json.dumps(state, indent=2, sort_keys=True))
        for entry in file_entries:
            archive.write(entry["source_path"], entry["zip_path"])
    return {
        "path": target,
        "filename": target.name,
        "manifest": manifest,
    }


def preview_system_state_restore(uploaded_file):
    token = uuid.uuid4().hex
    target_dir = restore_preview_root() / token
    target_dir.mkdir(parents=True, exist_ok=False)
    zip_path = target_dir / "snapshot.zip"
    with zip_path.open("wb") as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)
    manifest, state = _read_snapshot(zip_path)
    validation = _validate_snapshot(zip_path, manifest)
    preview = {
        "token": token,
        "created_at": timezone.now().isoformat(),
        "expires_at": (timezone.now() + timedelta(seconds=RESTORE_PREVIEW_TTL_SECONDS)).isoformat(),
        "database_signature": database_signature(),
        "conference_name": manifest.get("conference_name") or "",
        "app_name": manifest.get("app_name") or "Conference Final Manager",
        "app_version": manifest.get("app_version") or "Unknown",
        "state_archive_version": manifest.get("state_archive_version", manifest.get("snapshot_version")),
        "record_counts": manifest.get("record_counts", {}),
        "file_count": manifest.get("file_count", 0),
        "artifact_counts": manifest.get("artifact_counts") or _artifact_counts(
            manifest.get("files", [])
        ),
        "missing_files": validation["missing"],
        "corrupt_files": validation["corrupt"],
        "referenced_artifact_warnings": _referenced_artifact_warnings(state),
        "settings_summary": _settings_summary(state),
    }
    (target_dir / "preview.json").write_text(json.dumps(preview, indent=2), encoding="utf-8")
    return preview


def load_restore_preview(token):
    preview_path = restore_preview_root() / token / "preview.json"
    if not preview_path.exists():
        raise SystemStateError("Restore preview was not found. Upload the snapshot again.")
    return json.loads(preview_path.read_text(encoding="utf-8"))


def apply_system_state_restore(token, confirmation):
    if confirmation.strip() != CONFIRMATION_TEXT:
        raise SystemStateError(f'Type "{CONFIRMATION_TEXT}" to apply this restore.')
    preview = load_restore_preview(token)
    created_at = parse_datetime(preview["created_at"])
    if created_at and timezone.now() - created_at > timedelta(seconds=RESTORE_PREVIEW_TTL_SECONDS):
        raise SystemStateError("Restore preview expired. Upload the snapshot again.")
    if preview["database_signature"] != database_signature():
        raise SystemStateError("Database changed after preview. Upload the snapshot again before restoring.")
    if preview["missing_files"] or preview["corrupt_files"]:
        raise SystemStateError("Snapshot files did not pass validation. Restore was not applied.")

    preview_dir = restore_preview_root() / token
    zip_path = preview_dir / "snapshot.zip"
    manifest, state = _read_snapshot(zip_path)
    pre_restore = export_system_state(reason="pre_restore_backup")
    settings_before_restore = AppSetting.load()
    with transaction.atomic():
        _clear_database_rows()
        _clear_managed_files_for_restore(settings_before_restore, state)
        path_map, root_map = _extract_snapshot_files(zip_path, manifest)
        _restore_models(state, path_map, root_map)
    shutil.rmtree(preview_dir, ignore_errors=True)
    return {
        "pre_restore_backup": pre_restore["path"],
        "restored_counts": manifest.get("record_counts", {}),
        "conference_name": manifest.get("conference_name") or "",
    }


def _read_snapshot(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            state = json.loads(archive.read("state.json").decode("utf-8"))
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise SystemStateError(f"Invalid system state ZIP: {exc}") from exc
    archive_version = manifest.get("state_archive_version", manifest.get("snapshot_version"))
    if archive_version != STATE_ARCHIVE_VERSION:
        raise SystemStateError("Unsupported system state snapshot version.")
    return manifest, state


def _validate_snapshot(zip_path, manifest):
    missing = []
    corrupt = []
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        for entry in manifest.get("files", []):
            if entry["zip_path"] not in names:
                missing.append(entry["zip_path"])
                continue
            digest = hashlib.sha256(archive.read(entry["zip_path"])).hexdigest()
            if digest != entry.get("sha256"):
                corrupt.append(entry["zip_path"])
    return {"missing": missing, "corrupt": corrupt}


def _settings_summary(state):
    rows = state.get("models", {}).get("settings", [])
    if not rows:
        return {}
    row = rows[0]
    keys = [
        "conference_name",
        "page_minimum",
        "page_limit",
        "author_paper_limit",
        "max_authors_per_paper",
        "active_version_rule",
        "time_zone",
        "plagiarism_percent_threshold",
        "single_similarity_threshold",
    ]
    return {key: row.get(key, "") for key in keys}


def _clear_database_rows():
    PaperAuthor.objects.all().delete()
    FinalSubmission.objects.all().delete()
    InitialPaper.objects.all().delete()
    AuthorLimitWaiver.objects.all().delete()
    AppSetting.objects.all().delete()


def _clear_managed_files_for_restore(settings_obj, state):
    folders = {
        Path(django_settings.MEDIA_ROOT),
        django_settings.BASE_DIR / "data" / "crosscheck_upload",
        django_settings.BASE_DIR / "data" / "restored_external",
    }
    for field_name in DEFAULT_FOLDER_SETTINGS:
        folders.add(resolve_folder(getattr(settings_obj, field_name)))
    for raw_value in _snapshot_folder_values(state):
        folders.add(resolve_folder(raw_value))
    for folder in folders:
        folder = folder if folder.is_absolute() else django_settings.BASE_DIR / folder
        if folder.exists():
            for child in folder.iterdir():
                if child.name in {"system_state_backups", "system_state_restore_previews"}:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            folder.mkdir(parents=True, exist_ok=True)


def _snapshot_folder_values(state):
    rows = state.get("models", {}).get("settings", [])
    if not rows:
        return []
    return [
        rows[0].get(field_name, "")
        for field_name in DEFAULT_FOLDER_SETTINGS
        if rows[0].get(field_name)
    ]


def _portable_folder_setting(field_name, value):
    if not value:
        return DEFAULT_FOLDER_SETTINGS[field_name]
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        return str(value)
    try:
        return path.resolve().relative_to(django_settings.BASE_DIR).as_posix()
    except ValueError:
        return f"data/restored_external_folders/{field_name}"


def _portable_path_value(value, path_aliases, root_aliases):
    if not value:
        return value
    candidates = _path_candidates(value)
    for resolved_value in candidates:
        if resolved_value in path_aliases:
            return path_aliases[resolved_value]["token"]
    for old_root, alias in sorted(root_aliases.items(), key=lambda item: len(item[0]), reverse=True):
        for resolved_value in candidates:
            if resolved_value == old_root:
                return alias["token"]
            prefix = old_root.rstrip("/") + "/"
            if resolved_value.startswith(prefix):
                return f"{alias['token']}/{resolved_value[len(prefix):]}"
    if _is_temp_path(str(value)) or Path(str(value)).is_absolute():
        return ""
    return value


def _artifact_counts(files):
    counts = {
        "original_and_corrected_files": 0,
        "title_author_verification_images": 0,
        "pdf_thumbnails": 0,
        "format_previews": 0,
        "reports_exports": 0,
        "other_files": 0,
    }
    for entry in files:
        zip_path = entry.get("zip_path", "")
        if "/title_author_verification/" in zip_path:
            counts["title_author_verification_images"] += 1
        elif "/pdf_thumbnails/" in zip_path:
            counts["pdf_thumbnails"] += 1
        elif "/format_previews/" in zip_path:
            counts["format_previews"] += 1
        elif (
            "/reports/" in zip_path
            or "/plagiarism_reports/" in zip_path
            or "/crosscheck_upload/" in zip_path
            or "/plagiarism_upload/" in zip_path
        ):
            counts["reports_exports"] += 1
        elif (
            "/final_submissions/" in zip_path
            or "/source_submissions/" in zip_path
            or "/formatted_pdfs/" in zip_path
            or "/formatted_sources/" in zip_path
            or "/active_final/" in zip_path
            or "/old_versions/" in zip_path
        ):
            counts["original_and_corrected_files"] += 1
        else:
            counts["other_files"] += 1
    return counts


def _referenced_artifact_warnings(state):
    warnings = []
    for row in state.get("models", {}).get("final_submissions", []):
        final_id = row.get("final_submission_id") or row.get("id") or "Unknown"
        image = row.get("title_author_verification_image") or ""
        if image and not str(image).startswith(("snapshot-file:", "snapshot-root:")):
            warnings.append(
                {
                    "final_submission_id": final_id,
                    "field": "title_author_verification_image",
                    "message": "Verification image path was not included as a portable snapshot file.",
                }
            )
        thumbnail_folder = row.get("thumbnail_folder") or ""
        if thumbnail_folder and not str(thumbnail_folder).startswith(("snapshot-file:", "snapshot-root:")):
            warnings.append(
                {
                    "final_submission_id": final_id,
                    "field": "thumbnail_folder",
                    "message": "Thumbnail folder path was not included as a portable snapshot path.",
                }
            )
    return warnings


def _restore_target(entry):
    if entry["restore_kind"] == "media":
        return Path(django_settings.MEDIA_ROOT) / entry["restore_rel"]
    return django_settings.BASE_DIR / entry["restore_rel"]


def _extract_snapshot_files(zip_path, manifest):
    path_map = {}
    root_map = []
    with zipfile.ZipFile(zip_path) as archive:
        for entry in manifest.get("files", []):
            target = _restore_target(entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry["zip_path"]) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            if _sha256(target) != entry.get("sha256"):
                raise SystemStateError(f"Restored file hash mismatch: {entry['zip_path']}")
            path_map[f"snapshot-file:{entry['file_id']}"] = str(target)
        for entry in manifest.get("root_maps", []):
            if entry["restore_kind"] == "media":
                restored_root = Path(django_settings.MEDIA_ROOT) / entry["restore_rel"]
            else:
                restored_root = django_settings.BASE_DIR / entry["restore_rel"]
            root_map.append((f"snapshot-root:{entry['root_id']}", str(restored_root)))
    return path_map, root_map


def _restore_models(state, path_map, root_map):
    models_payload = state.get("models", {})
    for key, model in RESTORE_MODEL_ORDER:
        objs = []
        for raw_row in models_payload.get(key, []):
            row = {}
            for field in model._meta.fields:
                payload_key = field.attname
                legacy_key = field.name
                if payload_key not in raw_row and legacy_key not in raw_row:
                    continue
                value = raw_row.get(payload_key, raw_row.get(legacy_key))
                row[payload_key] = _deserialize_value(field, value, path_map, root_map)
            obj = model(**row)
            try:
                obj.full_clean(validate_unique=False)
            except ValidationError:
                # Existing imported projects may contain legacy free-text choices; preserve them.
                pass
            objs.append(obj)
        if objs:
            model.objects.bulk_create(objs)
    if not AppSetting.objects.exists():
        AppSetting.load()
    sync_final_submission_state_records()


def _deserialize_value(field, value, path_map, root_map):
    if value in ("", None):
        return value
    if field.name in FOLDER_SETTING_FIELDS:
        return _restore_folder_setting(field.name, value)
    if field.name in PATH_TEXT_FIELDS and isinstance(value, str):
        return _remap_path_value(value, path_map, root_map)
    if isinstance(field, models.DateTimeField):
        return parse_datetime(value) if value else None
    if isinstance(field, models.DecimalField):
        return Decimal(str(value)) if value not in ("", None) else None
    if isinstance(field, (models.IntegerField, models.PositiveIntegerField, models.BigAutoField, models.AutoField)):
        return int(value) if value not in ("", None) else None
    if isinstance(field, models.BooleanField):
        return bool(value)
    return value


def _restore_folder_setting(field_name, value):
    if isinstance(value, str):
        if _is_temp_path(value):
            return f"data/restored_external_folders/{field_name}"
        path = Path(value).expanduser()
        if path.is_absolute():
            try:
                return path.resolve().relative_to(django_settings.BASE_DIR).as_posix()
            except ValueError:
                return f"data/restored_external_folders/{field_name}"
    return value


def _remap_path_value(value, path_map, root_map):
    if value in path_map:
        return path_map[value]
    if isinstance(value, str) and value.startswith("snapshot-root:"):
        for root_token, restored_root in root_map:
            if value == root_token:
                return restored_root
            prefix = root_token + "/"
            if value.startswith(prefix):
                return str(Path(restored_root) / value[len(prefix) :])
    candidates = _path_candidates(value)
    for resolved_value in candidates:
        if resolved_value in path_map:
            return path_map[resolved_value]
    for old_root, new_root in sorted(root_map, key=lambda item: len(item[0]), reverse=True):
        compare_values = {value, *candidates}
        if old_root in compare_values:
            return new_root
        prefix = old_root.rstrip("/") + "/"
        for candidate in compare_values:
            if candidate.startswith(prefix):
                return str(Path(new_root) / candidate[len(prefix) :])
    if _is_temp_path(value):
        return ""
    return value
