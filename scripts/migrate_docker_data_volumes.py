#!/usr/bin/env python3
"""Migrate existing bind-mounted Docker instance data into named volumes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_instance_tools import (  # noqa: E402
    DockerCommandError,
    compose_command,
    compose_env,
    ensure_compose_volume,
    exclusive_lock,
    inspect_compose_web_containers,
    matching_instances,
    planned_volume_name,
    run,
    start_container,
    stop_container,
    temporary_env_file,
    transfer_data,
    verify_data,
    wait_until_running,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate bind-mounted /app/data directories for every Docker Compose "
            "instance in this checkout into project-scoped named volumes."
        )
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only migrate the named Compose project. Can be used more than once.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration plan without creating volumes or stopping containers.",
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
            return run_migrations(
                root,
                selected_projects=set(args.project),
                dry_run=args.dry_run,
                stop_timeout=args.stop_timeout,
            )
    except (DockerCommandError, RuntimeError, ValueError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


def run_migrations(
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
    migrated = 0
    for instance in instances:
        if instance["mount_type"] == "volume":
            print(
                f"Project: {instance['project']} - already uses named volume "
                f"{instance['volume_name']}."
            )
            continue
        try:
            migrate_instance(
                instance,
                root,
                dry_run=dry_run,
                stop_timeout=stop_timeout,
            )
            migrated += 1
        except Exception as exc:
            failures += 1
            print(
                f"Project {instance['project']} migration failed: {exc}",
                file=sys.stderr,
            )
    if failures:
        print(f"{failures} project migration(s) failed.", file=sys.stderr)
        return 1
    if not migrated:
        print("No bind-mounted instances required migration.")
    return 0


def migrate_instance(
    instance: dict,
    root: Path,
    *,
    dry_run: bool,
    stop_timeout: int,
) -> None:
    env_values = compose_env(instance)
    with temporary_env_file(env_values) as env_file:
        volume_name = planned_volume_name(root, instance, env_file)
        print(f"Project: {instance['project']} ({instance['name']})")
        print(f"  current host data: {instance['sms_data_dir']}")
        print(f"  target volume:     {volume_name}")
        print(
            "  cutover:           online pre-copy, graceful stop, verified final copy"
        )
        if dry_run:
            return

        run(
            compose_command(
                root,
                env_file,
                instance["project"],
                "build",
                "web",
            ),
            cwd=root,
            capture=False,
        )
        ensure_compose_volume(instance["project"], volume_name)
        pre_sync = transfer_data(
            root=root,
            image=instance["image"],
            source_type="bind",
            source=instance["sms_data_dir"],
            destination_type="volume",
            destination=volume_name,
            verify_content=True,
            tolerate_source_changes=True,
        )

        cutover_attempted = False
        try:
            if instance["running"]:
                stop_container(instance, stop_timeout)
            transfer_data(
                root=root,
                image=instance["image"],
                source_type="bind",
                source=instance["sms_data_dir"],
                destination_type="volume",
                destination=volume_name,
                verify_content=True,
                baseline_manifest=pre_sync["manifest"],
            )
            verification = verify_data(
                root=root,
                image=instance["image"],
                data_type="volume",
                data_source=volume_name,
            )
            cutover_attempted = True
            action = ("up", "-d", "--no-build") if instance["running"] else (
                "create",
                "--no-build",
                "web",
            )
            run(
                compose_command(
                    root,
                    env_file,
                    instance["project"],
                    *action,
                ),
                cwd=root,
                capture=False,
            )
            migrated = current_project_instance(root, instance["project"])
            if not migrated or migrated["mount_type"] != "volume":
                raise RuntimeError("Recreated container is not using a named volume.")
            if migrated["volume_name"] != volume_name:
                raise RuntimeError(
                    "Recreated container uses an unexpected named volume: "
                    f"{migrated['volume_name']}"
                )
            if instance["running"]:
                wait_until_running(migrated["id"])
            print(
                f"  completed: {verification['file_count']} files, "
                f"{verification['total_bytes']} bytes, SQLite integrity ok"
            )
            print(
                f"  rollback copy remains at: {instance['sms_data_dir']}"
            )
        except Exception:
            rollback_to_bind_mount(
                instance,
                root,
                env_file,
                cutover_attempted=cutover_attempted,
            )
            raise


def rollback_to_bind_mount(
    instance: dict,
    root: Path,
    env_file: Path,
    *,
    cutover_attempted: bool,
) -> None:
    print(
        f"  restoring bind-mounted container for {instance['project']}...",
        file=sys.stderr,
    )
    if not cutover_attempted:
        if instance["running"]:
            start_container(instance)
            wait_until_running(instance["id"])
        return
    action = ("up", "-d", "--no-build") if instance["running"] else (
        "create",
        "--no-build",
        "web",
    )
    run(
        compose_command(
            root,
            env_file,
            instance["project"],
            *action,
            bind_override=True,
        ),
        cwd=root,
        capture=False,
    )


def current_project_instance(root: Path, project: str) -> dict | None:
    instances = matching_instances(
        inspect_compose_web_containers(),
        root,
        {project},
    )
    return instances[0] if instances else None


if __name__ == "__main__":
    raise SystemExit(main())
