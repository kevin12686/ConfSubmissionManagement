#!/usr/bin/env python3
"""Unified, local deployment orchestration. Remote enrollment is an explicit file import."""

import argparse
import hashlib
from contextlib import contextmanager, nullcontext, ExitStack
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import sys
import tempfile
from urllib import request, parse, error

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import deployment_provision
import docker_instance_tools as docker
import start_comparison_slave as slave
from scripts import docker_update_engine as updater

from conference_final_manager.comparison_config import parse_remote_headers
from conference_final_manager.comparison_http import NoRedirect, worker_headers

BOOTSTRAP = "cfm-bootstrap"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
TOKEN = re.compile(r"[!-~]{32,512}\Z")


class DeployError(RuntimeError):
    pass


def audit(command, operation, state, *, error_type=None):
    path = checked_path(ROOT / "runtime/deployment.log")
    record = {"at": datetime.now(timezone.utc).isoformat(), "command": command,
              "operation": operation, "state": state}
    if error_type:
        record["error_type"] = error_type
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise DeployError("Deployment audit log must be a private regular file.")
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def checked_path(path):
    path = Path(os.path.abspath(Path(path).expanduser()))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise DeployError("Deployment configuration paths cannot contain symbolic links.")
    return path


def read_file(path, *, private=False):
    path = checked_path(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 65536:
        raise DeployError("Deployment configuration must be a regular file of at most 64 KiB.")
    if private and os.name != "nt" and path.stat().st_mode & 0o077:
        raise DeployError("Credential/enrollment files require private mode 0600.")
    return path.read_bytes()


def atomic_write(path, data, mode=0o600):
    path = checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def file_changes(changes):
    """Atomic individual replacements with restoration on a reported deployment failure."""
    originals, written = {}, []
    try:
        for path, data in changes.items():
            original = read_file(path)
            originals[path] = (original, path.stat().st_mode & 0o777 if original is not None else 0o600)
            if original != data:
                atomic_write(path, data)
                written.append(path)
        yield
    except BaseException as exc:
        if isinstance(exc, (docker.CutoverStartedError, slave.WorkerCutoverStartedError)):
            print("Container cutover was attempted; retaining its configuration for recovery. Earlier completed updates are not rolled back.", file=sys.stderr)
            raise
        for path in reversed(written):
            if read_file(path) != changes[path]:
                raise DeployError("Configuration changed concurrently; refusing to overwrite it during recovery.")
            original, mode = originals[path]
            if original is None:
                path.unlink()
            else:
                atomic_write(path, original, mode)
        raise


class EnvFile:
    def __init__(self, path):
        self.path = checked_path(path)
        self.original = read_file(self.path)
        self.text = (self.original or b"").decode("utf-8")
        self.values, self.lines = {}, {}
        self.content = self.text.splitlines(keepends=True)
        for index, line in enumerate(self.content):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=([^\r\n]*)\r?\n?", line)
            if not match:
                raise DeployError("Unsupported env syntax; use single-line KEY=value assignments.")
            key, raw = match.groups()
            if key in self.values:
                raise DeployError(f"Duplicate env setting: {key}.")
            if "\\" in raw and not raw.lstrip().startswith(("'", '"')):
                raise DeployError(f"Quote backslash-containing env values explicitly: {key}; no value was rewritten.")
            try:
                parts = shlex.split(raw, comments=True, posix=True)
            except ValueError as exc:
                raise DeployError(f"Unsupported quoted env setting: {key}.") from exc
            value = " ".join(parts)
            self.values[key] = value if raw.lstrip().startswith("'") else value.replace("$$", "$")
            self.lines[key] = index

    def get(self, key, default=""):
        return self.values.get(key, default)

    def require(self, key, value):
        existing = self.get(key)
        if existing and existing != str(value):
            raise DeployError(f"Existing {key} conflicts with the requested configuration; it was not overwritten.")
        self.default(key, value)

    def default(self, key, value):
        if self.get(key):
            return
        value = str(value)
        if any(c in value for c in "\r\n"):
            raise DeployError("Multiline generated settings are not supported.")
        encoded = docker.format_env_line(key, value)
        if not encoded.endswith("\n"):
            encoded += "\n"
        if key in self.lines:
            index = self.lines[key]
            old = self.content[index]
            comment = old[old.index("#"):].rstrip("\r\n") if "#" in old else ""
            self.content[index] = encoded.rstrip("\n") + (" " + comment if comment else "") + "\n"
        else:
            if self.content and not self.content[-1].endswith("\n"):
                self.content[-1] += "\n"
            self.lines[key] = len(self.content)
            self.content.append(encoded)
        self.values[key] = value

    def bytes(self):
        return "".join(self.content).encode()


def namespace(value):
    if not isinstance(value, str) or not NAME.fullmatch(value) or value == BOOTSTRAP:
        raise DeployError("Use a lowercase conference/project slug; the bootstrap namespace is reserved.")
    return value


def valid_token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise DeployError("Existing credential is invalid; credentials are never silently rotated.")
    return value


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeployError("Duplicate JSON configuration key.")
            result[key] = value
        return result
    result = json.loads(data, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise DeployError("Expected a JSON object.")
    return result


def clients_file(path, *, required=False):
    data = read_file(path, private=True)
    if required and data is None:
        raise DeployError(f"Existing worker authorization is missing: {path}. Restore it; update never creates replacement tokens.")
    clients = json_object(data) if data is not None else {}
    if required and not clients:
        raise DeployError("Existing worker authorization is empty; restore its clients instead of initializing new credentials.")
    if len(clients) > 32:
        raise DeployError("Worker supports at most 32 namespaces.")
    for name, token in clients.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise DeployError("Invalid existing worker namespace.")
        valid_token(token)
    if len(set(clients.values())) != len(clients):
        raise DeployError("Worker clients must have distinct credentials.")
    return clients


def endpoint(value):
    parts = parse.urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise DeployError("Enrollment endpoint must be a direct HTTP(S) origin without credentials.")
    try:
        parts.port
    except ValueError as exc:
        raise DeployError("Invalid enrollment endpoint port.") from exc
    if parts.hostname in {"0.0.0.0", "::"}:
        raise DeployError("An advertised endpoint cannot use a wildcard address; supply --endpoint.")
    return value.rstrip("/")


def enrollment(path, project, url=None):
    data = read_file(path, private=True)
    if data is None:
        raise DeployError("Remote enrollment file does not exist.")
    value = json_object(data)
    if set(value) != {"version", "namespace", "endpoint", "token"} or value["version"] != 1:
        raise DeployError("Unsupported remote enrollment format.")
    if value["namespace"] != namespace(project):
        raise DeployError("Remote enrollment namespace must match COMPOSE_PROJECT_NAME.")
    value["endpoint"] = endpoint(value["endpoint"])
    valid_token(value["token"])
    if url and endpoint(url) != value["endpoint"]:
        raise DeployError("--remote-url does not match the explicit trusted enrollment.")
    return value


def worker_env(path=None, *, gpu=None, bind_host=None, initialize=False, template=None):
    env = EnvFile(path or ROOT / "runtime/comparison-slave/worker.env")
    if initialize:
        if env.original is not None:
            raise DeployError(f"Worker configuration already exists: {env.path}. Use update without --init; it was not overwritten.")
        if template:
            source = EnvFile(template)
            if source.original is None:
                raise DeployError("Worker initialization template does not exist.")
            env.content, env.lines, env.values = list(source.content), dict(source.lines), dict(source.values)
    else:
        if template:
            raise DeployError("--template is only allowed with --init, never for an update.")
        if env.original is None:
            raise DeployError(f"Existing worker configuration is missing: {env.path}. Restore it; use --init only for a genuinely new worker.")
        identity = ("CFM_SLAVE_PROJECT_NAME", "CFM_COMPARISON_NETWORK", "CFM_SLAVE_BIND_HOST",
                    "CFM_SLAVE_PORT", "CFM_SLAVE_MODEL_DIR", "CFM_SLAVE_CLIENTS_SOURCE")
        missing = [key for key in identity if not env.get(key)]
        if missing:
            raise DeployError("Existing worker configuration lacks identity settings: " + ", ".join(missing) + ". Restore their original values; no defaults were applied.")
        if not env.get("CFM_DEPLOY_WORKER_BACKEND") and gpu is None:
            raise DeployError("Existing worker backend is missing. Restore CFM_DEPLOY_WORKER_BACKEND, or explicitly select --gpu for an existing CUDA worker.")
    defaults = {"CFM_SLAVE_PROJECT_NAME": "cfm-comparison-slave", "CFM_COMPARISON_NETWORK": "cfm-comparison",
                "CFM_SLAVE_BIND_HOST": "127.0.0.1", "CFM_SLAVE_PORT": "8091", "CFM_SLAVE_MEMORY_LIMIT": "8g",
                "CFM_SLAVE_MODEL_DIR": "./runtime/rapidocr-models",
                "CFM_SLAVE_CLIENTS_SOURCE": "./runtime/comparison-slave/clients.json", "CFM_RAPIDOCR_THREADS": "4"}
    if bind_host is not None:
        ipaddress.ip_address(bind_host)
        env.require("CFM_SLAVE_BIND_HOST", bind_host)
    for key, value in defaults.items():
        env.default(key, value)
    if gpu is not None:
        env.require("CFM_DEPLOY_WORKER_BACKEND", "cuda" if gpu else "cpu")
    env.default("CFM_DEPLOY_WORKER_BACKEND", "cpu")
    if env.get("CFM_DEPLOY_WORKER_BACKEND") not in {"cpu", "cuda"}:
        raise DeployError("Invalid preserved worker backend.")
    if not NAME.fullmatch(env.get("CFM_SLAVE_PROJECT_NAME")):
        raise DeployError("Invalid worker project identity.")
    return env


def configured_path(env, key):
    value = env.get(key)
    if "$" in value:
        raise DeployError(f"{key} needs a literal path for safe automatic provisioning.")
    return checked_path(Path(value) if Path(value).is_absolute() else ROOT / value)


def inspect_worker_containers(env):
    """Include stopped workers and project collisions; observation only."""
    ids = set()
    for label in (f"com.docker.compose.project={env.get('CFM_SLAVE_PROJECT_NAME')}",
                  "com.docker.compose.service=comparison-slave"):
        result = docker.run(["docker", "ps", "-a", "--filter", "label=" + label,
                             "--format", "{{.ID}}"], capture=True)
        ids.update(result.stdout.split())
    containers = json.loads(docker.run(["docker", "inspect", *sorted(ids)], capture=True).stdout) if ids else []
    if not isinstance(containers, list) or any(not isinstance(c, dict) for c in containers):
        raise DeployError("Docker returned invalid worker ownership information; no deployment was authorized.")
    return containers


def validate_worker_identity(env, *, initialize=False):
    """No models, credentials, audit directories or containers are created here."""
    path = configured_path(env, "CFM_SLAVE_CLIENTS_SOURCE")
    marker_path = env.path.parent / "deployed.json"
    marker_bytes = read_file(marker_path)
    marker = json_object(marker_bytes) if marker_bytes is not None else None
    if initialize:
        if read_file(env.path) is not None or read_file(path, private=True) is not None or marker is not None:
            raise DeployError("Worker initialization found existing configuration/authorization/deployment state; restore and update it instead.")
        enrollment_dir = checked_path(env.path.parent / "enrollments")
        if enrollment_dir.exists() and (not enrollment_dir.is_dir() or any(enrollment_dir.iterdir())):
            raise DeployError("Worker initialization found existing enrollments; refusing to generate replacement credentials.")
        volumes = docker.run(["docker", "volume", "ls", "-q", "--filter",
                              f"label=com.docker.compose.project={env.get('CFM_SLAVE_PROJECT_NAME')}"], capture=True)
        if volumes.stdout.strip():
            raise DeployError("Worker initialization found project volumes; refusing to reuse previous worker data implicitly.")
    else:
        clients_file(path, required=True)
        if marker and (marker.get("project") != env.get("CFM_SLAVE_PROJECT_NAME")
                       or marker.get("backend") != env.get("CFM_DEPLOY_WORKER_BACKEND")):
            raise DeployError("Worker project/backend differs from its saved deployment identity; update cannot rename or switch it.")
    containers = inspect_worker_containers(env)
    same_project = [c for c in containers if (c.get("Config", {}).get("Labels", {}) or {}).get("com.docker.compose.project") == env.get("CFM_SLAVE_PROJECT_NAME")]
    if not initialize and len(same_project) > 1:
        raise DeployError("Multiple containers use this worker project, including stopped containers; resolve ownership before update.")
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project")
        mounts = container.get("Mounts", [])
        same_clients = any(m.get("Destination") == "/run/secrets/clients.json" and m.get("Source") == str(path) for m in mounts)
        if project != env.get("CFM_SLAVE_PROJECT_NAME"):
            if same_clients:
                raise DeployError("Worker authorization is mounted by a different project; update cannot create a replacement identity.")
            continue
        if initialize:
            raise DeployError("Worker initialization found an existing or stopped project container; use its existing configuration to update.")
        owner = labels.get("com.docker.compose.project.working_dir")
        if (labels.get("com.docker.compose.service") != "comparison-slave" or not owner
                or Path(owner).resolve() != ROOT.resolve()):
            raise DeployError("Worker project belongs to another checkout/service; refusing to replace it.")
        live_env = dict(item.split("=", 1) for item in container.get("Config", {}).get("Env", []) if "=" in item)
        if live_env.get("CFM_OCR_BACKEND") != env.get("CFM_DEPLOY_WORKER_BACKEND"):
            raise DeployError("Worker backend differs from its existing container, including stopped containers; reconcile it before update.")
        bindings = container.get("HostConfig", {}).get("PortBindings", {}).get("8091/tcp") or []
        if len(bindings) != 1 or bindings[0].get("HostIp") != env.get("CFM_SLAVE_BIND_HOST") or bindings[0].get("HostPort") != env.get("CFM_SLAVE_PORT"):
            raise DeployError("Worker bind address/port differs from its existing container; update cannot guess a different endpoint.")
        client_mounts = [m for m in mounts if m.get("Destination") == "/run/secrets/clients.json"]
        data_mounts = [m for m in mounts if m.get("Destination") == "/worker-data"]
        if (len(client_mounts) != 1 or not same_clients or len(data_mounts) != 1
                or data_mounts[0].get("Type") != "volume"
                or data_mounts[0].get("Name") != project + "_worker_data"):
            raise DeployError("Worker authorization/data mounts differ from its preserved configuration; reconcile the existing deployment first.")


def worker_status(env, clients):
    result = docker.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={env.get('CFM_SLAVE_PROJECT_NAME')}",
                         "--filter", "label=com.docker.compose.service=comparison-slave", "--format", "{{.ID}}"], capture=True)
    if not result.stdout.strip():
        return "absent"
    identifiers = result.stdout.split()
    if len(identifiers) != 1:
        raise DeployError("Multiple workers use this project identity; refusing automatic management.")
    info = json.loads(docker.run(["docker", "inspect", identifiers[0]], capture=True).stdout)
    if not isinstance(info, list) or len(info) != 1 or not isinstance(info[0], dict):
        raise DeployError("Cannot validate worker ownership; no automatic changes were authorized.")
    labels = info[0].get("Config", {}).get("Labels", {}) or {}
    directory = labels.get("com.docker.compose.project.working_dir")
    if not directory or Path(directory).resolve() != ROOT.resolve():
        raise DeployError("The shared worker belongs to another checkout; manage it from its owner instead of replacing it.")
    if not clients:
        raise DeployError("A worker is running but its private clients configuration is missing; refusing to replace its identity.")
    host = env.get("CFM_SLAVE_BIND_HOST")
    host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    host = f"[{host}]" if ":" in host else host
    url = f"http://{host}:{env.get('CFM_SLAVE_PORT')}/v1/health"
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        req = request.Request(url, headers=worker_headers(next(iter(clients.values()))))
        with opener.open(req, timeout=5) as response:
            data = response.read(8193)
            if response.status != 200 or len(data) > 8192:
                raise ValueError
        health = json_object(data)
        if health.get("protocol") != 1 or health.get("state") not in {"ready", "busy", "not_ready"}:
            raise ValueError
        if health.get("backend") != env.get("CFM_DEPLOY_WORKER_BACKEND"):
            raise DeployError("Running worker backend differs from preserved configuration; explicitly reconcile it before deployment.")
        return health["state"]
    except (error.URLError, OSError, ValueError) as exc:
        raise DeployError("Cannot authenticate/check the running worker; refusing configuration changes or restart.") from exc


