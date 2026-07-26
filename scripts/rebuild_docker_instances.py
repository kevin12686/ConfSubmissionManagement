#!/usr/bin/env python3
"""Rebuild and restart existing Docker Compose instances for this checkout."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_instance_tools import (  # noqa: E402
    DockerCommandError,
    compose_command,
    compose_env,
    inspect_compose_containers,
    matching_instances,
    run,
    temporary_env_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild Docker Compose projects that already have an application "
            "container created from this repository checkout."
        )
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only rebuild the named Compose project. Can be used more than once.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the inferred settings and commands without running them.",
    )
    args = parser.parse_args()

    root = SCRIPT_DIR.parent
    try:
        instances = matching_instances(
            inspect_compose_containers(),
            root,
            set(args.project),
        )
        if not instances:
            print(
                "No matching Docker Compose application instances were found "
                "for this checkout.",
                file=sys.stderr,
            )
            print(
                "Start an instance first, for example: "
                "docker compose --env-file .env.conference-a "
                "-p sms-conf-a up -d --build",
                file=sys.stderr,
            )
            return 1

        for instance in instances:
            rebuild_instance(instance, root, dry_run=args.dry_run)
    except (DockerCommandError, RuntimeError, ValueError) as exc:
        print(f"Rebuild failed: {exc}", file=sys.stderr)
        return 1
    return 0


def rebuild_instance(instance: dict, root: Path, *, dry_run: bool) -> None:
    env_values = compose_env(instance)
    display_command = [
        "docker",
        "compose",
        "--env-file",
        "<generated>",
        "-p",
        instance["project"],
        "up",
        "-d",
        "--build",
    ]
    print(f"Project: {instance['project']} ({instance['name']})")
    print(f"  public endpoint source: {instance['public_service']}")
    if instance.get("mount_type") == "volume":
        print(f"  SMS_DATA_VOLUME={instance.get('volume_name', '')}")
    for key in sorted(env_values):
        display_value = "***" if key == "SMS_SECRET_KEY" else env_values[key]
        print(f"  {key}={display_value}")
    print(f"  command: {shlex.join(display_command)}")
    if dry_run:
        return

    with temporary_env_file(env_values) as env_file:
        run(
            compose_command(
                root,
                env_file,
                instance["project"],
                "up",
                "-d",
                "--build",
            ),
            cwd=root,
            capture=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
