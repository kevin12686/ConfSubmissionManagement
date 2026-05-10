import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.file_manager import (
    copy_pdf_to_folder,
    resolve_folder,
    sanitize_filename_part,
    source_pdf_path,
    title_short_name,
)


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
@dataclass(frozen=True)
class StoragePathRef:
    path: Path
    category: str
    role: str
    protected: bool = True


def cleanup_preview_root():
    root = django_settings.BASE_DIR / "data" / "storage_cleanup_previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _format_size(size):
    value = float(size or 0)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _path_size(path):
    return path.stat().st_size if path.exists() and path.is_file() else 0


def _safe_path(path):
    return str(Path(path).resolve())


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
            _configured_folder(settings_obj.active_final_folder),
            _configured_folder(settings_obj.old_versions_folder),
            django_settings.BASE_DIR / "data" / "crosscheck_upload",
        ],
        "reports_backups": [
            _configured_folder(settings_obj.reports_folder),
            _configured_folder(settings_obj.plagiarism_reports_folder),
            django_settings.BASE_DIR / "data" / "system_state_backups",
        ],
    }


def _cleanup_managed_output_roots(settings_obj):
    return [
        _configured_folder(settings_obj.active_final_folder),
        _configured_folder(settings_obj.old_versions_folder),
    ]


def _cleanup_report_export_roots(settings_obj):
    return [
        _configured_folder(settings_obj.reports_folder),
        django_settings.BASE_DIR / "data" / "crosscheck_upload",
        django_settings.BASE_DIR / "data" / "plagiarism_upload",
    ]


def _iter_files(root):
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def _collect_referenced_paths():
    refs = {}
    missing = []
    for submission in FinalSubmission.objects.all():
        submission_label = (
            f"Final {submission.final_submission_id} / "
            f"{submission.paper_id_filled or 'No Paper ID'}"
        )
        candidates = [
            ("canonical_original", "Original PDF", _filefield_path(submission.pdf_file)),
            ("canonical_original", "Original source", _filefield_path(submission.source_file)),
            ("corrected", "Corrected PDF", _filefield_path(submission.formatted_pdf_file)),
            ("corrected", "Corrected source", _filefield_path(submission.formatted_source_file)),
            ("managed_output", "Current publication PDF", _text_path(submission.current_file_path)),
            ("managed_output", "Current source", _text_path(submission.source_current_file_path)),
            ("generated_cache", "PDF thumbnails", _text_path(submission.thumbnail_folder)),
            (
                "generated_cache",
                "Title/author verification image",
                _text_path(submission.title_author_verification_image),
            ),
            ("reports_backups", "Plagiarism report", _text_path(submission.plagiarism_report_path)),
        ]
        for category, role, path in candidates:
            if not path:
                continue
            refs[_safe_path(path)] = StoragePathRef(path.resolve(), category, role)
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


def build_storage_inventory():
    settings_obj = AppSetting.load()
    roots = _managed_roots(settings_obj)
    managed_output_cleanup_roots = _cleanup_managed_output_roots(settings_obj)
    refs, missing_refs = _collect_referenced_paths()
    categories = {}
    all_files = {}
    for category, root_list in roots.items():
        total_size = 0
        total_count = 0
        for root in root_list:
            for path in _iter_files(root) or []:
                resolved = path.resolve()
                all_files[_safe_path(resolved)] = {"path": resolved, "category": category}
                total_size += _path_size(resolved)
                total_count += 1
        labels = {
            "canonical_original": "Canonical originals",
            "corrected": "Corrected files",
            "generated_cache": "Generated cache",
            "managed_output": "Managed outputs",
            "reports_backups": "Reports and backups",
        }
        categories[category] = _category_row(
            category,
            labels.get(category, category.replace("_", " ").title()),
            total_size,
            total_count,
        )

    orphaned = []
    cleanup_candidates = []
    for key, file_info in sorted(all_files.items()):
        path = file_info["path"]
        category = file_info["category"]
        referenced = key in refs or _file_is_under_referenced_cache_path(path, refs)
        if not referenced:
            orphaned.append(
                {
                    "path": str(path),
                    "category": category,
                    "size": _path_size(path),
                    "size_label": _format_size(_path_size(path)),
                    "reason": "Not referenced by database records.",
                }
            )
        if category == "generated_cache" and not referenced:
            cleanup_candidates.append(
                {
                    "path": str(path),
                    "category": category,
                    "size": _path_size(path),
                    "size_label": _format_size(_path_size(path)),
                    "reason": "Generated cache can be regenerated.",
                }
            )
        elif (
            category == "managed_output"
            and not referenced
            and any(_relative_to(path, root) for root in managed_output_cleanup_roots)
        ):
            cleanup_candidates.append(
                {
                    "path": str(path),
                    "category": category,
                    "size": _path_size(path),
                    "size_label": _format_size(_path_size(path)),
                    "reason": "Orphaned active/old publication output is not referenced by database records.",
                }
            )

    large_files = sorted(
        [
            {
                "path": str(info["path"]),
                "category": info["category"],
                "size": _path_size(info["path"]),
                "size_label": _format_size(_path_size(info["path"])),
            }
            for info in all_files.values()
        ],
        key=lambda row: row["size"],
        reverse=True,
    )[:20]
    return {
        "categories": list(categories.values()),
        "missing_references": missing_refs,
        "orphaned_files": orphaned,
        "cleanup_candidates": cleanup_candidates,
        "report_export_cleanup_candidates": _report_export_cleanup_candidates(settings_obj),
        "large_files": large_files,
        "total_size": sum(row["size"] for row in categories.values()),
        "total_size_label": _format_size(sum(row["size"] for row in categories.values())),
        "total_file_count": sum(row["count"] for row in categories.values()),
    }


