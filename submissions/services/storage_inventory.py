import csv
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.db import connection
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.file_manager import (
    copy_pdf_to_folder,
    publication_pdf_filename,
    publication_pdf_info,
    resolve_folder,
    sanitize_filename_part,
    source_pdf_path,
    title_short_name,
)
from submissions.services.audit import audit_failure, audit_preview, audit_success
from submissions.services.final_submission_state import bulk_update_submissions


CLEANUP_CONFIRMATION_TEXT = "CLEAN STORAGE"
CLEANUP_PREVIEW_TTL_SECONDS = 2 * 60 * 60
GENERATED_CACHE_DIRS = {
    "pdf_thumbnails": "Generated PDF thumbnails",
    "format_previews": "Generated format previews",
    "title_author_verification": "Generated title/author verification images",
}
REPORT_EXPORT_EXTENSIONS = {".xlsx", ".zip"}
POLICY_LABELS = {
    "generated_cache_or_orphan_output": "Conservative cleanup",
    "generated_reports_exports": "Generated reports/exports cleanup",
}
STORAGE_CATEGORY_PROTECTION_PRIORITY = {
    "generated_cache": 0,
    "managed_output": 1,
    "reports_backups": 2,
    "canonical_original": 3,
    "corrected": 3,
}


@dataclass(frozen=True)
class StoragePathRef:
    path: Path
    category: str
    role: str
    protected: bool = True
    scope: str = "exact"


@dataclass(frozen=True)
class StorageReferenceIndex:
    exact_path_keys: frozenset
    tree_path_keys: frozenset
    exact_file_identities: frozenset
    tree_directory_identities: frozenset
    missing_references: tuple

    def is_referenced(self, path):
        return self.contains_canonical(_canonical_path(path))

    def contains_canonical(self, path):
        key = _path_key(path)
        if key in self.exact_path_keys:
            return True
        if any(
            _path_key(parent) in self.tree_path_keys
            for parent in (path, *path.parents)
        ):
            return True
        identity = _existing_identity(path)
        if identity and identity in self.exact_file_identities:
            return True
        return any(
            parent_identity in self.tree_directory_identities
            for parent_identity in (
                _existing_identity(parent)
                for parent in (path, *path.parents)
            )
            if parent_identity
        )


@dataclass(frozen=True)
class StorageFileRecord:
    path: Path
    path_key: str
    category: str
    size: int
    signature: tuple


def cleanup_preview_root(*, create=True):
    root = django_settings.BASE_DIR / "data" / "storage_cleanup_previews"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _purge_expired_cleanup_previews(now=None):
    now = now or timezone.now()
    for path in cleanup_preview_root().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expires_at = parse_datetime(
                str(payload.get("expires_at", ""))
            )
            if expires_at is not None and timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(
                    expires_at,
                    timezone.get_current_timezone(),
                )
            if expires_at is not None and expires_at >= now:
                continue
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        try:
            path.unlink()
        except OSError:
            pass


