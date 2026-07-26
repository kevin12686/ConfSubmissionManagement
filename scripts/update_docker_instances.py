#!/usr/bin/env python3
"""Build code and synchronize env-managed Docker Compose instances."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_instance_tools import (  # noqa: E402
    BACKUP_DIR_LABEL,
    ENV_KEYS,
    DockerCommandError,
    compose_command,
    compose_env,
    exclusive_lock,
    inspect_compose_containers,
    matching_instances,
    run,
    wait_until_ready,
)
from rebuild_docker_instances import (  # noqa: E402
    current_project_instance,
    rebuild_instance,
    verify_rebuilt_instance,
)


PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
COMPOSE_INPUT_KEYS = {
    "COMPOSE_PROJECT_NAME",
    "SMS_BIND_HOST",
    "SMS_PORT",
    "SMS_DATA_DIR",
    *ENV_KEYS,
}
PROXY_RECREATE_KEYS = {
    "SMS_BIND_HOST",
    "SMS_PORT",
    "SMS_PROXY_MAX_BODY_SIZE",
}
SENSITIVE_KEYS = {"SMS_SECRET_KEY"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Discover conference .env files, build the current application, "
            "and synchronize existing Docker Compose instances."
        )
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only update the named Compose project. Can be used more than once.",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Create projects declared by env files that do not yet exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show the complete plan without changing containers.",
    )
    args = parser.parse_args()

    root = SCRIPT_DIR.parent
    lock_path = root / "runtime" / ".docker-data-operation.lock"
    try:
        with exclusive_lock(lock_path):
            plans = build_update_plans(
                root,
                selected_projects=set(args.project),
            )
            print_update_plans(plans, create_missing=args.create_missing)
            if args.dry_run:
                return 0
            apply_update_plans(
                plans,
                root,
                create_missing=args.create_missing,
            )
    except (DockerCommandError, OSError, RuntimeError, ValueError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    return 0


def build_update_plans(
    root: Path,
    *,
    selected_projects: set[str],
) -> list[dict]:
    containers = inspect_compose_containers()
    instances = matching_instances(containers, root, set())
    existing_by_project = {instance["project"]: instance for instance in instances}
    all_project_roots = compose_project_roots(containers)
    env_specs = discover_env_specs(root)

    duplicate_projects = sorted(
        project
        for project, paths in group_env_paths_by_project(env_specs).items()
        if len(paths) > 1
    )
    if duplicate_projects:
        details = ", ".join(
            f"{project}: "
            + ", ".join(
                str(path.relative_to(root))
                for path in group_env_paths_by_project(env_specs)[project]
            )
            for project in duplicate_projects
        )
        raise ValueError(f"Multiple env files declare the same project: {details}")

    env_by_project = {spec["project"]: spec for spec in env_specs}
    requested = set(selected_projects)
    known_projects = set(existing_by_project) | set(env_by_project)
    unknown = sorted(requested - known_projects)
    if unknown:
        raise ValueError(
            "Unknown Compose project(s): " + ", ".join(unknown)
        )

    projects = sorted(requested or known_projects)
    plans = []
    with compose_file_precedence():
        for project in projects:
            existing = existing_by_project.get(project)
            env_spec = env_by_project.get(project)
            if env_spec:
                other_root = all_project_roots.get(project)
                if (
                    other_root
                    and not same_resolved_path(other_root, root)
                    and existing is None
                ):
                    raise ValueError(
                        f"{project} already belongs to another checkout: {other_root}"
                    )
                bind_override = bool(
                    existing and existing.get("mount_type") == "bind"
                )
                desired = planned_instance_from_env(
                    root,
                    env_spec["path"],
                    project,
                    bind_override=bind_override,
                )
                if existing:
                    validate_existing_data_mount(existing, desired)
                    changes = environment_changes(existing, desired, root)
                    plans.append(
                        {
                            "action": "update",
                            "project": project,
                            "existing": existing,
                            "desired": desired,
                            "env_path": env_spec["path"],
                            "changes": changes,
                            "force_proxy_recreate": bool(
                                PROXY_RECREATE_KEYS & set(changes)
                            ),
                        }
                    )
                else:
                    plans.append(
                        {
                            "action": "create",
                            "project": project,
                            "existing": None,
                            "desired": desired,
                            "env_path": env_spec["path"],
                            "changes": {},
                            "force_proxy_recreate": True,
                        }
                    )
                continue

            if existing:
                plans.append(
                    {
                        "action": "update_live",
                        "project": project,
                        "existing": existing,
                        "desired": {
                            **existing,
                            "env_values": compose_env(existing),
                        },
                        "env_path": None,
                        "changes": {},
                        "force_proxy_recreate": False,
                    }
                )

    validate_desired_endpoints(plans, instances)
    validate_desired_data_directories(plans, instances)
    return plans


def discover_env_specs(root: Path) -> list[dict]:
    paths = []
    default_env = root / ".env"
    if default_env.is_file():
        paths.append(default_env)
    paths.extend(
        path
        for path in sorted(root.glob(".env.*"))
        if path.name != ".env.example" and path.is_file()
    )
    specs = []
    for path in paths:
        project = read_env_project_name(path)
        specs.append({"path": path, "project": project})
    return specs


def read_env_project_name(path: Path) -> str:
    matches = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip() != "COMPOSE_PROJECT_NAME":
            continue
        try:
            values = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(
                f"{path.name}:{line_number} has an invalid "
                "COMPOSE_PROJECT_NAME value."
            ) from exc
        if len(values) != 1:
            raise ValueError(
                f"{path.name}:{line_number} must define one "
                "COMPOSE_PROJECT_NAME value."
            )
        matches.append(values[0])
    if not matches:
        raise ValueError(
            f"{path.name} is missing COMPOSE_PROJECT_NAME. Add the exact "
            "Docker Compose project name before running the updater."
        )
    if len(matches) > 1:
        raise ValueError(
            f"{path.name} defines COMPOSE_PROJECT_NAME more than once."
        )
    project = matches[0]
    if not PROJECT_NAME_PATTERN.fullmatch(project):
        raise ValueError(
            f"{path.name} has invalid COMPOSE_PROJECT_NAME={project!r}."
        )
    return project


def planned_instance_from_env(
    root: Path,
    env_file: Path,
    project: str,
    *,
    bind_override: bool,
) -> dict:
    result = run(
        compose_command(
            root,
            env_file,
            project,
            "config",
            "--format",
            "json",
            bind_override=bind_override,
        ),
        cwd=root,
        capture=True,
    )
    try:
        payload = json.loads(result.stdout)
        web = payload["services"]["web"]
        proxy = payload["services"]["proxy"]
        port = next(
            value
            for value in proxy["ports"]
            if int(value["target"]) == 80 and value.get("protocol", "tcp") == "tcp"
        )
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{env_file.name} did not produce the expected web/proxy Compose config."
        ) from exc

    web_environment = {
        key: str(value)
        for key, value in (web.get("environment") or {}).items()
    }
    proxy_environment = {
        key: str(value)
        for key, value in (proxy.get("environment") or {}).items()
    }
    env_values = {
        "SMS_BIND_HOST": str(port.get("host_ip") or "0.0.0.0"),
        "SMS_PORT": str(port["published"]),
        "SMS_DATA_DIR": str(
            (web.get("labels") or {}).get(BACKUP_DIR_LABEL, "")
        ),
    }
    for key in ENV_KEYS:
        if key in web_environment:
            env_values[key] = web_environment[key]
        elif key in proxy_environment:
            env_values[key] = proxy_environment[key]
    if not env_values["SMS_DATA_DIR"]:
        raise ValueError(
            f"{env_file.name} did not define the required SMS_DATA_DIR."
        )

    return {
        "project": project,
        "sms_bind_host": env_values["SMS_BIND_HOST"],
        "sms_port": env_values["SMS_PORT"],
        "sms_data_dir": str(
            resolve_env_path(env_values["SMS_DATA_DIR"], root)
        ),
        "env": {
            key: value
            for key, value in env_values.items()
            if key in ENV_KEYS
        },
        "env_values": env_values,
    }


def validate_existing_data_mount(existing: dict, desired: dict) -> None:
    if not same_resolved_path(
        existing["sms_data_dir"],
        Path(desired["sms_data_dir"]),
    ):
        raise ValueError(
            f"{existing['project']}: SMS_DATA_DIR changed from "
            f"{existing['sms_data_dir']} to {desired['sms_data_dir']}. "
            "The unified updater will not redirect conference data or its raw "
            "mirror. Use the documented data migration workflow."
        )


def environment_changes(
    existing: dict,
    desired: dict,
    root: Path,
) -> dict[str, tuple[str, str]]:
    current_values = compose_env(existing)
    desired_values = desired["env_values"]
    changes = {}
    for key in sorted(set(current_values) | set(desired_values)):
        current = str(current_values.get(key, ""))
        target = str(desired_values.get(key, ""))
        if key == "SMS_DATA_DIR" and same_resolved_path(
            resolve_env_path(current, root),
            resolve_env_path(target, root),
        ):
            continue
        if current != target:
            changes[key] = (current_values.get(key, ""), desired_values.get(key, ""))
    return changes


def validate_desired_endpoints(plans: list[dict], instances: list[dict]) -> None:
    endpoints = []
    managed_projects = {plan["project"] for plan in plans}
    for plan in plans:
        desired = plan["desired"]
        endpoints.append(
            (
                plan["project"],
                desired["sms_bind_host"],
                str(desired["sms_port"]),
            )
        )
    for instance in instances:
        if instance["project"] not in managed_projects:
            endpoints.append(
                (
                    instance["project"],
                    instance["sms_bind_host"],
                    str(instance["sms_port"]),
                )
            )
    for index, left in enumerate(endpoints):
        for right in endpoints[index + 1 :]:
            if endpoint_conflicts(left[1:], right[1:]):
                raise ValueError(
                    f"Host endpoint conflict: {left[0]} and {right[0]} both "
                    f"claim port {left[2]} on overlapping bind addresses "
                    f"({left[1]} / {right[1]})."
                )


def endpoint_conflicts(
    left: tuple[str, str],
    right: tuple[str, str],
) -> bool:
    left_host, left_port = left
    right_host, right_port = right
    if left_port != right_port:
        return False
    wildcard_hosts = {"0.0.0.0", "::", "[::]"}
    return (
        left_host == right_host
        or left_host in wildcard_hosts
        or right_host in wildcard_hosts
    )


def validate_desired_data_directories(
    plans: list[dict],
    instances: list[dict],
) -> None:
    owners = {}
    managed_projects = {plan["project"] for plan in plans}
    for instance in instances:
        if instance["project"] in managed_projects:
            continue
        path = str(Path(instance["sms_data_dir"]).resolve())
        owners[path] = instance["project"]
    for plan in plans:
        path = str(Path(plan["desired"]["sms_data_dir"]).resolve())
        other = owners.get(path)
        if other and other != plan["project"]:
            raise ValueError(
                f"SMS_DATA_DIR is shared by {other} and {plan['project']}: {path}"
            )
        owners[path] = plan["project"]


def print_update_plans(plans: list[dict], *, create_missing: bool) -> None:
    if not plans:
        print("No env-managed or existing Docker Compose instances were found.")
        return
    for plan in plans:
        print(f"Project: {plan['project']}")
        if plan["env_path"]:
            print(f"  env file: {plan['env_path'].name}")
        else:
            print("  env file: none; retaining the running container environment")
        if plan["action"] == "create":
            print(
                "  action:   "
                + ("create and verify" if create_missing else "new; not created")
            )
        else:
            print("  action:   build, synchronize, and verify")
        desired = plan["desired"]
        print(
            f"  endpoint: {desired['sms_bind_host']}:{desired['sms_port']}"
        )
        print(f"  data:     {desired['sms_data_dir']}")
        if plan["changes"]:
            print("  environment changes:")
            for key, (before, after) in plan["changes"].items():
                print(
                    f"    {key}: {display_env_value(key, before)} -> "
                    f"{display_env_value(key, after)}"
                )
        elif plan["action"] == "update":
            print("  environment changes: none")
        if plan["force_proxy_recreate"]:
            print("  proxy:    recreate required")


def apply_update_plans(
    plans: list[dict],
    root: Path,
    *,
    create_missing: bool,
) -> None:
    for plan in plans:
        if plan["action"] == "create":
            if not create_missing:
                print(
                    f"Skipped new project {plan['project']}; rerun with "
                    "--create-missing to create it."
                )
                continue
            create_instance(plan, root)
            continue

        with compose_file_precedence():
            rebuild_instance(
                plan["existing"],
                root,
                dry_run=False,
                env_values=plan["desired"]["env_values"],
                force_proxy_recreate=plan["force_proxy_recreate"],
            )


def create_instance(plan: dict, root: Path) -> None:
    env_file = plan["env_path"]
    project = plan["project"]
    print(f"Creating project: {project}")
    with compose_file_precedence():
        run(
            compose_command(
                root,
                env_file,
                project,
                "config",
                "--quiet",
            ),
            cwd=root,
            capture=True,
        )
        run(
            compose_command(
                root,
                env_file,
                project,
                "build",
                "web",
            ),
            cwd=root,
            capture=False,
        )
        run(
            compose_command(
                root,
                env_file,
                project,
                "up",
                "-d",
                "--force-recreate",
            ),
            cwd=root,
            capture=False,
        )
        instance = current_project_instance(root, project)
        if not instance or not instance.get("proxy_id"):
            raise RuntimeError(
                f"{project}: created web/proxy containers were not found."
            )
        wait_until_ready(instance["id"])
        wait_until_ready(instance["proxy_id"])
        verify_rebuilt_instance(
            instance,
            root,
            env_file,
            bind_override=False,
        )
    print(f"  verified: proxy config, static asset, and same-origin CSRF POST")


def group_env_paths_by_project(env_specs: list[dict]) -> dict[str, list[Path]]:
    grouped = {}
    for spec in env_specs:
        grouped.setdefault(spec["project"], []).append(spec["path"])
    return grouped


def compose_project_roots(containers: list[dict]) -> dict[str, str]:
    roots = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project", "")
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        if project and working_dir:
            roots.setdefault(project, working_dir)
    return roots


def display_env_value(key: str, value: object) -> str:
    if key in SENSITIVE_KEYS:
        return "***" if value else "(empty)"
    return str(value) if value not in {None, ""} else "(empty)"


def resolve_env_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def same_resolved_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


@contextmanager
def compose_file_precedence():
    previous = {
        key: os.environ[key]
        for key in COMPOSE_INPUT_KEYS
        if key in os.environ
    }
    try:
        for key in COMPOSE_INPUT_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key in COMPOSE_INPUT_KEYS:
            os.environ.pop(key, None)
        os.environ.update(previous)


if __name__ == "__main__":
    raise SystemExit(main())
