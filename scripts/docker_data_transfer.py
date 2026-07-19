#!/usr/bin/env python3
"""Synchronize and validate raw Conference Final Manager data directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path


CHUNK_SIZE = 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("source", type=Path)
    sync_parser.add_argument("destination", type=Path)
    sync_parser.add_argument("--verify-content", action="store_true")
    sync_parser.add_argument("--baseline-manifest", type=Path)
    sync_parser.add_argument("--tolerate-source-changes", action="store_true")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("data_dir", type=Path)
    verify_parser.add_argument("--database", default="db.sqlite3")

    args = parser.parse_args()
    try:
        if args.command == "sync":
            result = sync_tree(
                args.source,
                args.destination,
                verify_content=args.verify_content,
                baseline_manifest=_load_baseline(args.baseline_manifest),
                tolerate_source_changes=args.tolerate_source_changes,
            )
        else:
            result = verify_data_directory(args.data_dir, args.database)
    except Exception as exc:
        print(f"Data transfer failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def sync_tree(
    source: Path,
    destination: Path,
    *,
    verify_content: bool,
    baseline_manifest: dict[str, dict] | None = None,
    tolerate_source_changes: bool = False,
) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if source == destination:
        raise ValueError("Source and destination directories must be different.")

    destination.mkdir(parents=True, exist_ok=True)
    source_entries = _collect_entries(source)
    destination_entries = _collect_entries(destination)
    copied = 0
    removed = 0
    total_bytes = 0
    manifest = {}
    unstable_files = 0

    for relative, kind in sorted(source_entries.items()):
        source_path = source / relative
        destination_path = destination / relative
        if kind == "directory":
            if destination_path.exists() and not destination_path.is_dir():
                _remove_path(destination_path)
                removed += 1
            destination_path.mkdir(parents=True, exist_ok=True)
            continue

        try:
            total_bytes += source_path.stat().st_size
            if destination_path.exists() and not destination_path.is_file():
                _remove_path(destination_path)
                removed += 1
            source_size = source_path.stat().st_size
            source_hash = _sha256(source_path) if verify_content else ""
            baseline_entry = (baseline_manifest or {}).get(relative, {})
            trusted_baseline = bool(
                verify_content
                and destination_path.is_file()
                and destination_path.stat().st_size == source_size
                and baseline_entry.get("size") == source_size
                and baseline_entry.get("sha256") == source_hash
            )
            if not trusted_baseline and not _files_equal(
                source_path,
                destination_path,
                verify_content=verify_content,
                source_hash=source_hash,
            ):
                _copy_file(source_path, destination_path)
                copied += 1
            if verify_content:
                final_source_hash = source_hash
                if not trusted_baseline:
                    final_source_hash = _sha256(source_path)
                    if final_source_hash != _sha256(destination_path):
                        if tolerate_source_changes:
                            unstable_files += 1
                            continue
                        raise ValueError(
                            f"Content verification failed: {relative}"
                        )
                manifest[relative] = {
                    "sha256": final_source_hash,
                    "size": source_size,
                }
        except (FileNotFoundError, PermissionError):
            if not tolerate_source_changes:
                raise
            unstable_files += 1

    extra_entries = sorted(
        set(destination_entries) - set(source_entries),
        key=lambda value: (len(Path(value).parts), value),
        reverse=True,
    )
    for relative in extra_entries:
        target = destination / relative
        if target.exists() or target.is_symlink():
            _remove_path(target)
            removed += 1

    return {
        "copied_files": copied,
        "removed_entries": removed,
        "source_bytes": total_bytes,
        "source_entries": len(source_entries),
        "unstable_files": unstable_files,
        "verified_content": verify_content,
        "manifest": manifest,
    }


def verify_data_directory(data_dir: Path, database_name: str) -> dict:
    data_dir = data_dir.resolve()
    database_path = data_dir / database_name
    if not database_path.is_file():
        raise ValueError(f"SQLite database does not exist: {database_path}")

    uri = f"{database_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    finally:
        connection.close()
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        raise ValueError(f"SQLite integrity_check failed: {messages}")

    entries = _collect_entries(data_dir)
    file_count = sum(1 for kind in entries.values() if kind == "file")
    total_bytes = sum(
        (data_dir / relative).stat().st_size
        for relative, kind in entries.items()
        if kind == "file"
    )
    return {
        "database": database_name,
        "file_count": file_count,
        "integrity_check": "ok",
        "total_bytes": total_bytes,
    }


def _collect_entries(root: Path) -> dict[str, str]:
    entries = {}
    if not root.exists():
        return entries
    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        for name in sorted(dirnames):
            path = current / name
            if path.is_symlink():
                raise ValueError(f"Symbolic links are not supported: {path}")
            entries[path.relative_to(root).as_posix()] = "directory"
        for name in sorted(filenames):
            path = current / name
            if path.is_symlink():
                raise ValueError(f"Symbolic links are not supported: {path}")
            if not path.is_file():
                raise ValueError(f"Unsupported data entry: {path}")
            entries[path.relative_to(root).as_posix()] = "file"
    return entries


def _files_equal(
    source: Path,
    destination: Path,
    *,
    verify_content: bool,
    source_hash: str = "",
) -> bool:
    if not destination.is_file():
        return False
    source_stat = source.stat()
    destination_stat = destination.stat()
    if source_stat.st_size != destination_stat.st_size:
        return False
    if verify_content:
        return (source_hash or _sha256(source)) == _sha256(destination)
    return source_stat.st_mtime_ns == destination_stat.st_mtime_ns


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.sms-copy-{uuid.uuid4().hex}"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_baseline(path: Path | None) -> dict[str, dict] | None:
    if path is None:
        return None
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Baseline manifest must be a JSON object.")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
