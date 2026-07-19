#!/usr/bin/env python3
"""Rebuild and restart existing Docker Compose instances for this checkout."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


SERVICE_NAME = "web"
APP_DATA_DESTINATION = "/app/data"
APP_PORT = "8000/tcp"
BACKUP_DIR_LABEL = "com.conferencefinalmanager.host-data-dir"
ENV_KEYS = (
    "SMS_SECRET_KEY",
    "SMS_DEBUG",
    "SMS_ALLOWED_HOSTS",
    "SMS_RUN_MIGRATIONS",
    "SMS_WEB_WORKERS",
    "SMS_WEB_THREADS",
    "SMS_WEB_TIMEOUT",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild Docker Compose projects that already have a web container "
            "created from this repository checkout."
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

    root = Path(__file__).resolve().parent.parent
    containers = inspect_compose_web_containers()
    instances = matching_instances(containers, root, set(args.project))
    if not instances:
        print(
            "No matching Docker Compose web containers were found for this checkout.",
            file=sys.stderr,
        )
        print(
            "Start an instance first, for example: "
            "docker compose --env-file .env.conference-a -p sms-conf-a up -d --build",
            file=sys.stderr,
        )
        return 1

    for instance in instances:
        rebuild_instance(instance, root, dry_run=args.dry_run)
    return 0


def inspect_compose_web_containers() -> list[dict]:
    ids = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project",
            "--filter",
            f"label=com.docker.compose.service={SERVICE_NAME}",
            "--format",
            "{{.ID}}",
        ],
        capture=True,
    ).stdout.splitlines()
    if not ids:
        return []
    payload = run(["docker", "inspect", *ids], capture=True).stdout
    return json.loads(payload)


def matching_instances(
    containers: list[dict], root: Path, selected_projects: set[str]
) -> list[dict]:
    by_project = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project", "")
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        if not project or not same_path(working_dir, root):
            continue
        if selected_projects and project not in selected_projects:
            continue
        instance = instance_from_container(container, project)
        if not instance:
            continue
        existing = by_project.get(project)
        if existing is None or (
            not existing["running"] and instance["running"]
        ):
            by_project[project] = instance
    return [by_project[name] for name in sorted(by_project)]


def instance_from_container(container: dict, project: str) -> dict | None:
    mount = data_mount(container)
    port_binding = first_port_binding(container)
    if not mount or not port_binding:
        name = container.get("Name", "").lstrip("/") or container.get("Id", "")[:12]
        print(
            f"Skipping {name}: could not infer {APP_DATA_DESTINATION} mount or port.",
            file=sys.stderr,
        )
        return None
    mount_type = mount.get("Type") or (
        "volume" if mount.get("Name") else "bind"
    )
    labels = container.get("Config", {}).get("Labels", {}) or {}
    data_dir = (
        mount.get("Source", "")
        if mount_type == "bind"
        else labels.get(BACKUP_DIR_LABEL, "")
    )
    if not data_dir:
        name = container.get("Name", "").lstrip("/") or container.get("Id", "")[:12]
        print(
            f"Skipping {name}: could not infer the host backup directory.",
            file=sys.stderr,
        )
        return None
    env = container_env(container)
    return {
        "project": project,
        "name": container.get("Name", "").lstrip("/"),
        "running": bool(container.get("State", {}).get("Running")),
        "mount_type": mount_type,
        "volume_name": mount.get("Name") or (
            mount.get("Source", "") if mount_type == "volume" else ""
        ),
        "sms_bind_host": port_binding.get("HostIp") or "0.0.0.0",
        "sms_port": port_binding.get("HostPort", ""),
        "sms_data_dir": data_dir,
        "env": {key: env[key] for key in ENV_KEYS if key in env},
    }


def data_mount(container: dict) -> dict:
    for mount in container.get("Mounts", []):
        if mount.get("Destination") == APP_DATA_DESTINATION:
            return mount
    return {}


def data_mount_source(container: dict) -> str:
    return data_mount(container).get("Source", "")


def first_port_binding(container: dict) -> dict:
    bindings = (
        container.get("HostConfig", {})
        .get("PortBindings", {})
        .get(APP_PORT, [])
    )
    return bindings[0] if bindings else {}


def container_env(container: dict) -> dict[str, str]:
    env = {}
    for item in container.get("Config", {}).get("Env", []) or []:
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    return env


def rebuild_instance(instance: dict, root: Path, *, dry_run: bool) -> None:
    env_values = {
        "SMS_BIND_HOST": instance["sms_bind_host"],
        "SMS_PORT": instance["sms_port"],
        "SMS_DATA_DIR": instance["sms_data_dir"],
        **instance["env"],
    }
    command = [
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
    if instance.get("mount_type") == "volume":
        print(f"  SMS_DATA_VOLUME={instance.get('volume_name', '')}")
    for key in sorted(env_values):
        display_value = "***" if key == "SMS_SECRET_KEY" else env_values[key]
        print(f"  {key}={display_value}")
    print(f"  command: {shlex.join(command)}")
    if dry_run:
        return

    env_file_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as handle:
            env_file_path = Path(handle.name)
            for key, value in env_values.items():
                handle.write(format_env_line(key, value))
        actual_command = command.copy()
        actual_command[actual_command.index("<generated>")] = str(env_file_path)
        run(actual_command, cwd=root, capture=False)
    finally:
        if env_file_path is not None:
            env_file_path.unlink(missing_ok=True)


def format_env_line(key: str, value: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+-]+$", value):
        return f"{key}={value}\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def same_path(left: str, right: Path) -> bool:
    if not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def run(
    command: list[str], *, cwd: Path | None = None, capture: bool
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError:
        print(f"Command not found: {command[0]}", file=sys.stderr)
        raise SystemExit(127)
    except subprocess.CalledProcessError as exc:
        if capture and exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        raise SystemExit(exc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