def worker_fingerprint(env):
    files = [ROOT / name for name in ("Dockerfile.comparison-slave", "requirements-slave-gpu.txt",
                                     "compose.comparison-slave.yml", "compose.comparison-slave.gpu.yml", "submissions/version.py")]
    files += sorted((ROOT / "comparison_slave").glob("*.py"))
    services = ROOT / "submissions/services"
    files += [services / name for name in ("comparison_protocol.py", "pdf_comparison_worker.py",
              "pdf_comparison_execution.py", "pdf_comparison_errors.py", "pdf_comparison_cache.py", "comparison_metadata.py")]
    files += sorted((services / "rapidocr_comparison").glob("*.py"))
    digest = hashlib.sha256(json.dumps(env.values, sort_keys=True).encode())
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {"version": 1, "project": env.get("CFM_SLAVE_PROJECT_NAME"),
            "backend": env.get("CFM_DEPLOY_WORKER_BACKEND"), "fingerprint": digest.hexdigest()}


def worker_model_options(env):
    # Resolve the actual Compose values, including defaults edited in Compose.
    # Do not save new credentials/configuration before provisioning succeeds.
    with docker.temporary_env_file(env.values) as path:
        config, _, _ = slave.configuration(path, gpu=env.get("CFM_DEPLOY_WORKER_BACKEND") == "cuda")
    return slave.model_options(config)


