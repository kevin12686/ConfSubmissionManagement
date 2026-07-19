"""Shared Docker instance discovery and raw-data transfer helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


SERVICE_NAME = "web"
APP_DATA_DESTINATION = "/app/data"
APP_PORT = "8000/tcp"
BACKUP_DIR_LABEL = "com.conferencefinalmanager.host-data-dir"
VOLUME_KEY = "sms_data"
ENV_KEYS = (
    "SMS_SECRET_KEY",
    "SMS_DEBUG",
    "SMS_ALLOWED_HOSTS",
    "SMS_RUN_MIGRATIONS",
    "SMS_WEB_WORKERS",
    "SMS_WEB_THREADS",
    "SMS_WEB_TIMEOUT",
)


class DockerCommandError(RuntimeError):
    def __init__(self, command: list[str], returncode: int, stderr: str = ""):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        message = stderr.strip() or f"Command exited with status {returncode}."
        super().__init__(message)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool,
    check: bool = True,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:
        raise DockerCommandError(command, 127, f"Command not found: {command[0]}") from exc
    if check and result.returncode:
        raise DockerCommandError(command, result.returncode, result.stderr or "")
    return result


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
    return json.loads(run(["docker", "inspect", *ids], capture=True).stdout)


def matching_instances(
    containers: list[dict],
    root: Path,
    selected_projects: set[str],
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
        instance = instance_from_container(container, project, root)
        if not instance:
            continue
        existing = by_project.get(project)
        if existing is None or (
            not existing["running"] and instance["running"]
        ):
            by_project[project] = instance
    return [by_project[name] for name in sorted(by_project)]


def instance_from_container(
    container: dict,
    project: str,
    root: Path,
) -> dict | None:
    mount = data_mount(container)
    port_binding = first_port_binding(container)
    if not mount or not port_binding:
        return None
    mount_type = mount.get("Type") or (
        "volume" if mount.get("Name") else "bind"
    )
    labels = container.get("Config", {}).get("Labels", {}) or {}
    if mount_type == "bind":
        host_data_dir = mount.get("Source", "")
        volume_name = ""
    else:
        host_data_dir = labels.get(BACKUP_DIR_LABEL, "")
        volume_name = mount.get("Name") or mount.get("Source", "")
    if not host_data_dir:
        return None
    host_data_path = resolve_host_path(host_data_dir, root)
    env = container_env(container)
    return {
        "id": container.get("Id", ""),
        "project": project,
        "name": container.get("Name", "").lstrip("/"),
        "image": container.get("Config", {}).get("Image", ""),
        "running": bool(container.get("State", {}).get("Running")),
        "mount_type": mount_type,
        "volume_name": volume_name,
        "sms_bind_host": port_binding.get("HostIp") or "0.0.0.0",
        "sms_port": port_binding.get("HostPort", ""),
        "sms_data_dir": str(host_data_path),
        "env": {key: env[key] for key in ENV_KEYS if key in env},
    }


def data_mount(container: dict) -> dict:
    for mount in container.get("Mounts", []):
        if mount.get("Destination") == APP_DATA_DESTINATION:
            return mount
    return {}


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


def resolve_host_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def compose_env(instance: dict) -> dict[str, str]:
    return {
        "SMS_BIND_HOST": instance["sms_bind_host"],
        "SMS_PORT": instance["sms_port"],
        "SMS_DATA_DIR": instance["sms_data_dir"],
        **instance["env"],
    }


@contextmanager
def temporary_env_file(values: dict[str, str]):
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
        ) as handle:
            path = Path(handle.name)
            for key, value in values.items():
                handle.write(format_env_line(key, value))
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def format_env_line(key: str, value: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+\\-]+$", value):
        return f"{key}={value}\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


@contextmanager
def exclusive_lock(path: Path, *, stale_seconds: int = 12 * 60 * 60):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > stale_seconds:
        path.unlink()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another Docker data operation is active. Lock: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        yield
    finally:
        path.unlink(missing_ok=True)


def compose_command(
    root: Path,
    env_file: Path,
    project: str,
    *arguments: str,
    bind_override: bool = False,
) -> list[str]:
    command = ["docker", "compose"]
    if bind_override:
        command.extend(
            [
                "-f",
                str(root / "docker-compose.yml"),
                "-f",
                str(root / "docker-compose.bind.yml"),
            ]
        )
    command.extend(["--env-file", str(env_file), "-p", project, *arguments])
    return command


def planned_volume_name(root: Path, instance: dict, env_file: Path) -> str:
    command = compose_command(
        root,
        env_file,
        instance["project"],
        "config",
        "--format",
        "json",
    )
    payload = json.loads(run(command, cwd=root, capture=True).stdout)
    try:
        return payload["volumes"][VOLUME_KEY]["name"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Compose did not define the expected sms_data volume.") from exc


def ensure_compose_volume(project: str, volume_name: str) -> None:
    inspected = run(
        ["docker", "volume", "inspect", volume_name],
        capture=True,
        check=False,
    )
    if inspected.returncode == 0:
        payload = json.loads(inspected.stdout)
        labels = payload[0].get("Labels", {}) or {}
        if (
            labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.volume") != VOLUME_KEY
        ):
            raise RuntimeError(
                f"Refusing to reuse Docker volume with unexpected ownership: "
                f"{volume_name}"
            )
        return
    run(
        [
            "docker",
            "volume",
            "create",
            "--label",
            f"com.docker.compose.project={project}",
            "--label",
            f"com.docker.compose.volume={VOLUME_KEY}",
            volume_name,
        ],
        capture=True,
    )


def transfer_data(
    *,
    root: Path,
    image: str,
    source_type: str,
    source: str,
    destination_type: str,
    destination: str,
    verify_content: bool,
    baseline_manifest: dict[str, dict] | None = None,
    tolerate_source_changes: bool = False,
) -> dict:
    if destination_type == "bind":
        Path(destination).mkdir(parents=True, exist_ok=True)
    command = _transfer_base_command(
        root=root,
        image=image,
        source_type=source_type,
        source=source,
        source_read_only=True,
        destination_type=destination_type,
        destination=destination,
    )
    baseline_path = None
    if baseline_manifest is not None:
        baseline_path = root / "runtime" / (
            f".docker-backup-baseline-{uuid.uuid4().hex}.json"
        )
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_manifest, sort_keys=True),
            encoding="utf-8",
        )
        command[-1:-1] = [
            "--mount",
            _mount_argument(
                "bind",
                str(baseline_path),
                "/baseline.json",
                read_only=True,
            ),
        ]
    command.extend(
        [
            "/workspace/scripts/docker_data_transfer.py",
            "sync",
            "/source",
            "/destination",
        ]
    )
    if verify_content:
        command.append("--verify-content")
    if baseline_path is not None:
        command.extend(["--baseline-manifest", "/baseline.json"])
    if tolerate_source_changes:
        command.append("--tolerate-source-changes")
    try:
        result = run(command, capture=True)
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        if baseline_path is not None:
            baseline_path.unlink(missing_ok=True)


def verify_data(
    *,
    root: Path,
    image: str,
    data_type: str,
    data_source: str,
) -> dict:
    command = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "--mount",
        _mount_argument("bind", str(root), "/workspace", read_only=True),
        "--mount",
        _mount_argument(data_type, data_source, "/data", read_only=True),
        image,
        "/workspace/scripts/docker_data_transfer.py",
        "verify",
        "/data",
    ]
    result = run(command, capture=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _transfer_base_command(
    *,
    root: Path,
    image: str,
    source_type: str,
    source: str,
    source_read_only: bool,
    destination_type: str,
    destination: str,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "--mount",
        _mount_argument("bind", str(root), "/workspace", read_only=True),
        "--mount",
        _mount_argument(
            source_type,
            source,
            "/source",
            read_only=source_read_only,
        ),
        "--mount",
        _mount_argument(
            destination_type,
            destination,
            "/destination",
            read_only=False,
        ),
        image,
    ]


def _mount_argument(
    mount_type: str,
    source: str,
    destination: str,
    *,
    read_only: bool,
) -> str:
    if "," in source:
        raise ValueError(f"Docker mount source paths cannot contain commas: {source}")
    parts = [
        f"type={mount_type}",
        f"source={source}",
        f"target={destination}",
    ]
    if read_only:
        parts.append("readonly")
    return ",".join(parts)


def stop_container(instance: dict, timeout: int) -> None:
    run(
        ["docker", "stop", "--time", str(timeout), instance["id"]],
        capture=True,
    )


def start_container(instance: dict) -> None:
    run(["docker", "start", instance["id"]], capture=True)


def wait_until_running(container_id: str, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
            capture=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip().lower() == "true":
            return
        time.sleep(1)
    raise RuntimeError(f"Container did not become running within {timeout} seconds.")


def same_path(left: str, right: Path) -> bool:
    if not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False
