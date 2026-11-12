"""Private, bounded GPU IPC. No cache paths, Django settings or publication data."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import time

MAX_META = 65536
# Full OCR is not subject to the optional 4-MiB document-cache storage cap.
MAX_DOCUMENT = 128 * 1024 * 1024
MAX_PDF = 64 * 1024 * 1024
ENV_KEYS = ("CFM_GPU_RESIDENT_SOCKET", "CFM_GPU_RESIDENT_TOKEN",
            "CFM_GPU_RESIDENT_RUNTIME", "CFM_GPU_RESIDENT_DEADLINE")
HEX = re.compile(r"[a-f0-9]{64}\Z")


def wait(sock, *, write, deadline, cancelled):
    while True:
        if cancelled() or deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("GPU IPC deadline or cancellation")
        timeout = min(.1, max(0, deadline - time.monotonic())) if deadline is not None else .1
        readable, writable, _ = select.select([] if write else [sock], [sock] if write else [], [], timeout)
        if readable or writable:
            return


def transfer(sock, data=None, *, length=None, deadline=None, cancelled=lambda: False):
    remaining = memoryview(data) if data is not None else length
    chunks = []
    while remaining:
        wait(sock, write=data is not None, deadline=deadline, cancelled=cancelled)
        if data is not None:
            count = sock.send(remaining[:65536])
            if not count:
                raise EOFError("GPU IPC closed")
            remaining = remaining[count:]
        else:
            chunk = sock.recv(min(remaining, 65536))
            if not chunk:
                raise EOFError("GPU IPC closed")
            chunks.append(chunk)
            remaining -= len(chunk)
    return b"".join(chunks) if data is None else None


def send(sock, value, *, limit=MAX_META, binary=False, **options):
    data = value if binary else json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if not 0 < len(data) <= limit:
        raise ValueError("GPU IPC payload exceeds bounds")
    transfer(sock, struct.pack(">Q", len(data)) + data, **options)


def receive(sock, *, limit=MAX_META, binary=False, **options):
    size = struct.unpack(">Q", transfer(sock, length=8, **options))[0]
    if not 0 < size <= limit:
        raise ValueError("GPU IPC payload exceeds bounds")
    data = transfer(sock, length=size, **options)
    return data if binary else json.loads(data)


class ResidentClient:
    def __init__(self, path, token, fingerprint, deadline):
        if not isinstance(token, str) or not HEX.fullmatch(token) or not HEX.fullmatch(fingerprint):
            raise ValueError("Invalid GPU job identity")
        self.path, self.token, self.fingerprint = str(path), token, fingerprint
        self.deadline = float(deadline)
        if not math.isfinite(self.deadline) or self.deadline <= time.monotonic():
            raise ValueError("Expired GPU job")
        parent = Path(path).parent
        if (not Path(path).is_absolute() or parent.is_symlink() or Path(path).is_symlink()
                or parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077
                or not stat.S_ISSOCK(Path(path).stat().st_mode)):
            raise ValueError("Invalid private GPU socket")
        self.call("probe")

    @classmethod
    def from_environment(cls):
        values = [os.environ.get(key) for key in ENV_KEYS]
        if not any(values):
            return None
        if not all(values) or os.environ.get("CFM_OCR_BACKEND") != "cuda" or not os.environ.get("CFM_SLAVE_ROOT"):
            raise ValueError("Incomplete standalone GPU job configuration")
        return cls(*values)

    def call(self, operation, data=None):
        request = {"op": operation, "token": self.token, "runtime": self.fingerprint}
        if data is not None:
            request["sha256"] = hashlib.sha256(data).hexdigest()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(max(.001, self.deadline - time.monotonic()))
            connection.connect(self.path)
            connection.setblocking(False)
            send(connection, request, deadline=self.deadline)
            if data is not None:
                send(connection, data, binary=True, limit=MAX_PDF, deadline=self.deadline)
            response = receive(connection, limit=MAX_DOCUMENT, deadline=self.deadline)
        if not isinstance(response, dict) or response.get("runtime") != self.fingerprint or "error" in response:
            raise ValueError("GPU inference identity or execution failed")
        if data is not None and response.get("sha256") != request["sha256"]:
            raise ValueError("GPU document identity mismatch")
        return response

    def extract(self, data):
        from .cache import validate_document
        value = validate_document(self.call("extract", data)["document"])
        return value["pages"], value["native"]
