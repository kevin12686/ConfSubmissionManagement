"""Isolated CUDA model-lifetime acceptance. Never uses conference storage.

Run in a finite-memory GPU worker container with read-only PDF/model mounts.
The output must be a new file. Tests use new temporary worker roots and clients.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comparison_slave.runner import Service, validate_output
from comparison_slave.storage import namespace_path


@contextmanager
def environment(**values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_service(root, idle):
    root.mkdir(mode=0o700)
    clients = root / "clients.json"
    clients.write_text(json.dumps({name: hashlib.sha256((root.name + name).encode()).hexdigest() for name in ("a", "b", "c")}))
    clients.chmod(0o600)
    with environment(CFM_GPU_MODEL_IDLE_SECONDS=idle):
        return Service(root, clients, start=False)


def vram():
    data = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True, timeout=5)
    return [int(line.strip()) for line in data.splitlines()]


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def raw_documents(service, namespace):
    folder = namespace_path(service.root, namespace) / "data/pdf_comparison_cache/entries"
    values = {}
    for path in sorted(folder.glob("d-*.json")):
        # These files were signed/validated by the real isolated job. Compare
        # their raw payloads, not signatures (keys differ between namespaces).
        encoded = path.read_text().split(":", 1)[0]
        value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        values[value["hashes"][0]] = value["value"]
    return values


def run_pair(service, namespace, body):
    started = time.monotonic()
    payload = service.run_process("job", namespace, body)
    if "error" in payload:
        raise ValueError("Actual CUDA comparison failed: " + payload["error"])
    validate_output(payload)
    return payload, round(time.monotonic() - started, 3)


def wait_cold(manager, seconds=18):
    deadline = time.monotonic() + seconds
    while manager.actor is not None and time.monotonic() < deadline:
        time.sleep(.05)
    if manager.actor is not None:
        raise ValueError("GPU model process did not expire")


def orphan_parent(root):
    service = make_service(root, 1800)
    service.run_process("preflight", "a")
    print(service.resident.actor.child.pid, flush=True)
    while True:
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--orphan-parent", type=Path)
    args = parser.parse_args()
    if os.environ.get("CFM_OCR_BACKEND") != "cuda":
        parser.error("Actual CUDA backend required; no CPU fallback.")
    if args.orphan_parent:
        orphan_parent(args.orphan_parent)
    if not args.before or not args.after or args.output.exists():
        parser.error("Read-only inputs and a new output file are required.")
    before, after = args.before.read_bytes(), args.after.read_bytes()
    hashes = [hashlib.sha256(data).hexdigest() for data in (before, after)]
    body = struct.pack(">QQ", len(before), len(after)) + before + after
    report = {"hashes": hashes, "backend": "cuda", "stages": {}}
    with tempfile.TemporaryDirectory(prefix="cfm-residency-acceptance-") as temporary:
        root = Path(temporary).resolve()
        reference = make_service(root / "reference", 0)
        try:
            reference.run_process("preflight", "a")
            expected, elapsed = run_pair(reference, "a", body)
            reference_docs = raw_documents(reference, "a")
            if set(reference_docs) != set(hashes):
                raise ValueError("Reference raw OCR cache was not retained")
            report["stages"]["per_job_reference_seconds"] = elapsed
        finally:
            reference.close()
        service = make_service(root / "resident", 10)
        try:
            base_vram = vram()
            started = time.monotonic()
            preflight = service.run_process("preflight", "a")
            report["stages"]["cold_model_load_seconds"] = round(time.monotonic() - started, 3)
            pid = service.resident.actor.child.pid
            for namespace in ("a", "b"):
                payload, elapsed = run_pair(service, namespace, body)
                assert payload["result"] == expected["result"]
                assert payload["render_hashes"] == expected["render_hashes"]
                assert canonical(raw_documents(service, namespace)) == canonical(reference_docs)
                assert service.resident.actor.child.pid == pid
                report["stages"]["fresh_ocr_namespace_" + namespace + "_seconds"] = elapsed
            last_used = service.resident.last_used
            payload, elapsed = run_pair(service, "a", body)
            assert payload["diagnostics"]["pair_hit"] is True
            assert service.resident.last_used == last_used
            assert payload["result"] == expected["result"]
            report["stages"]["warm_pair_cache_seconds"] = elapsed
            loaded_vram = vram()
            wait_cold(service.resident)
            unloaded_vram = vram()
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError("Expired GPU process still exists")
            assert max(loaded_vram) - max(unloaded_vram) > 100
            report["vram_mib"] = {"baseline": base_vram, "loaded": loaded_vram, "unloaded": unloaded_vram}
            service.run_process("preflight", "a")
            assert service.resident.actor is None
            payload, elapsed = run_pair(service, "a", body)
            assert service.resident.actor is None and payload["diagnostics"]["pair_hit"] is True
            assert payload["result"] == expected["result"]
            report["stages"]["cold_pair_cache_seconds"] = elapsed
            payload, elapsed = run_pair(service, "c", body)
            assert payload["result"] == expected["result"] and canonical(raw_documents(service, "c")) == canonical(reference_docs)
            assert service.resident.actor.child.pid != pid
            report["stages"]["reload_fresh_ocr_seconds"] = elapsed
            crashed_pid = service.resident.actor.child.pid
            os.kill(crashed_pid, signal.SIGKILL)
            try:
                recovered = service.run_process("preflight", "a")
            except (OSError, EOFError, ValueError):
                assert service.resident.actor is None
                recovered = service.run_process("preflight", "a")
            assert recovered["runtime"] == preflight["runtime"]
            assert service.resident.actor.child.pid != crashed_pid
            report["actor_crash_recovered"] = True
            report["raw_ocr_sha256"] = hashlib.sha256(canonical(reference_docs)).hexdigest()
        finally:
            service.close()
        with environment(CFM_RAPIDOCR_TIMEOUT=30):
            service = make_service(root / "timeout", 1800)
            try:
                service.run_process("preflight", "a")
                pid = service.resident.actor.child.pid
                os.kill(pid, signal.SIGSTOP)
                started = time.monotonic()
                payload = service.run_process("job", "a", body)
                elapsed = time.monotonic() - started
                assert payload == {"error": "comparison_failed"}
                assert 28 <= elapsed <= 40 and service.resident.actor is None
                report["stages"]["stalled_actor_failure_seconds"] = round(elapsed, 3)
            finally:
                service.close()
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--output", str(root / "unused.json"),
            "--orphan-parent", str(root / "orphan")], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, text=True)
        try:
            from select import select
            if not select([child.stdout], [], [], 65)[0]:
                raise ValueError("Orphan-guard fixture did not start")
            pid = int(child.stdout.readline())
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert not Path(f"/proc/{pid}").exists()
            report["parent_death_reaps_gpu"] = True
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
            child.stdout.close()
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.before, args.after)] == hashes
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