def validate_worker_authorization(env, additions, *, initialize=False, new_clients=()):
    clients = clients_file(configured_path(env, "CFM_SLAVE_CLIENTS_SOURCE"), required=not initialize)
    if len(set(clients) | set(additions)) > 32:
        raise DeployError("Adding clients would exceed the worker's 32-namespace limit.")
    for name, proposed in additions.items():
        namespace(name)
        if proposed:
            valid_token(proposed)
        if name in clients and proposed and clients[name] != proposed:
            raise DeployError("Conference and worker credentials disagree; existing tokens were not changed.")
        if name not in clients and not initialize and name not in new_clients:
            raise DeployError(f"Existing worker authorization has no entry for {name}. Restore it; update never reconstructs existing conference tokens.")
        if name not in clients and (env.path.parent / "enrollments" / (name + ".json")).exists():
            raise DeployError("An existing enrollment has lost its worker authorization; restore the original entry instead of generating a new token.")
    return clients


def ensure_worker(env, additions, *, offline=False, dry_run=False, force=False, exports=None,
                  initialize=False, new_clients=(), wait_seconds=240):
    validate_env_snapshot(env)
    validate_worker_identity(env, initialize=initialize)
    path = configured_path(env, "CFM_SLAVE_CLIENTS_SOURCE")
    original_bytes = read_file(path, private=True)
    clients = validate_worker_authorization(env, additions, initialize=initialize, new_clients=new_clients)
    original_clients = dict(clients)
    for name, proposed in additions.items():
        token = clients.get(name) or proposed or ("DRY_RUN_CREDENTIAL_NOT_WRITTEN_" + name if dry_run else secrets.token_urlsafe(32))
        if token in clients.values() and clients.get(name) != token:
            raise DeployError("A credential is already assigned to another worker namespace.")
        clients[name] = token
    if not clients:
        if not initialize:
            raise DeployError("Update requires existing worker authorization.")
        clients[BOOTSTRAP] = "DRY_RUN_BOOTSTRAP_NOT_WRITTEN_0000" if dry_run else secrets.token_urlsafe(32)
    if len(clients) > 32:
        raise DeployError("Adding clients would exceed the worker's 32-namespace limit.")
    marker_path = env.path.parent / "deployed.json"
    expected_marker = worker_fingerprint(env)
    marker = read_file(marker_path)
    needs_update = marker is None or json_object(marker) != expected_marker
    changed = clients != original_clients or env.original is None or needs_update
    models = worker_model_options(env)
    if dry_run:
        deployment_provision.provision_models(configured_path(env, "CFM_SLAVE_MODEL_DIR"), offline=offline, dry_run=True, **models)
        action = "Initialize" if initialize else "Update"
        print(f"{action} shared {env.get('CFM_DEPLOY_WORKER_BACKEND')} worker {env.get('CFM_SLAVE_PROJECT_NAME')}: {env.path}.")
        print(f"  {len(additions)} selected conference namespace(s); existing credentials retained. Any worker restart affects all clients on this shared worker.")
        return clients
    state = "absent" if initialize else worker_status(env, original_clients)
    if state == "busy" and (changed or force):
        raise DeployError("Worker is busy across its namespaces; retry during maintenance with no new uploads. No worker config was changed.")
    deployment_provision.provision_models(configured_path(env, "CFM_SLAVE_MODEL_DIR"), offline=offline, dry_run=False, **models)
    validate_env_snapshot(env)
    if read_file(path, private=True) != original_bytes:
        raise DeployError("Worker authorization changed during planning/provisioning; no stale credentials were written.")
    changes = {env.path: env.bytes()}
    if clients != original_clients or read_file(path) is None:
        changes[path] = (json.dumps(clients, indent=2) + "\n").encode()
    if exports:
        for name, (export_path, advertised) in exports.items():
            value = {"version": 1, "namespace": name, "endpoint": advertised, "token": clients[name]}
            old = read_file(export_path, private=True)
            if old is not None and json_object(old) != value:
                raise DeployError("Existing enrollment differs; it was not overwritten.")
            changes[export_path] = (json.dumps(value, indent=2) + "\n").encode()
    with file_changes(changes):
        if changed or force or state not in {"ready", "busy"}:
            print(f"{'Initialize' if initialize else 'Update'} worker {env.get('CFM_SLAVE_PROJECT_NAME')}: {env.path}.")
            slave.deploy(env.path, gpu=env.get("CFM_DEPLOY_WORKER_BACKEND") == "cuda", wait_seconds=wait_seconds)
            atomic_write(marker_path, (json.dumps(expected_marker, indent=2) + "\n").encode())
        else:
            print("Reusing the existing shared worker; no restart requested.")
    return clients


