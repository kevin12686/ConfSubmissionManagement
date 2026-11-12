#!/usr/bin/env python3
"""Build, preflight, then start/update only the standalone comparison worker."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "comparison-slave"
MAX_CONFIG_BYTES = 2 * 1024 * 1024


class DeploymentError(RuntimeError):
    pass


def compose_environment():
    # The explicitly selected worker env file, not a conference shell, owns settings.
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("COMPOSE_", "CFM_SLAVE_"))
            and not key.startswith("CFM_RAPIDOCR_")
            and key not in {"CFM_COMPARISON_NETWORK", "CFM_GPU_MODEL_IDLE_SECONDS"}}


def run(command, *, environment, timeout, capture=False):
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, cwd=ROOT, env=environment, timeout=timeout,
                                    stdout=output if capture else None, stderr=errors, check=False)
            if result.returncode:
                # Compose diagnostics can echo interpolated configuration values.
                raise DeploymentError(f"Docker command failed with exit status {result.returncode}; inspect Docker diagnostics without exposing secrets.")
            if not capture:
                return ""
            output.seek(0)
            data = output.read(MAX_CONFIG_BYTES + 1)
            if len(data) > MAX_CONFIG_BYTES:
                raise DeploymentError("Docker configuration response exceeds the safe limit.")
            return data.decode("utf-8")
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError(f"Docker command could not complete ({type(exc).__name__}).") from exc


def validate_config(config, *, gpu):
    if not isinstance(config, dict) or set(config.get("services", {})) != {SERVICE}:
        raise DeploymentError("The worker Compose project must contain only comparison-slave.")
    project = config.get("name", "")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project):
        raise DeploymentError("Set a valid, separate CFM_SLAVE_PROJECT_NAME.")
    service = config["services"][SERVICE]
    backend = service.get("environment", {}).get("CFM_OCR_BACKEND")
    if backend != ("cuda" if gpu else "cpu"):
        raise DeploymentError("Worker backend does not match the explicit CPU/GPU selection.")
    if not service.get("healthcheck", {}).get("test") or service["healthcheck"].get("disable"):
        raise DeploymentError("Worker deployment requires its readiness healthcheck.")
    source = config.get("secrets", {}).get("clients", {}).get("file", "")
    clients = Path(source)
    if not source or not clients.is_absolute() or clients.is_symlink() or not clients.is_file():
        raise DeploymentError("Provision the clients JSON as a regular non-symlink file before deployment.")
    info = clients.stat()
    if info.st_size > 65536 or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077):
        raise DeploymentError("The clients JSON must be at most 64 KiB with private mode 0600.")
    models = [mount for mount in service.get("volumes", []) if mount.get("target") == "/opt/rapidocr-models"]
    if len(models) != 1 or models[0].get("type") != "bind" or not models[0].get("read_only"):
        raise DeploymentError("Worker models require the read-only model-directory bind mount.")
    directory = Path(models[0].get("source", ""))
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise DeploymentError("Provision the approved model directory before deployment; models are never downloaded automatically.")
    return project


def configuration(env_file, *, gpu=False):
    env_file = Path(env_file).expanduser().resolve(strict=True)
    if not env_file.is_file():
        raise DeploymentError("--env-file must name a worker environment file.")
    environment = compose_environment()
    base = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env_file),
            "-f", str(ROOT / "compose.comparison-slave.yml")]
    if gpu:
        base += ["-f", str(ROOT / "compose.comparison-slave.gpu.yml")]
    try:
        config = json.loads(run(base + ["config", "--format", "json"], environment=environment, timeout=30, capture=True))
    except (ValueError, TypeError) as exc:
        raise DeploymentError("Compose returned invalid worker configuration.") from exc
    return config, base, environment


def model_options(config):
    env = config.get("services", {}).get(SERVICE, {}).get("environment", {})
    options = {"det_model": env.get("CFM_RAPIDOCR_DET_MODEL", "small"),
               "rec_model": env.get("CFM_RAPIDOCR_REC_MODEL", "small")}
    if any(value not in {"small", "medium"} for value in options.values()):
        raise DeploymentError("Worker detection/recognition models must be small or medium.")
    bounds = [("CFM_RAPIDOCR_PAGES", 4 if env.get("CFM_OCR_BACKEND") == "cuda" else 2, 1, 8),
              ("CFM_RAPIDOCR_TIMEOUT", 300, 30, 3600)]
    if env.get("CFM_OCR_BACKEND") == "cuda":
        bounds.append(("CFM_GPU_MODEL_IDLE_SECONDS", 1800, 0, 86400))
    for name, default, low, high in bounds:
        try:
            value = int(env.get(name, default))
        except (TypeError, ValueError) as exc:
            raise DeploymentError(f"{name} must be an integer from {low} to {high}.") from exc
        if not low <= value <= high:
            raise DeploymentError(f"{name} must be between {low} and {high}.")
    return options


def deploy(env_file, *, gpu=False, dry_run=False, wait_seconds=240):
    config, base, environment = configuration(env_file, gpu=gpu)
    model_options(config)
    project = validate_config(config, gpu=gpu)
    base += ["--project-name", project]
    existing = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                    "--format", '{{.Label "com.docker.compose.service"}}'],
                   environment=environment, timeout=30, capture=True)
    if any(name.strip() != SERVICE for name in existing.splitlines()):
        raise DeploymentError("Selected project already owns non-worker containers; choose a separate worker project.")
    probe = "cfm-worker-preflight-" + uuid.uuid4().hex
    build = base + ["build", SERVICE]
    check = base + ["run", "--rm", "--no-deps", "-T", "--name", probe,
                    "--entrypoint", "python", "-e", f"CFM_SLAVE_ROOT=/tmp/{probe}",
                    SERVICE, "-m", "comparison_slave.entrypoint", "--preflight"]
    start = base + ["up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", str(wait_seconds), SERVICE]
    print(f"Worker project: {project}; backend: {'cuda' if gpu else 'cpu'}")
    if dry_run:
        for command in (build, check, start):
            print(shlex.join(command))
        print("Dry run: no image build, runtime probe, container start or service change performed.")
        return
    print("Building candidate worker image...")
    run(build, environment=environment, timeout=1800)
    print("Validating private clients and actual model/provider sessions in disposable storage...")
    try:
        run(check, environment=environment, timeout=120)
    except DeploymentError:
        # Killing a Compose CLI does not necessarily kill its one-off container.
        try:
            run(["docker", "rm", "--force", probe], environment=environment, timeout=15, capture=True)
        except DeploymentError:
            pass
        print("Worker preflight failed; the existing worker was not stopped or recreated.", file=sys.stderr)
        raise
    print("Preflight passed; starting worker and waiting for readiness...")
    run(start, environment=environment, timeout=wait_seconds + 60)
    print("Worker deployment is healthy.")
    try:
        details = run(base + ["exec", "-T", SERVICE, "python", "-m", "comparison_slave.entrypoint", "--status"],
                      environment=environment, timeout=10, capture=True)
        print(details.strip())
    except DeploymentError:
        # Diagnostic output must not turn a healthy completed cutover into a failure.
        print("Worker is running; runtime version/settings could not be retrieved. Check Formatting service Details.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--gpu", action="store_true", help="Explicitly use the NVIDIA GPU Compose override.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the plan without building or starting anything.")
    parser.add_argument("--wait-seconds", type=int, default=240)
    args = parser.parse_args(argv)
    if not 30 <= args.wait_seconds <= 600:
        parser.error("--wait-seconds must be between 30 and 600")
    try:
        deploy(args.env_file, gpu=args.gpu, dry_run=args.dry_run, wait_seconds=args.wait_seconds)
        return 0
    except (DeploymentError, OSError, ValueError) as exc:
        print(f"Worker deployment failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
