"""One supervised GPU model process, independent of per-conference job/cache children."""
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from submissions.services.rapidocr_comparison.config import timeout_seconds
from submissions.services.rapidocr_comparison.resident_protocol import (
    ENV_KEYS, HEX, MAX_DOCUMENT, MAX_META, MAX_PDF, receive, send,
)

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MODULE = "submissions.services.rapidocr_comparison.resident_worker"


def executable(environment):
    value = environment.get("CFM_RAPIDOCR_PYTHON", sys.executable)
    if not Path(value).is_absolute():
        raise ValueError("GPU inference requires an absolute Python executable")
    return value


def terminate(child):
    if child is None:
        return
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=5)


def checked_metadata(value):
    if (not isinstance(value, dict) or set(value) != {"runtime", "backend"}
            or not isinstance(value["runtime"], str) or not HEX.fullmatch(value["runtime"])
            or not isinstance(value["backend"], dict) or value["backend"].get("backend") != "cuda"):
        raise ValueError("Invalid GPU runtime metadata")
    return value


class Actor:
    def __init__(self, environment, guard_fd):
        self.environment, self.guard_fd = environment, guard_fd
        self.child, self.connection = None, None

    def start(self, deadline, cancelled):
        parent, child = socket.socketpair()
        try:
            self.connection = parent
            parent.setblocking(False)
            self.child = subprocess.Popen([executable(self.environment), "-B", "-m", MODULE, "serve", str(child.fileno()), str(deadline), str(os.getpid())],
                cwd=ROOT, env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(self.guard_fd, child.fileno()))
            self.metadata = checked_metadata(receive(parent, deadline=deadline, cancelled=cancelled))
        finally:
            child.close()

    def call(self, request, *, data=None, deadline, cancelled):
        options = {"deadline": deadline, "cancelled": cancelled}
        send(self.connection, {**request, "deadline": deadline}, **options)
        if data is not None:
            send(self.connection, data, binary=True, limit=MAX_PDF, **options)
        return receive(self.connection, limit=MAX_DOCUMENT if data is not None else MAX_META, **options)

    def alive(self):
        return self.child is not None and self.child.poll() is None

    @staticmethod
    def cold_probe(previous, environment, guard_fd, deadline, cancelled):
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
            source.write(json.dumps(previous, allow_nan=False).encode())
            source.seek(0)
            child = subprocess.Popen([executable(environment), "-B", "-m", MODULE, "probe", str(deadline), str(os.getpid())],
                cwd=ROOT, env=environment, stdin=source, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(guard_fd,))
            try:
                while child.poll() is None:
                    if cancelled() or time.monotonic() >= deadline:
                        raise TimeoutError("GPU metadata probe deadline or cancellation")
                    try:
                        child.wait(timeout=min(.1, max(.001, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        continue
                if child.returncode:
                    raise ValueError("GPU metadata probe failed")
                output.seek(0)
                encoded = output.read(MAX_META + 1)
                if len(encoded) > MAX_META:
                    raise ValueError("GPU metadata exceeds bounds")
                return checked_metadata(json.loads(encoded))
            finally:
                terminate(child)

    def close(self):
        try:
            terminate(self.child)
        finally:
            if self.connection:
                self.connection.close()
            self.connection = None
        self.child = None


class ResidentGPU:
    def __init__(self, environment, guard_fd, idle_seconds, *, actor_type=Actor, clock=time.monotonic):
        self.environment = {key: value for key, value in environment.items()
            if key not in ENV_KEYS and key not in {"DJANGO_SETTINGS_MODULE", "CFM_SLAVE_CLIENTS_FILE", "CFM_SLAVE_SECRET"}
            and not key.startswith(("SMS_", "CFM_COMPARISON_"))}
        self.guard_fd, self.idle_seconds = guard_fd, idle_seconds
        self.actor_type, self.clock = actor_type, clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.actor, self.metadata, self.job_state = None, None, None
        self.last_used = None
        self.loading = False
        self.inference_active = False
        self.failed = False
        # A short private pathname also supports long native checkout paths.
        self.directory = tempfile.TemporaryDirectory(prefix="cfm-gpu-", dir="/tmp")
        self.socket_path = Path(self.directory.name) / "rpc"
        self.listener = socket.socket(socket.AF_UNIX)
        try:
            self.listener.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            self.listener.listen(1)
            self.listener.settimeout(.2)
            self.thread = threading.Thread(target=self._serve, name="gpu-model-broker", daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            self.directory.cleanup()
            raise

    def _cancelled(self):
        return self.stop.is_set() or self.job_state is not None and self.job_state.cancel.is_set()

    def _start(self, deadline):
        self.loading = True
        try:
            self.actor = self.actor_type(self.environment, self.guard_fd)
            self.actor.start(deadline, self._cancelled)
            self.metadata = checked_metadata(self.actor.metadata)
            self.last_used = self.clock()
            self.failed = False
            logger.info("gpu_models_loaded idle_seconds=%s", self.idle_seconds)
            return self.metadata
        finally:
            self.loading = False

    def _discard(self, *, reason, clear=False):
        if self.actor:
            try:
                self.actor.close()
            except BaseException:
                self.metadata, self.failed = None, True
                raise
            self.actor = None
            logger.info("gpu_models_unloaded reason=%s", reason)
        if clear:
            self.metadata = None
            self.failed = True

    def probe(self, *, deadline=None):
        deadline = min(deadline, time.monotonic() + 60) if deadline is not None else time.monotonic() + 60
        with self.lock:
            if self.stop.is_set():
                raise RuntimeError("GPU broker stopped")
            try:
                if self.actor:
                    current = checked_metadata(self.actor.call({"op": "probe"}, deadline=deadline, cancelled=self._cancelled))
                elif self.metadata:
                    current = self.actor_type.cold_probe(self.metadata, self.environment, self.guard_fd, deadline, self._cancelled)
                else:
                    return self._start(deadline)
                if current != self.metadata:
                    self._discard(reason="runtime_changed", clear=True)
                    return self._start(deadline)
                return current
            except BaseException:
                self._discard(reason="probe_failed", clear=True)
                raise

    def expire(self):
        with self.lock:
            if self.actor and not self.actor.alive():
                self._discard(reason="actor_exited", clear=True)
                return
            if (self.actor and self.job_state is None and self.last_used is not None
                    and self.clock() - self.last_used >= self.idle_seconds):
                self._discard(reason="idle")

    def status(self):
        # Observations never block on inference or refresh the idle clock.
        state = "loading" if self.loading else "unavailable" if self.failed else "busy" if self.inference_active else "warm" if self.actor else "cold"
        remaining = max(0, int(self.idle_seconds - (self.clock() - self.last_used))) if self.actor and self.last_used is not None else None
        return {"state": state, "idle_seconds": self.idle_seconds, "idle_remaining_seconds": remaining}

    @contextmanager
    def job(self, body):
        from .worker import read_pair
        hashes = {hashlib.sha256(data).hexdigest() for data in read_pair(io.BytesIO(body))}
        with self.lock:
            if self.job_state is not None or not self.metadata:
                raise RuntimeError("GPU job cannot start")
            job = SimpleNamespace(token=secrets.token_hex(32), hashes=hashes, runtime=self.metadata["runtime"],
                deadline=time.monotonic() + timeout_seconds(), cancel=threading.Event())
            self.job_state = job
        try:
            yield dict(zip(ENV_KEYS, (str(self.socket_path), job.token, job.runtime, str(job.deadline))))
        except BaseException:
            self.invalidate()
            raise
        finally:
            job.cancel.set()
            with self.lock:
                self.job_state = None
            self.expire()

    def invalidate(self):
        if self.job_state:
            self.job_state.cancel.set()
        with self.lock:
            self._discard(reason="job_failed", clear=True)

    def _handle(self, connection):
        request = receive(connection, deadline=time.monotonic() + 2)
        with self.lock:
            job = self.job_state
            if (not isinstance(request, dict) or not job or not isinstance(request.get("token"), str)
                    or not hmac.compare_digest(request["token"], job.token)
                    or request.get("runtime") != job.runtime or job.cancel.is_set()):
                raise ValueError("Unauthorized GPU job")
            deadline, cancelled = job.deadline, self._cancelled
            operation = request.get("op")
            if operation not in {"probe", "extract"}:
                raise ValueError("Unknown GPU operation")
            if operation == "extract" and request.get("sha256") not in job.hashes:
                raise ValueError("GPU PDF is not bound to this job")
            data = None
            if operation == "extract":
                data = receive(connection, limit=MAX_PDF, binary=True, deadline=deadline, cancelled=cancelled)
                if hashlib.sha256(data).hexdigest() != request["sha256"]:
                    raise ValueError("GPU PDF bytes do not match this job")
            try:
                current = self.probe(deadline=deadline) if operation == "probe" else self.metadata
                if operation == "extract" and self.actor is None:
                    current = self._start(deadline)
                if current["runtime"] != job.runtime:
                    raise ValueError("GPU runtime no longer matches job")
                response = {"runtime": job.runtime}
                if operation == "extract":
                    self.inference_active = True
                    try:
                        response = self.actor.call({"op": "extract", "job": job.token, "runtime": job.runtime,
                            "sha256": request["sha256"]}, data=data, deadline=deadline, cancelled=cancelled)
                    finally:
                        self.inference_active = False
                    from submissions.services.rapidocr_comparison.cache import validate_document
                    if response.get("sha256") != request["sha256"] or response.get("runtime") != job.runtime:
                        raise ValueError("GPU inference returned wrong identity")
                    validate_document(response["document"])
                    self.last_used = self.clock()
                send(connection, response, limit=MAX_DOCUMENT, deadline=deadline, cancelled=cancelled)
            except BaseException:
                self._discard(reason="inference_failed", clear=True)
                raise

    def _serve(self):
        while not self.stop.is_set():
            try:
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    self.expire()
                    continue
                with connection:
                    connection.setblocking(False)
                    self._handle(connection)
                self.expire()
            except Exception as exc:
                # No peer-provided messages or tokens enter logs or responses.
                if not self.stop.is_set():
                    logger.warning("gpu_model_request_failed category=%s", type(exc).__name__)

    def close(self):
        self.stop.set()
        if self.job_state:
            self.job_state.cancel.set()
        self.listener.close()
        self.thread.join(timeout=65)
        if self.thread.is_alive():
            raise RuntimeError("GPU broker did not stop")
        with self.lock:
            self._discard(reason="shutdown", clear=True)
        self.directory.cleanup()