def conference_plan(env_file, project):
    containers = updater.inspect_compose_containers()
    instances = updater.matching_instances(containers, ROOT, set())
    existing = next((item for item in instances if item["project"] == project), None)
    other = updater.compose_project_roots(containers).get(project)
    if other and not updater.same_resolved_path(other, ROOT):
        raise DeployError("Conference project belongs to another checkout.")
    with updater.compose_file_precedence():
        desired = updater.planned_instance_from_env(ROOT, env_file, project,
                                                   bind_override=bool(existing and existing["mount_type"] == "bind"))
    if existing:
        updater.validate_existing_data_mount(existing, desired)
    changes = updater.environment_changes(existing, desired, ROOT) if existing else {}
    plan = {"action": "update" if existing else "create", "project": project, "existing": existing,
            "desired": desired, "env_path": env_file, "changes": changes,
            "force_proxy_recreate": not existing or bool(updater.PROXY_RECREATE_KEYS & set(changes))}
    updater.validate_desired_endpoints([plan], instances)
    updater.validate_desired_data_directories([plan], instances)
    return plan


def select_conference_env(project, explicit, *, dry_run):
    matches = [spec["path"] for spec in updater.discover_env_specs(ROOT) if spec["project"] == project]
    if len(matches) > 1:
        raise DeployError("Multiple managed env files declare this project; resolve the duplicate without changing its identity.")
    if explicit:
        selected = checked_path(explicit)
        if matches and selected != checked_path(matches[0]):
            raise DeployError("This project already has a different managed env file; use that file explicitly.")
    else:
        selected = checked_path(matches[0] if matches else ROOT / f".env.{project}")
    if not selected.exists() and not dry_run:
        containers = updater.inspect_compose_containers()
        if project in updater.compose_project_roots(containers):
            raise DeployError("An existing project has no selected env file; provide --env-file with its existing configuration.")
    return selected


