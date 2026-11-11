"""Container-only private-root and file-backed Compose secret initialization."""
import http.client
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from .storage import clients_from_file


def initialize(root, clients_source):
    root, source = Path(root), Path(clients_source)
    for path in (root, source):
        if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Worker root and clients source require absolute non-symlink paths")
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Worker root must be owned by the worker UID")
    root.chmod(0o700)

    # File-backed Compose secrets retain host UID. Only this read-only source
    # may have a different owner; the effective config meets server checks.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError("Clients source must be a private regular file of at most 64 KiB")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Clients source exceeds 64 KiB")

    effective = root / "clients.effective.json"
    if effective.is_symlink():
        raise ValueError("Effective clients config cannot be a symlink")
    fd, name = tempfile.mkstemp(prefix=".clients-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        clients_from_file(temporary)
        temporary.replace(effective)
    finally:
        temporary.unlink(missing_ok=True)
    return effective


def preflight(clients_source):
    """Load actual runtime in a bounded child without touching persistent worker data."""
    from .runner import Service
    from .storage import HEX
    from submissions.services.comparison_protocol import contract_id

    with tempfile.TemporaryDirectory(prefix="cfm-slave-preflight-") as temporary:
        root = Path(temporary).resolve()
        effective = initialize(root, clients_source)
        service = Service(root, effective, start=False)
        try:
            payload = service.run_process("preflight", next(iter(service.clients)))
            if (payload.get("contract") != contract_id() or payload.get("backend") != service.backend
                    or not isinstance(payload.get("runtime"), str) or not HEX.fullmatch(payload["runtime"])):
                raise ValueError("Worker model/provider preflight failed")
        finally:
            service.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ["--healthcheck"]:
        return 0 if healthy() else 1
    if not args:
        print("Worker startup failed: a server command or --preflight is required.", file=sys.stderr)
        return 1
    try:
        preflight(os.environ["CFM_SLAVE_CLIENTS_FILE"])
        if args == ["--preflight"]:
            print("Worker preflight OK: private clients and actual model/provider sessions validated.")
            return 0
        effective = initialize(os.environ["CFM_SLAVE_ROOT"], os.environ["CFM_SLAVE_CLIENTS_FILE"])
        os.environ["CFM_SLAVE_CLIENTS_FILE"] = str(effective)
        os.execvp(args[0], args)
    except Exception as exc:
        print(f"Worker startup preflight failed ({type(exc).__name__}). Check private clients, approved models, runtime dependencies and the selected CPU/CUDA provider.", file=sys.stderr)
        return 1


def healthy():
    """Prime lazy startup without exposing bearer tokens in args or logs."""
    connection = http.client.HTTPConnection("127.0.0.1", 8091, timeout=4)
    try:
        clients = clients_from_file(Path(os.environ["CFM_SLAVE_ROOT"]) / "clients.effective.json")
        token = next(iter(clients.values()))
        connection.request("GET", "/v1/health", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        data = response.read(4097)
        if response.status != 200 or len(data) > 4096:
            return False
        payload = json.loads(data)
        return isinstance(payload, dict) and payload.get("state") in ("ready", "busy")
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return False
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