def _file_is_under_referenced_cache_path(path, refs):
    for ref in refs.values():
        if ref.category != "generated_cache" or not ref.path.exists() or not ref.path.is_dir():
            continue
        if _relative_to(path, ref.path):
            return True
    return False


def _report_export_cleanup_candidates(settings_obj):
    candidates = []
    for root in _cleanup_report_export_roots(settings_obj):
        for path in _iter_files(root) or []:
            if root == _configured_folder(settings_obj.reports_folder) and path.suffix.lower() not in REPORT_EXPORT_EXTENSIONS:
                continue
            candidates.append(
                {
                    "path": str(path.resolve()),
                    "category": "report_export",
                    "size": _path_size(path),
                    "size_label": _format_size(_path_size(path)),
                    "reason": "Generated report/export download can be regenerated.",
                }
            )
    return candidates


def preview_storage_cleanup(policy="generated_cache_or_orphan_output"):
    inventory = build_storage_inventory()
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
    return payload


def load_storage_cleanup_preview(token):
    token = str(token or "").strip()
    if not token or "/" in token or "\\" in token:
        raise ValueError("Invalid cleanup preview token.")
    path = cleanup_preview_root() / f"{token}.json"
    if not path.exists():
        raise ValueError("Cleanup preview not found. Create a new preview.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expires_at = parse_datetime(payload["expires_at"])
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
    if confirmation != CLEANUP_CONFIRMATION_TEXT:
        raise ValueError(f'Type "{CLEANUP_CONFIRMATION_TEXT}" to confirm cleanup.')
    payload = load_storage_cleanup_preview(token)
    current_refs, _missing = _collect_referenced_paths()
    deleted = []
    skipped = []
    for row in payload["files"]:
        path = Path(row["path"])
        if not _deletable_path(path, row.get("category")):
            skipped.append({**row, "message": "Path is outside cleanup-approved folders."})
            continue
        if _safe_path(path) in current_refs:
            skipped.append({**row, "message": "Path is now referenced by a database record."})
            continue
        if not path.exists():
            skipped.append({**row, "message": "File no longer exists."})
            continue
        size = _path_size(path)
        path.unlink()
        deleted.append({**row, "size": size, "size_label": _format_size(size)})
    preview_path = cleanup_preview_root() / f"{payload['token']}.json"
    preview_path.unlink(missing_ok=True)
    _remove_empty_generated_cache_dirs()
    _remove_empty_managed_output_dirs()
    return {
        "deleted": deleted,
        "skipped": skipped,
        "deleted_count": len(deleted),
        "skipped_count": len(skipped),
        "deleted_size": sum(row["size"] for row in deleted),
        "deleted_size_label": _format_size(sum(row["size"] for row in deleted)),
    }


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
                submission.save(update_fields=["current_file_path", "updated_at"])
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
            submission.save(update_fields=["source_current_file_path", "updated_at"])
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