def _format_size(size):
    value = float(size or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _path_size(path):
    return path.stat().st_size if path.exists() and path.is_file() else 0


def _canonical_path(path):
    return Path(path).expanduser().resolve(strict=False)


def _path_key(path):
    return os.path.normcase(os.fspath(path))


def _stat_signature(stat):
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _current_file_signature(path):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return _stat_signature(stat)


def _database_change_token():
    if connection.vendor != "sqlite":
        return None
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA data_version")
        return cursor.fetchone()[0]


def _existing_identity(path):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _filefield_path(field_file):
    if not field_file:
        return None
    try:
        return Path(field_file.path)
    except ValueError:
        return None


def _text_path(value):
    if not value:
        return None
    return Path(value)


def _configured_folder(path_value):
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = django_settings.BASE_DIR / path
    return path


def _relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _category_row(category, label, size=0, count=0):
    return {
        "category": category,
        "label": label,
        "size": size,
        "size_label": _format_size(size),
        "count": count,
    }


def _managed_roots(settings_obj):
    return {
        "canonical_original": [
            Path(django_settings.MEDIA_ROOT) / "final_submissions",
            Path(django_settings.MEDIA_ROOT) / "source_submissions",
        ],
        "corrected": [
            Path(django_settings.MEDIA_ROOT) / "formatted_pdfs",
            Path(django_settings.MEDIA_ROOT) / "formatted_sources",
        ],
        "generated_cache": [
            Path(django_settings.MEDIA_ROOT) / key for key in GENERATED_CACHE_DIRS
        ],
        "managed_output": [
            _configured_folder(getattr(settings_obj, "publication_pdf_debug_folder", "data/publication_pdf_debug")),
            _configured_folder(settings_obj.active_final_folder),
            _configured_folder(settings_obj.old_versions_folder),
            django_settings.BASE_DIR / "data" / "crosscheck_upload",
            django_settings.BASE_DIR / "data" / "plagiarism_upload",
        ],
        "reports_backups": [
            _configured_folder(settings_obj.reports_folder),
            _configured_folder(settings_obj.plagiarism_reports_folder),
            django_settings.BASE_DIR / "data" / "system_state_backups",
        ],
    }


def _cleanup_managed_output_roots(settings_obj):
    return []


def _cleanup_report_export_roots(settings_obj):
    return [
        _configured_folder(settings_obj.reports_folder),
        django_settings.BASE_DIR / "data" / "crosscheck_upload",
        django_settings.BASE_DIR / "data" / "plagiarism_upload",
    ]


def _report_cleanup_protected_roots(settings_obj):
    return [
        Path(django_settings.MEDIA_ROOT),
        _configured_folder(settings_obj.plagiarism_reports_folder),
        _configured_folder(settings_obj.extraction_results_folder),
        django_settings.BASE_DIR / "data" / "import_previews",
        django_settings.BASE_DIR / "data" / "storage_cleanup_previews",
        django_settings.BASE_DIR / "data" / "system_state_backups",
        django_settings.BASE_DIR / "data" / "system_state_restore_previews",
        django_settings.BASE_DIR / "data" / "crosscheck_provenance",
        django_settings.BASE_DIR / "data" / "restored_external",
        django_settings.BASE_DIR / "data" / "restored_external_folders",
    ]


def _path_is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _application_data_clear_candidates(
    settings_obj,
    default_folder_values,
):
    folders = {
        Path(django_settings.MEDIA_ROOT),
        Path("data") / "media",
        Path("data") / "crosscheck_upload",
        Path("data") / "publication_pdf_debug",
        Path("data") / "import_previews",
        Path("data") / "storage_cleanup_previews",
        Path("data") / "system_state_backups",
        Path("data") / "system_state_restore_previews",
        Path("data") / "restored_external",
        Path("data") / "restored_external_folders",
        resolve_folder(settings_obj.incoming_folder),
        resolve_folder(settings_obj.active_final_folder),
        resolve_folder(settings_obj.publication_pdf_debug_folder),
        resolve_folder(settings_obj.old_versions_folder),
        resolve_folder(settings_obj.reports_folder),
        resolve_folder(settings_obj.extraction_results_folder),
        resolve_folder(settings_obj.plagiarism_reports_folder),
    }
    folders.update(Path(value) for value in default_folder_values)
    return {
        (
            folder
            if folder.is_absolute()
            else django_settings.BASE_DIR / folder
        ).expanduser().resolve(strict=False)
        for folder in folders
    }


def stage_application_data_clear(
    settings_obj,
    default_folder_values,
):
    base_data_root = (
        django_settings.BASE_DIR / "data"
    ).resolve(strict=False)
    media_root = Path(django_settings.MEDIA_ROOT).resolve(strict=False)
    base_dir = Path(django_settings.BASE_DIR).resolve(strict=False)
    audit_root = (base_data_root / "logs").resolve(strict=False)
    filesystem_root = Path(base_dir.anchor)
    if (
        media_root in {filesystem_root, base_dir, base_data_root}
        or _path_is_within(base_dir, media_root)
    ):
        raise ValueError(
            f"MEDIA_ROOT is too broad for Clear Database: {media_root}"
        )
    trusted_roots = (base_data_root, media_root)
    candidates = _application_data_clear_candidates(
        settings_obj,
        default_folder_values,
    )
    unsafe = sorted(
        {
            folder
            for folder in candidates
            if folder in {filesystem_root, base_dir, base_data_root}
            or _path_is_within(base_dir, folder)
            or _path_is_within(folder, audit_root)
            or _path_is_within(audit_root, folder)
        },
        key=str,
    )
    if unsafe:
        raise ValueError(
            "Clear Database blocked because a configured managed folder "
            "overlaps the project, data, or audit root: "
            + ", ".join(str(path) for path in unsafe)
        )
    trusted = sorted(
        {
            folder
            for folder in candidates
            if any(
                _path_is_within(folder, root)
                for root in trusted_roots
            )
        },
        key=lambda path: (len(path.parts), str(path)),
    )
    selected = []
    for folder in trusted:
        if any(
            _path_is_within(folder, parent)
            for parent in selected
        ):
            continue
        selected.append(folder)

    skipped_external = sorted(
        str(folder)
        for folder in candidates
        if folder.exists()
        and not any(
            _path_is_within(folder, root)
            for root in trusted_roots
        )
    )
    token = uuid.uuid4().hex
    staged = []
    try:
        for index, folder in enumerate(selected):
            if not folder.exists() or not folder.is_dir():
                continue
            quarantine = (
                folder.parent
                / f".cfm-clear-{token}-{index}-{folder.name}"
            )
            quarantine.mkdir(parents=False, exist_ok=False)
            row = {
                "folder": folder,
                "quarantine": quarantine,
                "removed": 0,
            }
            staged.append(row)
            for child in list(folder.iterdir()):
                child.replace(quarantine / child.name)
                row["removed"] += 1
    except Exception:
        restore_staged_application_data(staged)
        raise
    return {
        "staged": staged,
        "skipped_external": skipped_external,
    }


def restore_staged_application_data(staged):
    for row in reversed(staged):
        folder = row["folder"]
        quarantine = row["quarantine"]
        folder.mkdir(parents=True, exist_ok=True)
        if quarantine.exists():
            for child in list(quarantine.iterdir()):
                child.replace(folder / child.name)
            quarantine.rmdir()


def discard_staged_application_data(staged):
    retained = []
    for row in staged:
        quarantine = row["quarantine"]
        try:
            shutil.rmtree(quarantine)
        except OSError as exc:
            retained.append(
                {
                    "path": str(quarantine),
                    "error": exc.__class__.__name__,
                }
            )
    return retained


def _append_scan_error(scan_errors, root, path, exc):
    if scan_errors is None:
        return
    scan_errors.append(
        {
            "root": str(_canonical_path(root)),
            "path": str(_canonical_path(path)),
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
    )


def _iter_file_records(root, category, scan_errors=None):
    root = Path(root)
    try:
        if root.is_file():
            stat = root.stat()
            canonical = _canonical_path(root)
            yield StorageFileRecord(
                path=canonical,
                path_key=_path_key(canonical),
                category=category,
                size=stat.st_size,
                signature=_stat_signature(stat),
            )
            return
        if not root.exists():
            return
    except FileNotFoundError:
        return
    except OSError as exc:
        _append_scan_error(scan_errors, root, root, exc)
        return

    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _append_scan_error(scan_errors, root, current, exc)
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=True):
                        continue
                    stat = entry.stat(follow_symlinks=True)
                    canonical = _canonical_path(entry.path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    _append_scan_error(
                        scan_errors,
                        root,
                        entry.path,
                        exc,
                    )
                    continue
                yield StorageFileRecord(
                    path=canonical,
                    path_key=_path_key(canonical),
                    category=category,
                    size=stat.st_size,
                    signature=_stat_signature(stat),
                )


def _collect_referenced_paths():
    refs = {}
    missing = []
    submissions = FinalSubmission.objects.only(
        "final_submission_id",
        "paper_id_filled",
        "pdf_file",
        "source_file",
        "formatted_pdf_file",
        "formatted_source_file",
        "current_file_path",
        "source_current_file_path",
        "thumbnail_folder",
        "title_author_verification_image",
        "plagiarism_report_path",
    )
    for submission in submissions:
        submission_label = (
            f"Final {submission.final_submission_id} / "
            f"{submission.paper_id_filled or 'No Paper ID'}"
        )
        candidates = [
            ("canonical_original", "Original PDF", _filefield_path(submission.pdf_file), "exact"),
            ("canonical_original", "Original source", _filefield_path(submission.source_file), "exact"),
            ("corrected", "Corrected PDF", _filefield_path(submission.formatted_pdf_file), "exact"),
            ("corrected", "Corrected source", _filefield_path(submission.formatted_source_file), "exact"),
            ("managed_output", "Legacy processed PDF path", _text_path(submission.current_file_path), "exact"),
            ("managed_output", "Legacy current source path", _text_path(submission.source_current_file_path), "exact"),
            ("generated_cache", "PDF thumbnails", _text_path(submission.thumbnail_folder), "tree"),
            (
                "generated_cache",
                "Title/author verification image",
                _text_path(submission.title_author_verification_image),
                "exact",
            ),
            ("reports_backups", "Plagiarism report", _text_path(submission.plagiarism_report_path), "exact"),
        ]
        for category, role, path, scope in candidates:
            if not path:
                continue
            canonical = _canonical_path(path)
            key = _path_key(canonical)
            existing = refs.get(key)
            effective_scope = (
                "tree"
                if scope == "tree"
                or (existing and existing.scope == "tree")
                else "exact"
            )
            refs[key] = StoragePathRef(
                canonical,
                category,
                role,
                scope=effective_scope,
            )
            if not path.exists():
                missing.append(
                    {
                        "submission_id": submission.pk,
                        "submission_label": submission_label,
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "role": role,
                        "category": category,
                        "path": str(path),
                    }
                )
    return refs, missing


def build_storage_reference_index():
    refs, missing_references = _collect_referenced_paths()
    exact_path_keys = frozenset(
        key for key, ref in refs.items() if ref.scope == "exact"
    )
    tree_path_keys = frozenset(
        key for key, ref in refs.items() if ref.scope == "tree"
    )
    exact_file_identities = frozenset(
        identity
        for identity in (
            _existing_identity(ref.path)
            for ref in refs.values()
            if ref.scope == "exact"
        )
        if identity
    )
    tree_directory_identities = frozenset(
        identity
        for identity in (
            _existing_identity(ref.path)
            for ref in refs.values()
            if ref.scope == "tree"
        )
        if identity
    )
    return StorageReferenceIndex(
        exact_path_keys=exact_path_keys,
        tree_path_keys=tree_path_keys,
        exact_file_identities=exact_file_identities,
        tree_directory_identities=tree_directory_identities,
        missing_references=tuple(missing_references),
    )


class StorageInventoryBuilder:
    CATEGORY_LABELS = {
        "canonical_original": "Canonical originals",
        "corrected": "Corrected files",
        "generated_cache": "Generated cache",
        "managed_output": "Managed outputs",
        "reports_backups": "Reports and backups",
    }

    def __init__(self, settings_obj=None, reference_index=None):
        self.settings_obj = settings_obj or AppSetting.read()
        self.reference_index = (
            reference_index or build_storage_reference_index()
        )

    def build(self):
        roots = _managed_roots(self.settings_obj)
        managed_output_cleanup_roots = tuple(
            _canonical_path(root)
            for root in _cleanup_managed_output_roots(self.settings_obj)
        )
        all_files = {}
        scan_cache = {}
        scan_errors = []
        for category, root_list in roots.items():
            for root in root_list:
                root_key = _path_key(_canonical_path(root))
                scanned_records = scan_cache.get(root_key)
                if scanned_records is None:
                    scanned_records = tuple(
                        _iter_file_records(
                            root,
                            category,
                            scan_errors,
                        )
                        or ()
                    )
                    scan_cache[root_key] = scanned_records
                for scanned_record in scanned_records:
                    record = (
                        scanned_record
                        if scanned_record.category == category
                        else replace(scanned_record, category=category)
                    )
                    existing = all_files.get(record.path_key)
                    if (
                        existing is None
                        or STORAGE_CATEGORY_PROTECTION_PRIORITY.get(
                            record.category,
                            1,
                        )
                        > STORAGE_CATEGORY_PROTECTION_PRIORITY.get(
                            existing.category,
                            1,
                        )
                    ):
                        all_files[record.path_key] = record

        categories = [
            _category_row(
                category,
                self.CATEGORY_LABELS.get(
                    category,
                    category.replace("_", " ").title(),
                ),
                sum(
                    record.size
                    for record in all_files.values()
                    if record.category == category
                ),
                sum(
                    1
                    for record in all_files.values()
                    if record.category == category
                ),
            )
            for category in roots
        ]

        orphaned = []
        cleanup_candidates = []
        for record in sorted(
            all_files.values(),
            key=lambda item: item.path_key,
        ):
            referenced = self.reference_index.contains_canonical(record.path)
            if not referenced:
                orphaned.append(
                    _inventory_file_row(
                        record,
                        "Not referenced by database records.",
                    )
                )
            if record.category == "generated_cache" and not referenced:
                cleanup_candidates.append(
                    _inventory_file_row(
                        record,
                        "Generated cache can be regenerated.",
                    )
                )
            elif (
                record.category == "managed_output"
                and not referenced
                and any(
                    _relative_to(record.path, root)
                    for root in managed_output_cleanup_roots
                )
            ):
                cleanup_candidates.append(
                    _inventory_file_row(
                        record,
                        (
                            "Orphaned active/old publication output is not "
                            "referenced by database records."
                        ),
                    )
                )

        large_files = sorted(
            [
                {
                    "path": str(record.path),
                    "category": record.category,
                    "size": record.size,
                    "size_label": _format_size(record.size),
                }
                for record in all_files.values()
            ],
            key=lambda row: row["size"],
            reverse=True,
        )[:20]
        unique_total_size = sum(
            record.size for record in all_files.values()
        )
        return {
            "categories": categories,
            "missing_references": list(
                self.reference_index.missing_references
            ),
            "orphaned_files": orphaned,
            "cleanup_candidates": cleanup_candidates,
            "report_export_cleanup_candidates": (
                _report_export_cleanup_candidates(
                    self.settings_obj,
                    all_files.values(),
                )
            ),
            "large_files": large_files,
            "scan_errors": scan_errors,
            "total_size": unique_total_size,
            "total_size_label": _format_size(unique_total_size),
            "total_file_count": len(all_files),
        }


def _inventory_file_row(record, reason):
    return {
        "path": str(record.path),
        "category": record.category,
        "size": record.size,
        "size_label": _format_size(record.size),
        "signature": list(record.signature),
        "reason": reason,
    }


def build_storage_inventory():
    return StorageInventoryBuilder().build()


def _report_export_cleanup_candidates(settings_obj, records):
    candidates = []
    reports_root = _canonical_path(
        _configured_folder(settings_obj.reports_folder)
    )
    external_upload_roots = tuple(
        _canonical_path(root)
        for root in _cleanup_report_export_roots(settings_obj)[1:]
    )
    protected_roots = tuple(
        _canonical_path(root)
        for root in _report_cleanup_protected_roots(settings_obj)
    )
    for record in sorted(records, key=lambda item: item.path_key):
        if any(_relative_to(record.path, root) for root in protected_roots):
            continue
        in_external_upload = any(
            _relative_to(record.path, root)
            for root in external_upload_roots
        )
        in_reports = _relative_to(record.path, reports_root)
        if not in_external_upload and not (
            in_reports
            and record.path.suffix.lower() in REPORT_EXPORT_EXTENSIONS
        ):
            continue
        candidates.append(
            {
                "path": str(record.path),
                "category": "report_export",
                "size": record.size,
                "size_label": _format_size(record.size),
                "signature": list(record.signature),
                "reason": (
                    "Generated report/export download can be regenerated."
                ),
            }
        )
    return candidates


def preview_storage_cleanup(policy="generated_cache_or_orphan_output"):
    _purge_expired_cleanup_previews()
    inventory = build_storage_inventory()
    if inventory["scan_errors"]:
        exc = ValueError(
            "Storage cleanup preview blocked because one or more managed "
            "folders could not be fully scanned."
        )
        audit_failure(
            "storage_cleanup_preview",
            exc,
            "Storage cleanup preview failed closed.",
            result_counts={
                "scan_errors": len(inventory["scan_errors"]),
            },
            extra={"scan_errors": inventory["scan_errors"][:20]},
        )
        raise exc
    if policy == "generated_reports_exports":
        candidates = inventory["report_export_cleanup_candidates"]
    else:
        policy = "generated_cache_or_orphan_output"
        candidates = inventory["cleanup_candidates"]
    payload = {
        "token": uuid.uuid4().hex,
        "policy": policy,
        "policy_label": POLICY_LABELS.get(policy, policy.replace("_", " ").title()),
        "created_at": timezone.now().isoformat(),
        "expires_at": (
            timezone.now() + timedelta(seconds=CLEANUP_PREVIEW_TTL_SECONDS)
        ).isoformat(),
        "files": candidates,
        "total_size": sum(row["size"] for row in candidates),
        "total_size_label": _format_size(sum(row["size"] for row in candidates)),
        "file_count": len(candidates),
    }
    path = cleanup_preview_root() / f"{payload['token']}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    audit_preview(
        "storage_cleanup_preview",
        f"{payload['policy_label']} preview created.",
        result_counts={
            "file_count": payload["file_count"],
            "total_size": payload["total_size"],
        },
        extra={"policy": payload["policy"], "token": payload["token"]},
    )
    return payload


def load_storage_cleanup_preview(token):
    token = str(token or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid cleanup preview token.")
    path = cleanup_preview_root(create=False) / f"{token}.json"
    if not path.exists():
        raise ValueError("Cleanup preview not found. Create a new preview.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("token") != token
            or not isinstance(payload.get("files"), list)
            or payload.get("policy") not in POLICY_LABELS
        ):
            raise ValueError
        expires_at = parse_datetime(payload.get("expires_at", ""))
    except (OSError, TypeError, json.JSONDecodeError, ValueError):
        raise ValueError(
            "Cleanup preview is invalid. Create a new preview."
        ) from None
    if expires_at is None:
        raise ValueError("Cleanup preview is invalid. Create a new preview.")
    if timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
    if timezone.now() > expires_at:
        raise ValueError("Cleanup preview expired. Create a new preview.")
    return payload


def _deletable_path(path, category=None):
    resolved = Path(path).resolve()
    allowed_roots = [
        (Path(django_settings.MEDIA_ROOT) / name).resolve()
        for name in GENERATED_CACHE_DIRS
    ]
    if category == "managed_output":
        settings_obj = AppSetting.load()
        allowed_roots.extend(
            root.resolve() for root in _cleanup_managed_output_roots(settings_obj)
        )
    if category == "report_export":
        settings_obj = AppSetting.load()
        if any(
            _relative_to(resolved, root)
            for root in _report_cleanup_protected_roots(settings_obj)
        ):
            return False
        reports_root = _configured_folder(settings_obj.reports_folder).resolve()
        if _relative_to(resolved, reports_root):
            return resolved.suffix.lower() in REPORT_EXPORT_EXTENSIONS
        for root in _cleanup_report_export_roots(settings_obj)[1:]:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def apply_storage_cleanup(token, confirmation):
    try:
        if confirmation != CLEANUP_CONFIRMATION_TEXT:
            raise ValueError(f'Type "{CLEANUP_CONFIRMATION_TEXT}" to confirm cleanup.')
        payload = load_storage_cleanup_preview(token)
        current_reference_index = build_storage_reference_index()
        current_inventory = StorageInventoryBuilder(
            reference_index=current_reference_index,
        ).build()
        if current_inventory["scan_errors"]:
            raise ValueError(
                "Storage cleanup blocked because one or more managed folders "
                "could not be fully scanned."
            )
        current_candidates = (
            current_inventory["report_export_cleanup_candidates"]
            if payload["policy"] == "generated_reports_exports"
            else current_inventory["cleanup_candidates"]
        )
        current_candidate_keys = {
            _path_key(_canonical_path(row["path"]))
            for row in current_candidates
        }
        database_change_token = _database_change_token()
        deleted = []
        skipped = []
        maintenance_warnings = []
        for index, row in enumerate(payload["files"]):
            latest_change_token = _database_change_token()
            if latest_change_token != database_change_token:
                current_reference_index = (
                    build_storage_reference_index()
                )
                current_inventory = StorageInventoryBuilder(
                    reference_index=current_reference_index,
                ).build()
                if current_inventory["scan_errors"]:
                    skipped.extend(
                        {
                            **remaining,
                            "message": (
                                "Storage scan became incomplete during cleanup. "
                                "The file was kept."
                            ),
                        }
                        for remaining in payload["files"][index:]
                    )
                    break
                current_candidates = (
                    current_inventory[
                        "report_export_cleanup_candidates"
                    ]
                    if payload["policy"]
                    == "generated_reports_exports"
                    else current_inventory["cleanup_candidates"]
                )
                current_candidate_keys = {
                    _path_key(_canonical_path(candidate["path"]))
                    for candidate in current_candidates
                }
                database_change_token = latest_change_token
            path = Path(row["path"])
            if current_reference_index.is_referenced(path):
                skipped.append(
                    {
                        **row,
                        "message": (
                            "Path is now referenced by a database record."
                        ),
                    }
                )
                continue
            if (
                _path_key(_canonical_path(path))
                not in current_candidate_keys
            ):
                skipped.append(
                    {
                        **row,
                        "message": (
                            "Path is no longer eligible under the current "
                            "cleanup policy or folder settings."
                        ),
                    }
                )
                continue
            if not _deletable_path(path, row.get("category")):
                skipped.append({**row, "message": "Path is outside cleanup-approved folders."})
                continue
            if not path.exists():
                skipped.append({**row, "message": "File no longer exists."})
                continue
            current_signature = _current_file_signature(path)
            expected_signature = tuple(row.get("signature") or ())
            if (
                current_signature is None
                or current_signature != expected_signature
            ):
                skipped.append(
                    {
                        **row,
                        "message": (
                            "File changed after preview. Create a new cleanup "
                            "preview before deleting it."
                        ),
                    }
                )
                continue
            size = current_signature[3]
            try:
                path.unlink()
            except OSError as exc:
                skipped.append(
                    {
                        **row,
                        "message": (
                            "File could not be deleted and was kept: "
                            f"{exc.__class__.__name__}."
                        ),
                    }
                )
                continue
            deleted.append({**row, "size": size, "size_label": _format_size(size)})
        preview_path = (
            cleanup_preview_root(create=False)
            / f"{payload['token']}.json"
        )
        try:
            preview_path.unlink(missing_ok=True)
        except OSError as exc:
            maintenance_warnings.append(
                {
                    "operation": "remove_cleanup_preview",
                    "error": exc.__class__.__name__,
                }
            )
        for operation, cleanup in (
            (
                "remove_empty_generated_cache_dirs",
                _remove_empty_generated_cache_dirs,
            ),
            (
                "remove_empty_managed_output_dirs",
                _remove_empty_managed_output_dirs,
            ),
        ):
            try:
                cleanup()
            except OSError as exc:
                maintenance_warnings.append(
                    {
                        "operation": operation,
                        "error": exc.__class__.__name__,
                    }
                )
        result = {
            "deleted": deleted,
            "skipped": skipped,
            "maintenance_warnings": maintenance_warnings,
            "deleted_count": len(deleted),
            "skipped_count": len(skipped),
            "maintenance_warning_count": len(maintenance_warnings),
            "deleted_size": sum(row["size"] for row in deleted),
            "deleted_size_label": _format_size(sum(row["size"] for row in deleted)),
        }
        audit_success(
            "storage_cleanup_apply",
            "Storage cleanup applied.",
            result_counts={
                "deleted_count": result["deleted_count"],
                "skipped_count": result["skipped_count"],
                "maintenance_warning_count": result[
                    "maintenance_warning_count"
                ],
                "deleted_size": result["deleted_size"],
            },
            file_changes={
                "deleted": deleted[:20],
                "skipped": skipped[:20],
                "maintenance_warnings": maintenance_warnings,
            },
        )
        return result
    except Exception as exc:
        audit_failure("storage_cleanup_apply", exc, "Storage cleanup failed.", extra={"token": token})
        raise


def _remove_empty_generated_cache_dirs():
    for root_name in GENERATED_CACHE_DIRS:
        root = Path(django_settings.MEDIA_ROOT) / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass


def _remove_empty_managed_output_dirs():
    settings_obj = AppSetting.load()
    for root in _cleanup_managed_output_roots(settings_obj):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clear_debug_folder(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for child in folder.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _assert_safe_debug_folder(folder, settings_obj):
    resolved = folder.resolve()
    protected_roots = [
        django_settings.BASE_DIR,
        django_settings.BASE_DIR / "data",
        Path(django_settings.MEDIA_ROOT),
        _configured_folder(settings_obj.reports_folder),
        _configured_folder(settings_obj.plagiarism_reports_folder),
        _configured_folder(settings_obj.extraction_results_folder),
        _configured_folder(settings_obj.active_final_folder),
        _configured_folder(settings_obj.old_versions_folder),
        _configured_folder(settings_obj.incoming_folder),
        django_settings.BASE_DIR / "data" / "crosscheck_upload",
        django_settings.BASE_DIR / "data" / "system_state_backups",
    ]
    unsafe_names = {"", "/", "data", "media", "reports", "crosscheck_upload"}
    if folder.name in unsafe_names:
        raise ValueError("Publication PDF debug folder must be a dedicated folder.")
    for protected in protected_roots:
        protected = protected.resolve()
        if resolved == protected:
            raise ValueError("Publication PDF debug folder must not be a protected app folder.")
        try:
            protected.relative_to(resolved)
            raise ValueError(
                "Publication PDF debug folder must not contain other managed app folders."
            )
        except ValueError as exc:
            if "must not" in str(exc):
                raise


def sync_publication_pdf_debug_folder():
    try:
        settings_obj = AppSetting.load()
        debug_folder = resolve_folder(
            getattr(settings_obj, "publication_pdf_debug_folder", "data/publication_pdf_debug")
        )
        _assert_safe_debug_folder(debug_folder, settings_obj)
        _clear_debug_folder(debug_folder)
        manifest_path = debug_folder / "publication_pdf_debug_manifest.csv"
        synced = []
        skipped = []
        active_by_paper = {
            submission.paper_id_filled: submission
            for submission in FinalSubmission.objects.filter(
                active_version=True,
                discarded=False,
                excluded_from_publication=False,
            )
        }
        with manifest_path.open("w", newline="", encoding="utf-8-sig") as manifest_file:
            writer = csv.DictWriter(
                manifest_file,
                fieldnames=[
                    "paper_id",
                    "final_submission_id",
                    "publication_pdf_source",
                    "source_path",
                    "debug_filename",
                    "sha256",
                ],
            )
            writer.writeheader()
            for paper in InitialPaper.objects.all().order_by("paper_id"):
                submission = active_by_paper.get(paper.paper_id)
                if not submission:
                    skipped.append(
                        {
                            "paper_id": paper.paper_id,
                            "final_submission_id": "",
                            "message": "No active final submission.",
                        }
                    )
                    continue
                pdf_info = publication_pdf_info(submission)
                if not pdf_info["exists"]:
                    skipped.append(
                        {
                            "paper_id": paper.paper_id,
                            "final_submission_id": submission.final_submission_id,
                            "message": "No publication PDF source.",
                        }
                    )
                    continue
                filename = publication_pdf_filename(
                    paper.paper_id,
                    submission.extracted_title,
                    settings_obj.title_words_for_filename,
                )
                target = debug_folder / filename
                shutil.copy2(pdf_info["path"], target)
                row = {
                    "paper_id": paper.paper_id,
                    "final_submission_id": submission.final_submission_id,
                    "publication_pdf_source": pdf_info["label"],
                    "source_path": pdf_info["path"],
                    "debug_filename": filename,
                    "sha256": _sha256(target),
                }
                synced.append(row)
                writer.writerow(row)
        result = {
            "folder": str(debug_folder),
            "manifest_path": str(manifest_path),
            "synced": synced,
            "skipped": skipped,
            "synced_count": len(synced),
            "skipped_count": len(skipped),
        }
        audit_success(
            "sync_publication_pdf_debug",
            "Publication PDF debug folder synced.",
            result_counts={
                "synced_count": len(synced),
                "skipped_count": len(skipped),
            },
            file_changes={"folder": str(debug_folder), "manifest_path": str(manifest_path)},
            extra={"skipped": skipped[:20]},
        )
        return result
    except Exception as exc:
        audit_failure("sync_publication_pdf_debug", exc, "Publication PDF debug sync failed.")
        raise


def _current_source_path(submission):
    for field_file in [submission.formatted_source_file, submission.source_file]:
        path = _filefield_path(field_file)
        if path and path.exists():
            return path
    current = _text_path(submission.source_current_file_path)
    if current and current.exists():
        return current
    return None


def repair_publication_paths(force=False):
    settings_obj = AppSetting.load()
    active_folder = resolve_folder(settings_obj.active_final_folder)
    old_folder = resolve_folder(settings_obj.old_versions_folder)
    pdf_repaired = []
    source_repaired = []
    skipped = []
    pending_updates = {}
    for submission in FinalSubmission.objects.all():
        source = source_pdf_path(submission)
        if source:
            paper_part = sanitize_filename_part(submission.paper_id_filled or "NO_PAPER_ID")
            if submission.active_version:
                title_part = title_short_name(
                    submission.extracted_title or submission.final_submission_title,
                    settings_obj.title_words_for_filename,
                )
                target = active_folder / f"{paper_part}-{title_part}.pdf"
            else:
                target = old_folder / f"{paper_part}-{sanitize_filename_part(submission.final_submission_id)}.pdf"
            needs_copy = force or not target.exists() or submission.current_file_path != str(target)
            if needs_copy:
                copied = copy_pdf_to_folder(submission, target.parent, target.name)
            else:
                copied = target
            if copied and copied.exists():
                submission.current_file_path = str(copied)
                pending_updates[submission.pk] = submission
                pdf_repaired.append(
                    {
                        "final_submission_id": submission.final_submission_id,
                        "paper_id": submission.paper_id_filled,
                        "path": str(copied),
                        "copied": bool(needs_copy),
                    }
                )
            else:
                skipped.append(
                    {
                        "final_submission_id": submission.final_submission_id,
                        "paper_id": submission.paper_id_filled,
                        "message": "PDF copy failed.",
                    }
                )
        else:
            skipped.append(
                {
                    "final_submission_id": submission.final_submission_id,
                    "paper_id": submission.paper_id_filled,
                    "message": "No source PDF is available.",
                }
            )

        source_current = _text_path(submission.source_current_file_path)
        source_path = _current_source_path(submission)
        if source_path and (force or not source_current or not source_current.exists()):
            submission.source_current_file_path = str(source_path)
            pending_updates[submission.pk] = submission
            source_repaired.append(
                {
                    "final_submission_id": submission.final_submission_id,
                    "paper_id": submission.paper_id_filled,
                    "path": str(source_path),
                }
            )
        elif not source_path and submission.source_current_file_path:
            skipped.append(
                {
                    "final_submission_id": submission.final_submission_id,
                    "paper_id": submission.paper_id_filled,
                    "message": "No source manuscript is available.",
                }
            )
        if len(pending_updates) >= 100:
            bulk_update_submissions(
                pending_updates.values(),
                ["current_file_path", "source_current_file_path"],
            )
            pending_updates.clear()
    if pending_updates:
        bulk_update_submissions(
            pending_updates.values(),
            ["current_file_path", "source_current_file_path"],
        )
    repaired = pdf_repaired + source_repaired
    return {
        "repaired": repaired,
        "pdf_repaired": pdf_repaired,
        "source_repaired": source_repaired,
        "skipped": skipped,
        "repaired_count": len(repaired),
        "pdf_repaired_count": len(pdf_repaired),
        "source_repaired_count": len(source_repaired),
        "skipped_count": len(skipped),
    }


def prune_generated_cache(selection=None):
    preview = preview_storage_cleanup("generated_cache")
    return apply_storage_cleanup(preview["token"], CLEANUP_CONFIRMATION_TEXT)
