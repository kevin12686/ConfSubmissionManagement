#!/usr/bin/env python3
"""Mirror all named-volume Docker instance data to host directories."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_instance_tools import (  # noqa: E402
    DockerCommandError,
    exclusive_lock,
    inspect_compose_web_containers,
    matching_instances,
    start_container,
    stop_container,
    transfer_data,
    verify_data,
    wait_until_running,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mirror every named-volume Docker instance for this checkout to its "
            "configured SMS_DATA_DIR host folder."
        )
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only back up the named Compose project. Can be used more than once.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the instances and destinations without copying or stopping.",
    )
    parser.add_argument(
        "--stop-timeout",
        type=int,
        default=30,
        help="Seconds Docker gives Gunicorn to stop gracefully. Default: 30.",
    )
    args = parser.parse_args()
    if args.stop_timeout < 1:
        parser.error("--stop-timeout must be at least 1.")

    root = SCRIPT_DIR.parent
    lock_path = root / "runtime" / ".docker-data-operation.lock"
    try:
        with exclusive_lock(lock_path):
            return run_backups(
                root,
                selected_projects=set(args.project),
                dry_run=args.dry_run,
                stop_timeout=args.stop_timeout,
            )
    except (DockerCommandError, RuntimeError, ValueError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1


def run_backups(
    root: Path,
    *,
    selected_projects: set[str],
    dry_run: bool,
    stop_timeout: int,
) -> int:
    containers = inspect_compose_web_containers()
    instances = matching_instances(containers, root, selected_projects)
    if not instances:
        print(
            "No matching Docker Compose web containers were found for this checkout.",
            file=sys.stderr,
        )
        return 1

    failures = 0
    backed_up = 0
    for instance in instances:
        if instance["mount_type"] != "volume":
            print(
                f"Project: {instance['project']} - already uses host bind data at "
                f"{instance['sms_data_dir']}; no volume backup required."
            )
            continue
        try:
            backup_instance(
                instance,
                root,
                dry_run=dry_run,
                stop_timeout=stop_timeout,
            )
            backed_up += 1
        except Exception as exc:
            failures += 1
            print(
                f"Project {instance['project']} backup failed: {exc}",
                file=sys.stderr,
            )
    if failures:
        print(f"{failures} project backup(s) failed.", file=sys.stderr)
        return 1
    if not backed_up:
        print("No named-volume instances required backup.")
    return 0


def backup_instance(
    instance: dict,
    root: Path,
    *,
    dry_run: bool,
    stop_timeout: int,
) -> None:
    target = Path(instance["sms_data_dir"])
    staging = select_staging_path(target)
    print(f"Project: {instance['project']} ({instance['name']})")
    print(f"  source volume: {instance['volume_name']}")
    print(f"  host mirror:   {target}")
    print(f"  staging:       {staging}")
    print(
        "  final sync:    graceful stop and automatic restart"
        if instance["running"]
        else "  final sync:    container is already stopped"
    )
    if dry_run:
        return

    started_at = datetime.now(timezone.utc)
    success = False
    try:
        recover_interrupted_promotion(target)
        pre_sync = transfer_data(
            root=root,
            image=instance["image"],
            source_type="volume",
            source=instance["volume_name"],
            destination_type="bind",
            destination=str(staging),
            verify_content=True,
            tolerate_source_changes=True,
        )
        if instance["running"]:
            stop_container(instance, stop_timeout)
        transfer_result = transfer_data(
            root=root,
            image=instance["image"],
            source_type="volume",
            source=instance["volume_name"],
            destination_type="bind",
            destination=str(staging),
            verify_content=True,
            baseline_manifest=pre_sync["manifest"],
        )
        transfer_result.pop("manifest", None)
        verification = verify_data(
            root=root,
            image=instance["image"],
            data_type="bind",
            data_source=str(staging),
        )
        promote_staging(staging, target)
        success = True
        print(
            f"  completed: {verification['file_count']} files, "
            f"{verification['total_bytes']} bytes, SQLite integrity ok"
        )
        append_history(
            target,
            instance,
            status="success",
            started_at=started_at,
            details={
                "transfer": transfer_result,
                "verification": verification,
            },
        )
    except Exception as exc:
        append_history(
            target,
            instance,
            status="failed",
            started_at=started_at,
            details={"error": str(exc)},
        )
        raise
    finally:
        if instance["running"]:
            try:
                start_container(instance)
                wait_until_running(instance["id"])
            except Exception:
                if success:
                    raise
                print(
                    f"Project {instance['project']} could not be restarted automatically.",
                    file=sys.stderr,
                )


def select_staging_path(target: Path) -> Path:
    previous = target.with_name(f"{target.name}.backup-previous")
    return previous if previous.exists() else target.with_name(
        f"{target.name}.backup-next"
    )


def recover_interrupted_promotion(target: Path) -> None:
    swap = target.with_name(f"{target.name}.backup-swap")
    if not swap.exists():
        return
    if target.exists():
        raise RuntimeError(
            f"Both the host mirror and interrupted swap exist: {target}, {swap}"
        )
    swap.rename(target)


def promote_staging(staging: Path, target: Path) -> None:
    if not staging.is_dir():
        raise RuntimeError(f"Backup staging directory is missing: {staging}")
    previous = target.with_name(f"{target.name}.backup-previous")
    swap = target.with_name(f"{target.name}.backup-swap")
    if swap.exists():
        raise RuntimeError(f"Backup swap path already exists: {swap}")

    if not target.exists():
        staging.rename(target)
        return

    target.rename(swap)
    try:
        staging.rename(target)
    except Exception:
        swap.rename(target)
        raise

    if previous.exists() and previous != staging:
        shutil.rmtree(previous)
    try:
        swap.rename(previous)
    except Exception:
        print(
            f"Warning: previous backup remains at {swap}; latest mirror is valid.",
            file=sys.stderr,
        )


def append_history(
    target: Path,
    instance: dict,
    *,
    status: str,
    started_at: datetime,
    details: dict,
) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        history_path = target.parent / ".sms-docker-backup-history.jsonl"
        event = {
            "project": instance["project"],
            "status": status,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "host_mirror": str(target),
            "volume": instance["volume_name"],
            **details,
        }
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"Warning: could not write backup history: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