def ensure_network(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise DeployError("Invalid preserved comparison network name.")
    result = docker.run(["docker", "network", "inspect", name], capture=True, check=False)
    if result.returncode:
        docker.run(["docker", "network", "create", name], capture=True)


def validate_compute_config(values):
    """Reject invalid per-conference connections before shared infrastructure changes."""
    try:
        parse_remote_headers(values.get("CFM_COMPARISON_REMOTE_HEADERS", ""), url=values.get("CFM_COMPARISON_REMOTE_URL", ""))
    except ValueError as exc:
        raise DeployError(str(exc)) from None
    transport = values.get("CFM_COMPARISON_TRANSPORT", "service")
    if transport not in {"service", "embedded"}:
        raise DeployError("Unknown comparison transport.")
    if transport == "embedded":
        return
    for name in ("LOCAL", "REMOTE"):
        url = values.get(f"CFM_COMPARISON_{name}_URL", "").strip()
        token = values.get(f"CFM_COMPARISON_{name}_TOKEN", "").strip()
        if url:
            try:
                endpoint(url)
                valid_token(token)
            except (ValueError, DeployError):
                raise DeployError(f"Invalid {name.lower()} worker URL/token; no credential was changed.") from None
        elif token:
            raise DeployError(f"{name.title()} worker token is configured without its URL.")
    if not any(values.get(f"CFM_COMPARISON_{name}_URL", "").strip() for name in ("LOCAL", "REMOTE")):
        raise DeployError("Service mode needs at least one worker endpoint.")


def validate_env_snapshot(env):
    if read_file(env.path) != env.original:
        raise DeployError(f"{env.path.name} changed during deployment planning; no stale configuration was written. Preview again.")


def prepare_conference(args, *, existing=None, effective_values=None, updating=False):
    """Prepare configuration in memory; never provision or mutate containers."""
    project = namespace(args.name)
    env = EnvFile(select_conference_env(project, args.env_file, dry_run=args.dry_run))
    env.require("COMPOSE_PROJECT_NAME", project)
    # Compose owns interpolation. Reuse its structured effective values rather
    # than interpreting `$VAR` ourselves or re-expanding literal credentials.
    if effective_values is None and env.original is not None:
        with updater.compose_file_precedence():
            effective_values = updater.planned_instance_from_env(ROOT, env.path, project,
                bind_override=bool(existing and existing["mount_type"] == "bind"))["env_values"]
    for key, value in (effective_values or {}).items():
        if env.get(key):
            env.values[key] = value
    if existing:
        live = existing.get("env", {})
        # Missing env entries do not authorize rotating a running signing key
        # or dropping an already configured worker connection. Explicit values
        # (including an intentionally blank URL/header setting) remain owned by
        # the operator; tokens are retained only for the same endpoint URL.
        env.default("SMS_SECRET_KEY", live.get("SMS_SECRET_KEY", ""))
        for key in ("CFM_COMPARISON_TRANSPORT", "CFM_COMPARISON_NETWORK"):
            if key not in env.values and live.get(key):
                env.default(key, live[key])
        for name in ("LOCAL", "REMOTE"):
            url_key, token_key = f"CFM_COMPARISON_{name}_URL", f"CFM_COMPARISON_{name}_TOKEN"
            if url_key not in env.values and live.get(url_key):
                env.default(url_key, live[url_key])
            if env.get(url_key) == live.get(url_key) and live.get(token_key):
                env.default(token_key, live[token_key])
        key = "CFM_COMPARISON_REMOTE_HEADERS"
        if key not in env.values and env.get("CFM_COMPARISON_REMOTE_URL") == live.get("CFM_COMPARISON_REMOTE_URL") and live.get(key):
            env.default(key, live[key])
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise DeployError("Conference port must be between 1 and 65535.")
        env.require("SMS_PORT", args.port)
    for key, value in {"SMS_PORT": "8000", "SMS_BIND_HOST": "127.0.0.1", "SMS_DATA_DIR": f"./runtime/{project}",
                       "SMS_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver", "SMS_DEBUG": "0",
                       "CFM_COMPARISON_TRANSPORT": "service"}.items():
        env.default(key, value)
    if updating and not env.get("SMS_SECRET_KEY"):
        raise DeployError("Existing conference signing secret is missing from both configuration and live state; restore it instead of generating a new one.")
    if args.remote_enrollment:
        remote = enrollment(args.remote_enrollment, project, args.remote_url)
        env.require("CFM_COMPARISON_REMOTE_URL", remote["endpoint"])
        env.require("CFM_COMPARISON_REMOTE_TOKEN", remote["token"])
    elif args.remote_url:
        raise DeployError("--remote-url requires an explicit private --remote-enrollment file.")
    try:
        parse_remote_headers(env.get("CFM_COMPARISON_REMOTE_HEADERS"), url=env.get("CFM_COMPARISON_REMOTE_URL"))
    except ValueError as exc:
        raise DeployError(str(exc)) from None
    transport = env.get("CFM_COMPARISON_TRANSPORT")
    if transport not in {"service", "embedded"}:
        raise DeployError("Unknown preserved comparison transport.")
    remote_only = ("CFM_COMPARISON_LOCAL_URL" in env.values and not env.get("CFM_COMPARISON_LOCAL_URL")
                   and bool(env.get("CFM_COMPARISON_REMOTE_URL")))
    worker = None
    if transport == "service" and not remote_only:
        local_url = "http://comparison-slave:8091"
        if env.get("CFM_COMPARISON_LOCAL_URL") not in {"", local_url}:
            valid_token(env.get("CFM_COMPARISON_LOCAL_TOKEN"))
        else:
            worker_path = ROOT / "runtime/comparison-slave/worker.env"
            initialize = not updating and not worker_path.exists()
            if initialize and env.get("CFM_COMPARISON_LOCAL_TOKEN"):
                raise DeployError("Conference already declares a local worker credential but worker.env is missing; restore that worker, do not initialize a replacement.")
            if initialize:
                instances = updater.matching_instances(updater.inspect_compose_containers(), ROOT, set())
                if any(i.get("env", {}).get("CFM_COMPARISON_TRANSPORT", "service") == "service"
                       and i.get("env", {}).get("CFM_COMPARISON_LOCAL_URL") == "http://comparison-slave:8091" for i in instances):
                    raise DeployError("Existing conferences use this shared worker but worker.env is missing; restore it before creating another conference.")
            worker = worker_env(initialize=initialize)
            validate_worker_identity(worker, initialize=worker.original is None)
            env.require("CFM_COMPARISON_NETWORK", worker.get("CFM_COMPARISON_NETWORK"))
            proposed = env.get("CFM_COMPARISON_LOCAL_TOKEN")
            clients = validate_worker_authorization(worker, {project: proposed}, initialize=worker.original is None,
                                                    new_clients=() if updating else (project,))
            token = proposed or clients.get(project) or ("DRY_RUN_CREDENTIAL_NOT_WRITTEN_" + project if args.dry_run else secrets.token_urlsafe(32))
            env.default("CFM_COMPARISON_LOCAL_URL", local_url)
            env.require("CFM_COMPARISON_LOCAL_TOKEN", valid_token(token))
    if not env.get("SMS_SECRET_KEY"):
        env.default("SMS_SECRET_KEY", "DRY_RUN_SECRET_NOT_WRITTEN" if args.dry_run else secrets.token_urlsafe(48))
    validate_compute_config(env.values)
    return env, worker


def prepare_conference_deployment(args):
    instances = updater.matching_instances(updater.inspect_compose_containers(), ROOT, {args.name})
    existing = next((item for item in instances if item["project"] == args.name), None)
    env, worker = prepare_conference(args, existing=existing, updating=existing is not None)
    return env, worker, existing


def conference(args, prepared):
    env, worker, existing = prepared
    project = env.get("COMPOSE_PROJECT_NAME")
    transport = env.get("CFM_COMPARISON_TRANSPORT")
    if args.dry_run:
        if worker:
            ensure_worker(worker, {project: env.get("CFM_COMPARISON_LOCAL_TOKEN")}, offline=args.offline, dry_run=True,
                          initialize=worker.original is None, new_clients=() if existing else (project,))
        elif transport == "embedded":
            deployment_provision.provision_models(ROOT / "runtime/rapidocr-models", offline=args.offline, dry_run=True)
        print(f"Would preserve/fill {env.path.name} and create/update conference {project}; no credentials printed or written.")
        return
    # Validate identity, ports and data mounts before enrolling a client or changing files.
    with docker.temporary_env_file(env.values) as preview:
        plan = conference_plan(preview, project)
    plan["env_path"] = env.path
    validate_env_snapshot(env)
    if worker:
        ensure_worker(worker, {project: env.get("CFM_COMPARISON_LOCAL_TOKEN")}, offline=args.offline,
                      initialize=worker.original is None, new_clients=() if existing else (project,))
    else:
        if transport == "embedded":
            deployment_provision.provision_models(ROOT / "runtime/rapidocr-models", offline=args.offline)
        ensure_network(env.get("CFM_COMPARISON_NETWORK", "cfm-comparison"))
    validate_env_snapshot(env)
    with file_changes({env.path: env.bytes()}):
        updater.print_update_plans([plan], create_missing=True)
        updater.apply_update_plans([plan], ROOT, create_missing=True)


def prepare_worker_deployment(args):
    env = worker_env(args.env_file, gpu=True if args.gpu else None, bind_host=args.bind_host,
                     initialize=args.init, template=args.template)
    validate_worker_identity(env, initialize=args.init)
    names = list(dict.fromkeys(namespace(value) for value in args.client))
    clients = validate_worker_authorization(env, dict.fromkeys(names, ""), initialize=args.init, new_clients=names)
    if args.enrollment_out and len(names) != 1:
        raise DeployError("--enrollment-out requires exactly one --client.")
    exports = {}
    if names:
        host = env.get("CFM_SLAVE_BIND_HOST")
        host = f"[{host}]" if ":" in host else host
        advertised = endpoint(args.endpoint or f"http://{host}:{env.get('CFM_SLAVE_PORT')}")
        for name in names:
            path = checked_path(args.enrollment_out or ROOT / f"runtime/comparison-slave/enrollments/{name}.json")
            old = read_file(path, private=True)
            if old is not None and (name not in clients or json_object(old) != {
                    "version": 1, "namespace": name, "endpoint": advertised, "token": clients[name]}):
                raise DeployError("Existing enrollment differs or has lost worker authorization; restore it before update. No token was generated.")
            exports[name] = (path, advertised)
    return env, names, exports


def worker(args, prepared):
    env, names, exports = prepared
    ensure_worker(env, dict.fromkeys(names, ""), offline=args.offline, dry_run=args.dry_run, force=True, exports=exports,
                  initialize=args.init, new_clients=names, wait_seconds=args.wait_seconds)
    for path, _ in exports.values():
        print(f"{'Would export' if args.dry_run else 'Private enrollment'}: {path}")


def prepare_update_deployment(args):
    plans = updater.build_update_plans(ROOT, selected_projects=set(args.project))
    prepared, worker, additions = {}, None, {}
    new_clients = set()
    active = []
    for plan in plans:
        if plan["action"] == "create" and not args.create_missing:
            continue
        active.append(plan)
        if plan["env_path"]:
            env, local = prepare_conference(argparse.Namespace(
                name=plan["project"], port=None, env_file=plan["env_path"],
                remote_url=None, remote_enrollment=None, dry_run=args.dry_run,
            ), existing=plan["existing"], effective_values=plan["desired"]["env_values"],
                updating=plan["action"] != "create")
            prepared[plan["project"]] = env
            if local:
                worker = local
                additions[plan["project"]] = env.get("CFM_COMPARISON_LOCAL_TOKEN")
                if plan["action"] == "create":
                    new_clients.add(plan["project"])
        else:
            values = plan["desired"]["env_values"]
            validate_compute_config(values)
            # Retain a legacy instance's live environment, never invent a new
            # managed env file or replace its deployment identity.
            if values.get("CFM_COMPARISON_TRANSPORT", "service") == "service":
                url = values.get("CFM_COMPARISON_LOCAL_URL", "")
                token = values.get("CFM_COMPARISON_LOCAL_TOKEN", "")
                if url == "http://comparison-slave:8091" and token:
                    worker = worker_env()
                    validate_worker_identity(worker)
                    clients = clients_file(configured_path(worker, "CFM_SLAVE_CLIENTS_SOURCE"), required=True)
                    if clients.get(plan["project"]) != token:
                        raise DeployError("Legacy conference authorization is missing or differs from its worker; restore it before update.")
                    if values.get("CFM_COMPARISON_NETWORK", "cfm-comparison") != worker.get("CFM_COMPARISON_NETWORK"):
                        raise DeployError("Running conference and shared worker network differ; reconcile the existing configuration first.")
                    additions[plan["project"]] = valid_token(token)
                elif not url and not values.get("CFM_COMPARISON_REMOTE_URL"):
                    raise DeployError("A legacy instance without worker configuration needs its existing managed env file before upgrade; no identity was invented.")

    return plans, prepared, worker, additions, active, new_clients


def update(args, setup):
    plans, prepared, worker, additions, active, new_clients = setup
    # Re-evaluate filled defaults through Compose, retaining every data/port
    # guard before any worker or conference configuration is committed.
    with ExitStack() as staging, updater.compose_file_precedence():
        for plan in active:
            env = prepared.get(plan["project"])
            if not env:
                continue
            path = staging.enter_context(docker.temporary_env_file(env.values))
            existing = plan["existing"]
            desired = updater.planned_instance_from_env(ROOT, path, plan["project"],
                bind_override=bool(existing and existing["mount_type"] == "bind"))
            if existing:
                updater.validate_existing_data_mount(existing, desired)
            plan["desired"] = desired
            plan["changes"] = updater.environment_changes(existing, desired, ROOT) if existing else {}
            plan["force_proxy_recreate"] = not existing or bool(updater.PROXY_RECREATE_KEYS & set(plan["changes"]))
        instances = updater.matching_instances(updater.inspect_compose_containers(), ROOT, set())
        updater.validate_desired_endpoints(active, instances)
        updater.validate_desired_data_directories(active, instances)
        updater.print_update_plans(plans, create_missing=args.create_missing)
        for env in prepared.values():
            validate_env_snapshot(env)
        if worker:
            ensure_worker(worker, additions, offline=args.offline, dry_run=args.dry_run,
                          initialize=worker.original is None, new_clients=new_clients)
        embedded = any(p["desired"]["env_values"].get("CFM_COMPARISON_TRANSPORT", "service") == "embedded" for p in active)
        if embedded:
            deployment_provision.provision_models(ROOT / "runtime/rapidocr-models", offline=args.offline, dry_run=args.dry_run)
        if args.dry_run:
            for env in prepared.values():
                original = EnvFile(env.path)
                keys = sorted(k for k, v in env.values.items() if v and not original.get(k))
                if keys:
                    print(f"  {env.path.name}: fill missing settings: {', '.join(keys)} (values withheld).")
            print("Dry-run: configuration/Compose plans checked; no persistent files, models or containers changed. Live HTTP/runtime checks run before cutover during apply.")
            return
        for plan in active:
            values = plan["desired"]["env_values"]
            if not worker or values.get("CFM_COMPARISON_NETWORK", "cfm-comparison") != worker.get("CFM_COMPARISON_NETWORK"):
                ensure_network(values.get("CFM_COMPARISON_NETWORK", "cfm-comparison"))
        # Commit each env only around its own cutover; a later failure must not
        # restore settings for an earlier conference that was already updated.
        for plan in active:
            env = prepared.get(plan["project"])
            if env:
                validate_env_snapshot(env)
            with file_changes({env.path: env.bytes()} if env else {}):
                updater.apply_update_plans([plan], ROOT, create_missing=args.create_missing)
        for plan in plans:
            if plan not in active:
                print(f"Skipped new project {plan['project']}; use --create-missing to authorize creation.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--offline", action="store_true")
    w = sub.add_parser("worker", parents=[common])
    w.add_argument("--init", action="store_true", help="Initialize a genuinely new worker; never overwrite existing state.")
    w.add_argument("--template", type=Path, help="Initial settings template, only with --init and an absent target env.")
    w.add_argument("--gpu", action="store_true")
    w.add_argument("--bind-host")
    w.add_argument("--env-file", type=Path)
    w.add_argument("--client", action="append", default=[])
    w.add_argument("--endpoint")
    w.add_argument("--enrollment-out", type=Path)
    w.add_argument("--wait-seconds", type=int, default=240)
    c = sub.add_parser("conference", parents=[common])
    c.add_argument("--name", required=True)
    c.add_argument("--port", type=int)
    c.add_argument("--env-file", type=Path)
    c.add_argument("--remote-url")
    c.add_argument("--remote-enrollment", type=Path)
    u = sub.add_parser("update", parents=[common])
    u.add_argument("--project", action="append", default=[])
    u.add_argument("--create-missing", action="store_true", help="Also create new conferences declared by env files.")
    args = parser.parse_args(argv)
    try:
        if args.command == "worker" and not 30 <= args.wait_seconds <= 600:
            raise DeployError("--wait-seconds must be between 30 and 600.")
        # Resolve lifecycle/configuration before even creating the operation lock
        # or audit directory. Missing update identity must be a read-only failure.
        prepare = {"worker": prepare_worker_deployment, "conference": prepare_conference_deployment,
                   "update": prepare_update_deployment}[args.command]
        prepared = prepare(args)
        lock = nullcontext() if args.dry_run else docker.exclusive_lock(ROOT / "runtime/.docker-data-operation.lock")
        with lock:
            action = {"worker": worker, "conference": conference, "update": update}[args.command]
            if args.dry_run:
                action(args, prepared)
                return 0
            operation = secrets.token_hex(16)
            try:
                audit(args.command, operation, "requested")
            except (OSError, ValueError, DeployError) as exc:
                raise DeployError("Could not record deployment request; no deployment actions were started.") from exc
            try:
                action(args, prepared)
            except Exception as exc:
                try:
                    audit(args.command, operation, "failed", error_type=type(exc).__name__)
                except (OSError, ValueError, DeployError):
                    print("Deployment failed and its failure audit could not be written; inspect state before retrying.", file=sys.stderr)
                raise
            try:
                audit(args.command, operation, "success")
            except (OSError, ValueError, DeployError):
                print("Deployment completed, but success audit failed; deployment side effects were not rolled back.", file=sys.stderr)
                return 1
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, (DeployError, deployment_provision.ProvisionError, slave.DeploymentError,
                            docker.CutoverStartedError, updater.UpdatePlanError)):
            print(f"Deployment stopped: {exc}", file=sys.stderr)
        elif isinstance(exc, docker.DockerCommandError):
            stage = next((name for name in ("build", "run", "up", "config", "exec") if name in exc.command), "operation")
            print(f"Deployment stopped: Docker {stage} exited with status {exc.returncode}. See the preceding build/preflight/cutover output; earlier completed updates remain applied.", file=sys.stderr)
        else:
            print(f"Deployment stopped ({type(exc).__name__}); existing credentials were not silently replaced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
