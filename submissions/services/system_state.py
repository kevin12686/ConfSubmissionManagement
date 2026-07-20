import hashlib
import json
import re
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
from submissions.services.audit import (
    audit_failure,
    audit_log_root,
    audit_preview,
    audit_requested,
    audit_success,
)
from submissions.services.file_manager import resolve_folder


STATE_ARCHIVE_VERSION = django_settings.STATE_ARCHIVE_VERSION
CONFIRMATION_TEXT = "RESTORE SYSTEM STATE"
RESTORE_PREVIEW_TTL_SECONDS = 2 * 60 * 60

DEFAULT_FOLDER_SETTINGS = {
    "incoming_folder": "data/incoming",
    "active_final_folder": "data/active_final",
    "old_versions_folder": "data/old_versions",
    "publication_pdf_debug_folder": "data/publication_pdf_debug",
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
RESTORE_PRESERVED_CHILDREN = {
    "system_state_backups",
    "system_state_restore_previews",
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
    return timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M%S_%f")


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


def _model_payload_sha256(models_payload):
    encoded = json.dumps(
        models_payload,
        default=str,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def database_signature():
    payload = []
    for key, model in MODEL_SPECS:
        fields = [field.attname for field in model._meta.concrete_fields]
        payload.append(
            (
                key,
                list(model.objects.order_by("pk").values_list(*fields)),
            )
        )
    encoded = json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_temp_path(path_text):
    return any(str(path_text).startswith(prefix) for prefix in TEMP_PATH_PREFIXES)


def _resolved_path(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = django_settings.BASE_DIR / path
    return path.resolve(strict=False)


def _project_data_root():
    return (django_settings.BASE_DIR / "data").resolve(strict=False)


def _protected_restore_roots():
    data_root = _project_data_root()
    return {
        data_root / "system_state_backups",
        data_root / "system_state_restore_previews",
    }


def _validate_media_root():
    root = _resolved_path(django_settings.MEDIA_ROOT)
    base_dir = Path(django_settings.BASE_DIR).resolve(strict=False)
    home = Path.home().resolve(strict=False)
    unsafe_roots = {Path(root.anchor), base_dir, _project_data_root(), home}
    if root in unsafe_roots or _is_relative_to(base_dir, root):
        raise SystemStateError(
            f"MEDIA_ROOT is too broad for destructive restore: {root}"
        )
    return root


def _validate_project_restore_root(path):
    root = _resolved_path(path)
    data_root = _project_data_root()
    if root == data_root or not _is_relative_to(root, data_root):
        raise SystemStateError(
            "System State restore targets must be strict children of "
            f"{data_root}: {root}"
        )
    for protected in _protected_restore_roots():
        if (
            root == protected
            or _is_relative_to(root, protected)
            or _is_relative_to(protected, root)
        ):
            raise SystemStateError(
                f"System State restore target overlaps protected recovery data: {root}"
            )
    return root


def _validate_export_root(path, label):
    root = _resolved_path(path)
    base_dir = Path(django_settings.BASE_DIR).resolve(strict=False)
    data_root = _project_data_root()
    if root in {Path(root.anchor), base_dir, data_root} or _is_relative_to(
        base_dir,
        root,
    ):
        raise SystemStateError(
            f"{label} is too broad for System State export: {root}"
        )
    return root


def _portable_project_restore_rel(root, label):
    root = Path(root).resolve(strict=False)
    data_root = _project_data_root()
    if root != data_root and _is_relative_to(root, data_root):
        return root.relative_to(django_settings.BASE_DIR).as_posix()
    return f"data/restored_external/{label}"


def _managed_roots(settings_obj):
    roots = []

    def add(kind, path, label):
        if kind == "media":
            path = _validate_media_root()
        else:
            path = _validate_export_root(path, label)
        roots.append((kind, path, label))

    add("media", django_settings.MEDIA_ROOT, "media")
    add("project", audit_log_root(), "audit_logs")
    add("project", django_settings.BASE_DIR / "data" / "crosscheck_upload", "crosscheck_upload")
    for field_name in DEFAULT_FOLDER_SETTINGS:
        add("project", getattr(settings_obj, field_name), field_name)
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
        elif label == "audit_logs":
            root_restore_kind = "project"
            root_restore_rel = "data/logs"
        else:
            root_restore_kind = "project"
            root_restore_rel = _portable_project_restore_rel(
                resolved_root,
                label,
            )
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
            elif label == "audit_logs":
                rel = path.relative_to(root.resolve()).as_posix()
                zip_path = f"files/project/data/logs/{rel}"
                restore_rel = f"data/logs/{rel}"
            else:
                rel = path.relative_to(root.resolve()).as_posix()
                restore_rel = (
                    Path(root_restore_rel) / rel
                ).as_posix()
                zip_path = f"files/project/{restore_rel}"
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
    target = None
    pending_target = None
    try:
        audit_requested(
            "system_state_export_requested",
            "System state ZIP export requested.",
            extra={"reason": reason},
        )
        database_before = database_signature()
        settings_obj = AppSetting.load()
        file_entries, root_maps, path_aliases, root_aliases = _collect_file_entries(settings_obj)
        exported_at = timezone.now()
        snapshot_name = f"system_state_{_now_stamp()}.zip"
        target = system_state_reports_root() / snapshot_name
        pending_target = target.with_suffix(f"{target.suffix}.part")
        models_payload = {
            key: _serialize_queryset(model, path_aliases, root_aliases)
            for key, model in MODEL_SPECS
        }
        if database_signature() != database_before:
            raise SystemStateError(
                "Database changed while the System State snapshot was being built."
            )
        model_payload_sha256 = _model_payload_sha256(models_payload)
        state = {
            "snapshot_version": STATE_ARCHIVE_VERSION,
            "state_archive_version": STATE_ARCHIVE_VERSION,
            "app_name": django_settings.APP_NAME,
            "app_version": django_settings.APP_VERSION,
            "exported_at": exported_at.isoformat(),
            "conference_name": settings_obj.conference_name,
            "database_signature": database_before,
            "model_payload_sha256": model_payload_sha256,
            "models": models_payload,
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
            "record_counts": {
                key: len(rows)
                for key, rows in models_payload.items()
            },
            "database_signature": state["database_signature"],
            "model_payload_sha256": model_payload_sha256,
            "file_count": len(file_entries),
            "artifact_counts": _artifact_counts(manifest_files),
            "files": manifest_files,
            "root_maps": root_maps,
        }
        with zipfile.ZipFile(
            pending_target,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            archive.writestr("state.json", json.dumps(state, indent=2, sort_keys=True))
            for entry in file_entries:
                archive.write(entry["source_path"], entry["zip_path"])
        with zipfile.ZipFile(pending_target) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SystemStateError(
                    "System State ZIP contains duplicate entry names."
                )
            corrupt_entry = archive.testzip()
            if corrupt_entry:
                raise SystemStateError(
                    f"System State ZIP verification failed for {corrupt_entry}."
                )
            for entry in file_entries:
                digest = hashlib.sha256(
                    archive.read(entry["zip_path"])
                ).hexdigest()
                if digest != entry["sha256"]:
                    raise SystemStateError(
                        "A managed file changed while the System State snapshot "
                        f"was being written: {entry['source_path']}"
                    )
        if database_signature() != database_before:
            raise SystemStateError(
                "Database changed while the System State snapshot was being written."
            )
        pending_target.replace(target)
        audit_success(
            "system_state_export",
            "System state ZIP exported.",
            result_counts={"file_count": len(file_entries), **_model_counts()},
            file_changes={"path": str(target)},
            extra={"reason": reason},
        )
        return {
            "path": target,
            "filename": target.name,
            "manifest": manifest,
        }
    except Exception as exc:
        for path in (pending_target, target):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            audit_failure(
                "system_state_export",
                exc,
                "System state ZIP export failed.",
                extra={"reason": reason},
            )
        except OSError:
            pass
        raise


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
    _restore_managed_folders(AppSetting.load(), state, manifest)
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
    audit_preview(
        "system_state_restore_preview",
        "System state restore preview created.",
        result_counts={
            "file_count": preview["file_count"],
            "missing_files": len(preview["missing_files"]),
            "corrupt_files": len(preview["corrupt_files"]),
        },
        extra={"token": token, "conference_name": preview["conference_name"]},
    )
    return preview


def load_restore_preview(token):
    preview_path = restore_preview_root() / token / "preview.json"
    if not preview_path.exists():
        raise SystemStateError("Restore preview was not found. Upload the snapshot again.")
    return json.loads(preview_path.read_text(encoding="utf-8"))


def _restore_atomic():
    return transaction.atomic()


def apply_system_state_restore(token, confirmation):
    try:
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
        validation = _validate_snapshot(zip_path, manifest)
        if validation["missing"] or validation["corrupt"]:
            raise SystemStateError("Snapshot files did not pass validation. Restore was not applied.")
        pre_restore = export_system_state(reason="pre_restore_backup")
        settings_before_restore = AppSetting.load()
        file_transaction = _RestoreFileTransaction(
            settings_before_restore,
            state,
            manifest,
        )
        path_map, root_map = file_transaction.prepare(zip_path, manifest)
        result = {
            "pre_restore_backup": pre_restore["path"],
            "restored_counts": manifest.get("record_counts", {}),
            "conference_name": manifest.get("conference_name") or "",
            "retained_recovery_paths": [],
        }
        try:
            with _restore_atomic():
                _clear_database_rows()
                _restore_models(state, path_map, root_map)
                file_transaction.activate()
                audit_success(
                    "system_state_restore_apply",
                    "System state restore applied.",
                    result_counts=result["restored_counts"],
                    file_changes={
                        "pre_restore_backup": str(pre_restore["path"]),
                    },
                    extra={"conference_name": result["conference_name"]},
                )
        except Exception as exc:
            rollback_errors = file_transaction.rollback()
            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise SystemStateError(
                    "System State restore failed and automatic file rollback was incomplete. "
                    f"Recovery copies were retained: {details}"
                ) from exc
            raise
        retained_recovery_paths = file_transaction.finalize()
        result["retained_recovery_paths"] = retained_recovery_paths
        shutil.rmtree(preview_dir, ignore_errors=True)
        if retained_recovery_paths:
            try:
                audit_success(
                    "system_state_restore_cleanup_warning",
                    "System State restore succeeded but recovery directories "
                    "could not be removed.",
                    file_changes={
                        "retained_recovery_paths": retained_recovery_paths,
                    },
                )
            except OSError:
                pass
        return result
    except Exception as exc:
        try:
            audit_failure(
                "system_state_restore_apply",
                exc,
                "System state restore failed.",
                extra={"token": token},
            )
        except OSError:
            pass
        raise


def _read_snapshot(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SystemStateError(
                    "System State ZIP contains duplicate entry names."
                )
            corrupt_entry = archive.testzip()
            if corrupt_entry:
                raise SystemStateError(
                    f"System State ZIP verification failed for {corrupt_entry}."
                )
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            state = json.loads(archive.read("state.json").decode("utf-8"))
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise SystemStateError(f"Invalid system state ZIP: {exc}") from exc
    archive_version = manifest.get("state_archive_version", manifest.get("snapshot_version"))
    if archive_version != STATE_ARCHIVE_VERSION:
        raise SystemStateError("Unsupported system state snapshot version.")
    _validate_snapshot_payload(manifest, state)
    _normalize_legacy_project_restore_paths(manifest)
    _validate_restore_path_membership(manifest)
    return manifest, state


def _normalize_legacy_project_restore_paths(manifest):
    mappings = []
    base_dir = Path(django_settings.BASE_DIR).resolve(strict=False)
    for entry in manifest.get("root_maps", []):
        if entry.get("restore_kind") != "project":
            continue
        restore_rel = str(entry.get("restore_rel", "")).strip().rstrip("/")
        target = _safe_target_below(base_dir, restore_rel)
        if target != base_dir and _is_relative_to(target, _project_data_root()):
            continue
        if (
            entry.get("label") not in DEFAULT_FOLDER_SETTINGS
            or target == base_dir
            or not _is_relative_to(target, base_dir)
        ):
            continue
        replacement = f"data/restored_external/{entry['label']}"
        mappings.append((restore_rel, replacement))
        entry["restore_rel"] = replacement

    for entry in manifest.get("files", []):
        if entry.get("restore_kind") != "project":
            continue
        restore_rel = str(entry.get("restore_rel", "")).strip().rstrip("/")
        for old_prefix, replacement in sorted(
            mappings,
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if restore_rel == old_prefix:
                entry["restore_rel"] = replacement
                break
            prefix = old_prefix + "/"
            if restore_rel.startswith(prefix):
                entry["restore_rel"] = (
                    replacement + "/" + restore_rel[len(prefix) :]
                )
                break


def _validate_snapshot_payload(manifest, state):
    archive_version = manifest.get(
        "state_archive_version",
        manifest.get("snapshot_version"),
    )
    state_version = state.get(
        "state_archive_version",
        state.get("snapshot_version"),
    )
    if state_version != archive_version:
        raise SystemStateError(
            "System State manifest and model payload versions do not match."
        )
    models_payload = state.get("models")
    if not isinstance(models_payload, dict):
        raise SystemStateError("System State model payload is missing.")
    required_keys = {key for key, _model in MODEL_SPECS}
    if not required_keys.issubset(models_payload):
        missing = ", ".join(sorted(required_keys - set(models_payload)))
        raise SystemStateError(
            f"System State model payload is incomplete: {missing}."
        )
    for key in required_keys:
        if not isinstance(models_payload[key], list):
            raise SystemStateError(
                f"System State model payload for {key} must be a list."
            )
    _validate_model_payload_types(models_payload)
    _validate_model_payload_fields(models_payload)
    if len(models_payload["settings"]) != 1:
        raise SystemStateError(
            "System State must contain exactly one Settings record."
        )

    expected_counts = manifest.get("record_counts")
    actual_counts = {
        key: len(models_payload[key])
        for key in required_keys
    }
    try:
        counts_match = isinstance(expected_counts, dict) and all(
            int(expected_counts.get(key, -1)) == count
            for key, count in actual_counts.items()
        )
    except (TypeError, ValueError):
        counts_match = False
    if not counts_match:
        raise SystemStateError(
            "System State record counts do not match the model payload."
        )

    manifest_signature = manifest.get("database_signature")
    state_signature = state.get("database_signature")
    if not manifest_signature or manifest_signature != state_signature:
        raise SystemStateError(
            "System State database signatures do not match."
        )

    manifest_payload_hash = manifest.get("model_payload_sha256")
    state_payload_hash = state.get("model_payload_sha256")
    if manifest_payload_hash or state_payload_hash:
        actual_payload_hash = _model_payload_sha256(models_payload)
        if (
            manifest_payload_hash != state_payload_hash
            or manifest_payload_hash != actual_payload_hash
        ):
            raise SystemStateError(
                "System State model payload hash does not match."
            )

    files = manifest.get("files")
    if not isinstance(files, list) or manifest.get("file_count") != len(files):
        raise SystemStateError(
            "System State file count does not match its manifest."
        )
    file_ids = [entry.get("file_id") for entry in files if isinstance(entry, dict)]
    zip_paths = [entry.get("zip_path") for entry in files if isinstance(entry, dict)]
    if (
        len(file_ids) != len(files)
        or not all(file_ids)
        or len(file_ids) != len(set(file_ids))
        or not all(zip_paths)
        or len(zip_paths) != len(set(zip_paths))
    ):
        raise SystemStateError(
            "System State manifest contains missing or duplicate file entries."
        )

    root_maps = manifest.get("root_maps")
    if not isinstance(root_maps, list):
        raise SystemStateError("System State root mapping is missing.")
    root_ids = [
        entry.get("root_id")
        for entry in root_maps
        if isinstance(entry, dict)
    ]
    if (
        len(root_ids) != len(root_maps)
        or not all(root_ids)
        or len(root_ids) != len(set(root_ids))
    ):
        raise SystemStateError(
            "System State root mapping contains missing or duplicate identifiers."
        )


def _validate_model_payload_types(models_payload):
    for key, model in MODEL_SPECS:
        seen_primary_keys = set()
        field_map = {
            field.attname: field
            for field in model._meta.concrete_fields
        }
        for row in models_payload[key]:
            if not isinstance(row, dict):
                raise SystemStateError(
                    f"System State model payload for {key} contains a non-object row."
                )
            primary_key = row.get(model._meta.pk.attname)
            if (
                primary_key in (None, "")
                or primary_key in seen_primary_keys
            ):
                raise SystemStateError(
                    f"System State model payload for {key} contains a missing "
                    "or duplicate primary key."
                )
            seen_primary_keys.add(primary_key)
            for field_name, value in row.items():
                field = field_map.get(field_name)
                if field is None or value in (None, ""):
                    continue
                if isinstance(field, models.BooleanField):
                    if not isinstance(value, bool):
                        raise SystemStateError(
                            f"System State field {key}.{field_name} must be boolean."
                        )
                    continue
                if isinstance(
                    field,
                    (
                        models.IntegerField,
                        models.PositiveIntegerField,
                        models.BigAutoField,
                        models.AutoField,
                    ),
                ):
                    if isinstance(value, bool) or not (
                        isinstance(value, int)
                        or (
                            isinstance(value, str)
                            and re.fullmatch(r"-?\d+", value)
                        )
                    ):
                        raise SystemStateError(
                            f"System State field {key}.{field_name} must be an integer."
                        )
                    continue
                if isinstance(field, models.DecimalField):
                    try:
                        decimal_value = Decimal(str(value))
                    except Exception as exc:
                        raise SystemStateError(
                            f"System State field {key}.{field_name} must be decimal."
                        ) from exc
                    if not decimal_value.is_finite():
                        raise SystemStateError(
                            f"System State field {key}.{field_name} must be a "
                            "finite decimal."
                        )
                    continue
                if (
                    isinstance(field, models.DateTimeField)
                    and parse_datetime(str(value)) is None
                ):
                    raise SystemStateError(
                        f"System State field {key}.{field_name} has an invalid datetime."
                    )


def _validation_is_legacy_choice_only(errors):
    return bool(errors) and all(
        error.code == "invalid_choice"
        for error in errors
    )


def _validate_model_payload_fields(models_payload):
    for key, model in MODEL_SPECS:
        unique_values = {
            field.attname: set()
            for field in model._meta.concrete_fields
            if field.unique
        }
        normalized_paper_ids = set()
        for raw_row in models_payload[key]:
            obj = model()
            for field in model._meta.concrete_fields:
                payload_key = field.attname
                legacy_key = field.name
                if payload_key not in raw_row and legacy_key not in raw_row:
                    continue
                value = raw_row.get(payload_key, raw_row.get(legacy_key))
                value = _deserialize_value(field, value, {}, [])
                try:
                    if field.is_relation:
                        # Related rows are restored later in dependency order;
                        # preview validation must not query the current database
                        # for snapshot foreign keys.
                        cleaned = field.to_python(value)
                        field.run_validators(cleaned)
                    else:
                        cleaned = field.clean(value, obj)
                except ValidationError as exc:
                    if _validation_is_legacy_choice_only(exc.error_list):
                        cleaned = value
                    else:
                        raise SystemStateError(
                            f"System State model payload for {key} failed "
                            f"validation in: {field.name}."
                        ) from exc
                setattr(obj, field.attname, cleaned)
                if field.unique and cleaned not in (None, ""):
                    seen = unique_values[field.attname]
                    if cleaned in seen:
                        raise SystemStateError(
                            f"System State model payload for {key} contains "
                            f"duplicate {field.name} values."
                        )
                    seen.add(cleaned)
            if model is InitialPaper:
                paper_id = str(getattr(obj, "paper_id", ""))
                normalized = paper_id.strip().casefold()
                if not normalized or paper_id != paper_id.strip():
                    raise SystemStateError(
                        "System State Paper Master IDs must be non-empty and "
                        "must not contain surrounding whitespace."
                    )
                if normalized in normalized_paper_ids:
                    raise SystemStateError(
                        "System State Paper Master IDs must be unique after "
                        "case and whitespace normalization."
                    )
                normalized_paper_ids.add(normalized)


def _validate_restore_path_membership(manifest):
    roots_by_kind = {"media": [], "project": []}
    for entry in manifest.get("root_maps", []):
        kind = entry.get("restore_kind")
        if kind not in roots_by_kind:
            raise SystemStateError(
                f"Unsupported snapshot restore kind: {kind}"
            )
        roots_by_kind[kind].append(_safe_restore_root_target(entry))

    for entry in manifest.get("files", []):
        kind = entry.get("restore_kind")
        if kind not in roots_by_kind:
            raise SystemStateError(
                f"Unsupported snapshot restore kind: {kind}"
            )
        target = _safe_restore_target(entry)
        if not any(
            target != root and _is_relative_to(target, root)
            for root in roots_by_kind[kind]
        ):
            raise SystemStateError(
                "Snapshot file restore target is not covered by a declared "
                f"managed root: {target}"
            )


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


def _restore_managed_folders(settings_obj, state, manifest):
    media_root = _validate_media_root()
    data_root = _project_data_root()
    resolved = {
        media_root,
        _validate_project_restore_root(data_root / "logs"),
        _validate_project_restore_root(data_root / "crosscheck_upload"),
        _validate_project_restore_root(data_root / "restored_external"),
    }

    for field_name in DEFAULT_FOLDER_SETTINGS:
        current = _resolved_path(getattr(settings_obj, field_name))
        if _is_relative_to(current, data_root):
            resolved.add(_validate_project_restore_root(current))

    for field_name, raw_value in _snapshot_folder_values(state):
        raw_target = _resolved_path(raw_value)
        base_dir = Path(django_settings.BASE_DIR).resolve(strict=False)
        if (
            raw_target in {base_dir, data_root}
            or _is_relative_to(base_dir, raw_target)
        ):
            raise SystemStateError(
                "Snapshot folder settings may restore only into application-owned "
                f"data folders: {raw_target}"
            )
        target = _resolved_path(
            _restore_folder_setting(field_name, raw_value)
        )
        if _is_relative_to(target, data_root):
            resolved.add(_validate_project_restore_root(target))
        elif target != media_root:
            raise SystemStateError(
                "Snapshot folder settings may restore only into application-owned "
                f"data folders: {target}"
            )

    for root_entry in manifest.get("root_maps", []):
        target = _safe_restore_root_target(root_entry)
        if target == media_root:
            resolved.add(target)
        else:
            resolved.add(_validate_project_restore_root(target))

    selected = []
    for folder in sorted(resolved, key=lambda path: (len(path.parts), str(path))):
        if any(_is_relative_to(folder, parent) for parent in selected):
            continue
        selected.append(folder)
    return selected


def _rename_restore_path(source, target):
    source.replace(target)


def _remove_restore_path(path):
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


class _RestoreFileTransaction:
    def __init__(self, settings_obj, state, manifest):
        self.token = uuid.uuid4().hex
        self.areas = []
        self.activated = False
        self.finished = False
        for index, root in enumerate(
            _restore_managed_folders(settings_obj, state, manifest)
        ):
            self.areas.append(
                {
                    "root": root,
                    "staging": root.parent
                    / f".cfm-restore-staging-{self.token}-{index}-{root.name}",
                    "quarantine": root.parent
                    / f".cfm-restore-backup-{self.token}-{index}-{root.name}",
                    "promoted": [],
                }
            )

    def prepare(self, zip_path, manifest):
        path_map = {}
        root_map = []
        seen_targets = set()
        seen_file_ids = set()
        try:
            for area in self.areas:
                root = area["root"]
                if root.exists() and not root.is_dir():
                    raise SystemStateError(f"Managed restore path is not a directory: {root}")
                root.parent.mkdir(parents=True, exist_ok=True)
                area["staging"].mkdir(parents=False, exist_ok=False)
                area["quarantine"].mkdir(parents=False, exist_ok=False)

            with zipfile.ZipFile(zip_path) as archive:
                for entry in manifest.get("files", []):
                    file_id = entry.get("file_id")
                    if not file_id or file_id in seen_file_ids:
                        raise SystemStateError("Snapshot contains a missing or duplicate file identifier.")
                    seen_file_ids.add(file_id)
                    target = _safe_restore_target(entry)
                    if target in seen_targets:
                        raise SystemStateError(f"Snapshot contains duplicate restore target: {target}")
                    seen_targets.add(target)
                    area = self._area_for_target(target)
                    relative_target = target.relative_to(area["root"])
                    if (
                        relative_target.parts
                        and relative_target.parts[0] in RESTORE_PRESERVED_CHILDREN
                    ):
                        raise SystemStateError(
                            f"Snapshot attempts to overwrite protected restore data: {target}"
                        )
                    staged_target = area["staging"] / relative_target
                    staged_target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry["zip_path"]) as source, staged_target.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                    if _sha256(staged_target) != entry.get("sha256"):
                        raise SystemStateError(
                            f"Restored file hash mismatch: {entry['zip_path']}"
                        )
                    path_map[f"snapshot-file:{file_id}"] = str(target)

            for entry in manifest.get("root_maps", []):
                restored_root = _safe_restore_root_target(entry)
                self._area_for_target(restored_root, allow_root=True)
                root_map.append((f"snapshot-root:{entry['root_id']}", str(restored_root)))
            return path_map, root_map
        except Exception as exc:
            rollback_errors = self.rollback()
            if rollback_errors:
                raise SystemStateError(
                    "System State restore staging failed and temporary recovery data "
                    f"could not be cleaned up: {'; '.join(rollback_errors)}"
                ) from exc
            raise

    def activate(self):
        for area in self.areas:
            root = area["root"]
            root.mkdir(parents=True, exist_ok=True)
            for child in list(root.iterdir()):
                if child.name in RESTORE_PRESERVED_CHILDREN:
                    continue
                _rename_restore_path(child, area["quarantine"] / child.name)
            for child in list(area["staging"].iterdir()):
                if child.name in RESTORE_PRESERVED_CHILDREN:
                    raise SystemStateError(
                        f"Snapshot attempts to overwrite protected restore data: {child.name}"
                    )
                target = root / child.name
                _rename_restore_path(child, target)
                area["promoted"].append(target)
        self.activated = True

    def rollback(self):
        if self.finished:
            return []
        errors = []
        for area in reversed(self.areas):
            root = area["root"]
            try:
                for promoted in reversed(area["promoted"]):
                    _remove_restore_path(promoted)
                area["promoted"].clear()
                root.mkdir(parents=True, exist_ok=True)
                quarantine = area["quarantine"]
                if quarantine.exists():
                    for child in list(quarantine.iterdir()):
                        target = root / child.name
                        if target.exists():
                            _remove_restore_path(target)
                        _rename_restore_path(child, target)
            except Exception as exc:
                errors.append(f"{area['quarantine']}: {exc.__class__.__name__}: {exc}")
        if errors:
            return errors
        for area in self.areas:
            for path in (area["staging"], area["quarantine"]):
                try:
                    if path.exists():
                        shutil.rmtree(path)
                except OSError as exc:
                    errors.append(f"{path}: {exc.__class__.__name__}: {exc}")
        if not errors:
            self.finished = True
        return errors

    def finalize(self):
        retained = []
        for area in self.areas:
            for path in (area["staging"], area["quarantine"]):
                try:
                    if path.exists():
                        shutil.rmtree(path)
                except OSError:
                    retained.append(str(path))
        self.finished = True
        return retained

    def _area_for_target(self, target, allow_root=False):
        matches = [
            area
            for area in self.areas
            if target == area["root"] or _is_relative_to(target, area["root"])
        ]
        if not matches or (target in {area["root"] for area in matches} and not allow_root):
            raise SystemStateError(f"Snapshot restore target is outside managed folders: {target}")
        return max(matches, key=lambda area: len(area["root"].parts))


def _snapshot_folder_values(state):
    rows = state.get("models", {}).get("settings", [])
    if not rows:
        return []
    return [
        (field_name, rows[0].get(field_name, ""))
        for field_name in DEFAULT_FOLDER_SETTINGS
        if rows[0].get(field_name)
    ]


def _portable_folder_setting(field_name, value):
    if not value:
        return DEFAULT_FOLDER_SETTINGS[field_name]
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        return str(value)
    resolved = path.resolve(strict=False)
    data_root = _project_data_root()
    if resolved != data_root and _is_relative_to(resolved, data_root):
        return resolved.relative_to(django_settings.BASE_DIR).as_posix()
    return f"data/restored_external/{field_name}"


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
        "audit_logs": 0,
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
        elif "/logs/" in zip_path:
            counts["audit_logs"] += 1
        elif (
            "/final_submissions/" in zip_path
            or "/source_submissions/" in zip_path
            or "/formatted_pdfs/" in zip_path
            or "/formatted_sources/" in zip_path
            or "/publication_pdf_debug/" in zip_path
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


def _safe_target_below(root, relative_value):
    root = Path(root).expanduser().resolve(strict=False)
    relative = Path(str(relative_value))
    if relative.is_absolute():
        raise SystemStateError("Snapshot contains an absolute restore path.")
    target = (root / relative).resolve(strict=False)
    if target != root and not _is_relative_to(target, root):
        raise SystemStateError("Snapshot restore path escapes its managed root.")
    return target


def _safe_restore_target(entry):
    restore_kind = entry.get("restore_kind")
    if restore_kind == "media":
        return _safe_target_below(
            _validate_media_root(),
            entry.get("restore_rel", ""),
        )
    if restore_kind == "project":
        target = _safe_target_below(
            django_settings.BASE_DIR,
            entry.get("restore_rel", ""),
        )
        _validate_project_restore_root(target)
        return target
    raise SystemStateError(f"Unsupported snapshot restore kind: {restore_kind}")


def _safe_restore_root_target(entry):
    target = _safe_restore_target(entry)
    if entry.get("restore_kind") == "project":
        return _validate_project_restore_root(target)
    if target != _validate_media_root():
        raise SystemStateError(
            f"Media restore root must match MEDIA_ROOT exactly: {target}"
        )
    return target


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
            except ValidationError as exc:
                invalid_errors = []
                for field_name, errors in exc.error_dict.items():
                    # Older archives may contain free-text status values that
                    # predate current choices. Preserve only that explicit
                    # compatibility case; validators and constraints still
                    # fail closed.
                    if _validation_is_legacy_choice_only(errors):
                        continue
                    invalid_errors.append(field_name)
                if invalid_errors:
                    raise SystemStateError(
                        f"System State model payload for {key} failed validation "
                        f"in: {', '.join(sorted(invalid_errors))}."
                    ) from exc
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
        legacy_external = f"data/restored_external_folders/{field_name}"
        if value.strip().rstrip("/") == legacy_external:
            return f"data/restored_external/{field_name}"
        if _is_temp_path(value):
            return f"data/restored_external/{field_name}"
        path = Path(value).expanduser()
        if path.is_absolute():
            try:
                relative = path.resolve().relative_to(
                    django_settings.BASE_DIR
                ).as_posix()
            except ValueError:
                return f"data/restored_external/{field_name}"
            if relative == "data" or not relative.startswith("data/"):
                return f"data/restored_external/{field_name}"
            return relative
        normalized = path.as_posix().strip("/")
        if normalized == "data" or not normalized.startswith("data/"):
            return f"data/restored_external/{field_name}"
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
