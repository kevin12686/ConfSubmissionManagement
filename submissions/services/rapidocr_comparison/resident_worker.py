"""GPU-only model process. Never imports conference settings, models or caches."""
from contextlib import redirect_stdout
import ctypes
import hashlib
import json
import math
import os
import resource
import signal
import socket
import sys
import time

from .config import parallel_pages, timeout_seconds
from .resident_protocol import MAX_DOCUMENT, MAX_META, MAX_PDF, receive, send


def parent_guard(expected):
    if sys.platform.startswith("linux"):
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0):
            raise OSError("Cannot guard GPU parent lifetime")
    if os.getppid() != expected:
        raise RuntimeError("GPU parent exited")


def watchdog(deadline):
    if deadline is not None and (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise ValueError("Invalid GPU command deadline")
    # Kernel-default termination also interrupts a stuck native call; a Python
    # alarm handler could be delayed until that call releases the GIL.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, max(.001, deadline - time.monotonic()) if deadline is not None else 0)


def metadata():
    from . import cache, runtime
    runtime.validate_models()
    backend = runtime.backend_identity()
    if (runtime.cuda_device_identity() != backend["device"]
            or runtime.cuda_library_versions() != backend["libraries"]):
        raise ValueError("Loaded GPU identity or libraries changed")
    return {"runtime": cache.runtime_fingerprint(), "backend": backend}


def cold_probe(previous):
    """Revalidate a previously loaded engine without allocating model sessions."""
    from . import cache, runtime
    runtime.validate_models()
    runtime.validate_dependencies()
    backend = previous["backend"]
    if (runtime.ocr_backend() != "cuda" or backend.get("backend") != "cuda"
            or runtime.cuda_device_identity() != backend["device"]
            or runtime.cuda_library_versions() != backend["libraries"]
            or set(backend["sessions"]) != {"det", "rec"}
            or any(session["providers"][0] != "CUDAExecutionProvider" for session in backend["sessions"].values())):
        raise ValueError("GPU runtime changed since validation")
    return {"runtime": cache.runtime_fingerprint(backend_identity=backend), "backend": backend}


def serve(connection):
    from . import cache, runtime
    from submissions.services.pdf_comparison_worker import resource_limits
    resource_limits(persistent=True)
    recognizer = runtime.build_recognizer()
    initial = metadata()
    send(connection, initial)
    watchdog(None)
    last_job = None
    while True:
        request = receive(connection)
        if not isinstance(request, dict) or request.get("deadline") is None:
            raise ValueError("Missing GPU command deadline")
        watchdog(request["deadline"])
        if request.get("op") == "probe":
            current = metadata()
            if current != initial:
                raise ValueError("Loaded GPU runtime changed")
            send(connection, current)
            watchdog(None)
            continue
        if request.get("op") != "extract" or request.get("runtime") != initial["runtime"]:
            raise ValueError("Invalid GPU operation or runtime")
        data = receive(connection, binary=True, limit=MAX_PDF)
        digest = hashlib.sha256(data).hexdigest()
        if request.get("sha256") != digest or metadata() != initial:
            raise ValueError("GPU PDF or runtime changed")
        if last_job != request["job"]:
            used = resource.getrusage(resource.RUSAGE_SELF)
            budget = math.ceil(used.ru_utime + used.ru_stime) + runtime.MAX_THREADS * parallel_pages() * timeout_seconds()
            resource.setrlimit(resource.RLIMIT_CPU, (budget, resource.RLIM_INFINITY))
            last_job = request["job"]
        pages, native = runtime.extract_document(data, recognizer)
        document = cache.validate_document({"pages": pages, "native": native})
        if metadata() != initial:
            raise ValueError("GPU runtime changed during extraction")
        send(connection, {"runtime": initial["runtime"], "sha256": digest, "document": document}, limit=MAX_DOCUMENT)
        watchdog(None)
        del data, pages, native, document


def main():
    parent_guard(int(sys.argv[-1]))
    watchdog(float(sys.argv[-2]))
    from . import runtime
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 ** 2, 16 * 1024 ** 2))
    if runtime.ocr_backend() != "cuda":
        raise ValueError("Resident inference requires CUDA")
    if sys.argv[1] == "probe":
        previous = json.loads(sys.stdin.buffer.read(MAX_META + 1))
        with redirect_stdout(sys.stderr):
            result = cold_probe(previous)
        encoded = json.dumps(result, allow_nan=False).encode()
        if len(encoded) > MAX_META:
            raise ValueError("GPU metadata exceeds bounds")
        sys.stdout.buffer.write(encoded)
    elif sys.argv[1] == "serve":
        with socket.socket(fileno=int(sys.argv[2])) as connection, redirect_stdout(sys.stderr):
            connection.setblocking(False)
            serve(connection)
    else:
        raise ValueError("Invalid GPU process mode")


if __name__ == "__main__":
    main()
