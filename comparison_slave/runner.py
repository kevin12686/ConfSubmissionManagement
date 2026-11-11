"""Single-flight background scheduling and bounded subprocess supervision."""

import fcntl
from itertools import islice
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .storage import (
    HEX, Ledger, MAX_RESULT, clients_from_file, namespace_path, private_directory,
    signing_secret,
)

logger = logging.getLogger(__name__)
CACHE_BYTES = 512 * 1024 * 1024
CACHE_ENTRIES = 512


class Service:
    def __init__(self, root, clients_file, *, secret_file=None, start=True,
                 process=None, ledger_options=None):
        self.root = private_directory(root)
        self.clients = clients_from_file(clients_file)
        self.guard = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.probe_requested = threading.Event()
        self.probe_requested.set()
        self.thread = None
        self.ready = False
        self.health_error = "configuration_unavailable"
        self.runtime = None
        self.active = False
        self.preflight_at = 0.0
        self.maintenance_at = 0.0
        self.process = process or self.run_process
        self.backend = os.environ.get("CFM_OCR_BACKEND", "cpu")
        if self.backend not in {"cpu", "cuda"}:
            raise ValueError("Invalid comparison backend")
        try:
            fd = os.open(self.root / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self.guard = os.fdopen(fd, "a+b")
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.secret_file = signing_secret(self.root, secret_file)
            self.ledger = Ledger(self.root, **(ledger_options or {}))
            self.ledger.recover()
            parent = private_directory(self.root / "namespaces")
            # Bound lifetime namespaces, including removed clients, not just this config.
            existing = list(islice(parent.iterdir(), 33))
            names = {p.name for p in existing}
            names.update(namespace_path(self.root, n).name for n in self.clients)
            if len(names) > 32 or any(not HEX.fullmatch(p.name) or p.is_symlink() for p in existing):
                raise ValueError("Worker namespace storage limit exceeded")
            for namespace in self.clients:
                path = private_directory(namespace_path(self.root, namespace))
                private_directory(path / "tmp")
            if start:
                self.thread = threading.Thread(target=self.loop, name="comparison-scheduler", daemon=True)
                self.thread.start()
        except BaseException:
            if self.guard:
                self.guard.close()
            raise

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=280)
            if self.thread.is_alive():
                raise RuntimeError("Comparison scheduler did not stop")
        if self.guard:
            self.guard.close()
            self.guard = None

    def run_process(self, mode, namespace, body=None):
        directory = namespace_path(self.root, namespace)
        environment = os.environ.copy()
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        environment["CFM_SLAVE_ROOT"] = str(self.root)
        environment["TMPDIR"] = str(directory / "tmp")
        environment.setdefault("CFM_RAPIDOCR_MODEL_DIR", str(Path(__file__).resolve().parents[1] / "runtime" / "rapidocr-models"))
        command = [sys.executable, "-B", "-m", "comparison_slave.worker", mode, str(directory), str(self.secret_file)]
        # Files bound stdout independently of the child's behavior; no pipe buffers grow.
        with tempfile.TemporaryFile(dir=directory / "tmp") as source, tempfile.TemporaryFile(dir=directory / "tmp") as output:
            if body is not None:
                source.write(body)
                source.seek(0)
            child = subprocess.Popen(command, stdin=source, stdout=output, stderr=subprocess.DEVNULL,
                                     cwd=Path(__file__).resolve().parents[1], env=environment,
                                     start_new_session=True, pass_fds=(self.guard.fileno(),))
            try:
                try:
                    child.wait(timeout=60 if mode == "preflight" else 270)
                except subprocess.TimeoutExpired:
                    logger.warning("comparison_slave subprocess_timeout mode=%s namespace=%s", mode, namespace)
                    raise
                if child.returncode:
                    logger.warning("comparison_slave subprocess_exit mode=%s namespace=%s code=%s", mode, namespace, child.returncode)
                    raise ValueError("Runner failed")
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
            output.seek(0)
            encoded = output.read(MAX_RESULT + 1)
            if len(encoded) > MAX_RESULT:
                raise ValueError("Runner output exceeds limit")
            payload = json.loads(encoded)
            if not isinstance(payload, dict):
                raise ValueError("Invalid runner output")
            return payload

    def prune_caches(self):
        # No recursive/unbounded walk. Only the signed cache's fixed layouts are eligible.
        for directory in islice((self.root / "namespaces").iterdir(), 32):
            if directory.is_symlink() or not HEX.fullmatch(directory.name):
                continue
            cache = directory / "data" / "pdf_comparison_cache"
            if any(p.is_symlink() for p in (directory / "data", cache)):
                continue
            for kind, pattern in (("entries", r"[dp]-[0-9a-f]{64}\.json"), ("locks", r"[0-9a-f]{64}\.lock")):
                folder = cache / kind
                if not folder.is_dir() or folder.is_symlink():
                    continue
                paths = list(islice(folder.iterdir(), 1025))
                if len(paths) > 1024:
                    raise ValueError("Cache storage needs operator cleanup")
                retained = []
                for path in paths:
                    if re.fullmatch(r"[0-9a-f]{32}\.tmp", path.name) and not path.is_dir():
                        path.unlink(missing_ok=True)
                        continue
                    if path.is_symlink() or not re.fullmatch(pattern, path.name):
                        continue
                    if not path.is_file():
                        continue
                    info = path.stat()
                    if info.st_mtime < time.time() - 7 * 86400:
                        path.unlink(missing_ok=True)
                    else:
                        retained.append((info.st_mtime, info.st_size, path))
                size, count = sum(row[1] for row in retained), len(retained)
                for _, length, path in sorted(retained):
                    if count <= CACHE_ENTRIES and (kind != "entries" or size <= CACHE_BYTES):
                        break
                    path.unlink(missing_ok=True)
                    size -= length
                    count -= 1

    def clean_temporary_jobs(self):
        for directory in islice((self.root / "namespaces").iterdir(), 32):
            folder = directory / "tmp"
            if directory.is_symlink() or folder.is_symlink() or not folder.is_dir():
                raise ValueError("Invalid worker temporary directory")
            jobs = list(islice(folder.iterdir(), 129))
            if len(jobs) > 128:
                raise ValueError("Temporary storage needs operator cleanup")
            for job in jobs:
                if job.is_symlink() or job.is_file():
                    # Native libraries can leave regular scratch files in TMPDIR.
                    # This private directory belongs exclusively to stopped children.
                    job.unlink()
                    continue
                if not job.name.startswith("cfm-ocr-job-") or job.is_symlink() or not job.is_dir():
                    raise ValueError("Unexpected worker temporary artifact")
                files = list(islice(job.iterdir(), 17))
                if len(files) > 16 or any(p.is_dir() and not p.is_symlink() for p in files):
                    raise ValueError("Unexpected worker temporary layout")
                for path in files:
                    path.unlink()
                job.rmdir()

    def tick(self):
        from submissions.services import comparison_protocol
        self.clean_temporary_jobs()
        now = time.monotonic()
        if now >= self.maintenance_at:
            self.ledger.prune()
            self.prune_caches()
            self.maintenance_at = now + 60
        if now >= self.preflight_at and (self.probe_requested.is_set() or self.ledger.busy()):
            self.probe_requested.clear()
            self.ready = False
            try:
                payload = self.process("preflight", next(iter(self.clients)))
                self.ready = (payload.get("contract") == comparison_protocol.contract_id()
                              and payload.get("backend") == self.backend
                              and isinstance(payload.get("runtime"), str)
                              and bool(HEX.fullmatch(payload["runtime"])))
                self.runtime = payload.get("runtime") if self.ready else None
            except Exception as exc:
                self.ready = False
                logger.warning("comparison_slave preflight_exception type=%s", type(exc).__name__)
            self.health_error = "" if self.ready else "configuration_unavailable"
            if not self.ready:
                logger.warning("comparison_slave preflight_failed")
            self.preflight_at = time.monotonic() + (60 if self.ready else 30)
        if not self.ready or self.stop.is_set():
            return
        row = self.ledger.claim()
        if not row:
            return
        self.active = True
        try:
            if row["namespace"] not in self.clients or row["contract"] != comparison_protocol.contract_id():
                self.ledger.finish(row, error="configuration_changed")
                return
            payload = self.process("job", row["namespace"], row["input"])
            if "error" in payload:
                error = payload["error"] if payload["error"] in {"comparison_failed", "comparison_in_progress"} else "comparison_failed"
                self.ledger.finish(row, error=error)
                logger.warning("comparison_slave job_failed namespace=%s key=%s category=%s", row["namespace"], row["key"], error)
                return
            validate_output(payload)
            if payload["result"]["identical_bytes"] != (row["before_hash"] == row["after_hash"]):
                raise ValueError("Incorrect PDF identity")
            if payload["diagnostics"].get("runtime", self.runtime) != self.runtime:
                raise ValueError("Runtime changed during comparison")
            payload["runtime"] = self.runtime
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > MAX_RESULT:
                raise ValueError("Result exceeds limit")
            self.ledger.finish(row, output=encoded)
            if payload["diagnostics"].get("runtime") == self.runtime:
                self.ready = True
                self.health_error = ""
                self.preflight_at = time.monotonic() + 60
        except Exception as exc:
            self.ledger.finish(row, error="comparison_failed")
            logger.warning("comparison_slave job_failed namespace=%s key=%s category=comparison_failed type=%s",
                           row["namespace"], row["key"], type(exc).__name__)
        finally:
            self.active = False

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.ready = False
                self.health_error = "storage_or_scheduler_unavailable"
                logger.warning("comparison_slave scheduler_unavailable type=%s", type(exc).__name__)
                self.preflight_at = time.monotonic() + 30
                self.stop.wait(30)
            self.wake.wait(1)
            self.wake.clear()


def validate_output(payload):
    from submissions.services.rapidocr_comparison.result import validate_result
    if set(payload) != {"result", "diagnostics", "render_hashes"} or not isinstance(payload["diagnostics"], dict):
        raise ValueError("Invalid worker evidence")
    validate_result(payload["result"])
    renders = payload["render_hashes"]
    if not isinstance(renders, dict) or set(renders) != {"before", "after"}:
        raise ValueError("Invalid render hashes")
    for side in ("before", "after"):
        hashes = renders[side]
        if not isinstance(hashes, list) or not 1 <= len(hashes) <= 40 or any(not isinstance(h, str) or not HEX.fullmatch(h) for h in hashes):
            raise ValueError("Invalid render hashes")
        if len(hashes) != len(payload["result"][f"{side}_pages"]):
            raise ValueError("Incomplete render hashes")
