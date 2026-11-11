"""Isolated full-page RapidOCR comparison.

No Django or publication metadata access. Optional bounded job artifacts are
restricted to a private directory created by the parent service.
"""

import json
import copy
import hashlib
import sys
import struct
from contextlib import redirect_stdout
from pathlib import Path

import fitz
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def read_pair(stream):
    header = stream.read(16)
    if len(header) != 16:
        raise ValueError("Incomplete comparison request")
    lengths = struct.unpack(">QQ", header)
    if any(not 0 < size <= MAX_INPUT_BYTES for size in lengths):
        raise ValueError("Comparison input exceeds safe bounds")
    documents = [stream.read(size) for size in lengths]
    if any(len(data) != size for data, size in zip(documents, lengths)) or stream.read(1):
        raise ValueError("Invalid comparison request length")
    return documents


def resource_limits():
    # Windows relies on the parent wall-clock timeout and bounded inputs.
    # Linux also bounds native allocations; macOS reserves large virtual ranges
    # for its runtime, so an address-space limit there would reject valid files.
    from submissions.services.rapidocr_comparison.runtime import MAX_THREADS, WALL_SECONDS, ocr_backend
    backend = ocr_backend()
    if backend == "cuda" and not sys.platform.startswith("linux"):
        raise ValueError("CUDA comparison requires Linux with a finite cgroup container memory limit.")
    try:
        import resource
    except ImportError:
        return
    # CPU time accumulates across threads; it must not cut short the wall budget
    # when a deployment explicitly enables more ONNX threads.
    cpu_budget = MAX_THREADS * WALL_SECONDS
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_budget, cpu_budget + 20))
    if sys.platform.startswith("linux"):
        if backend == "cuda":
            # CUDA reserves large virtual ranges. Require an actual finite
            # container memory controller instead of an address-space cap.
            limits = []
            for path in (Path("/sys/fs/cgroup/memory.max"),
                         Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
                try:
                    value = int(path.read_text().strip())
                except (OSError, ValueError):
                    continue
                # cgroup v1's unlimited sentinel is near LONG_MAX/PAGE_SIZE.
                if 0 < value < (1 << 60):
                    limits.append(value)
            if not limits:
                raise ValueError("CUDA comparison requires a finite cgroup container memory limit (Compose mem_limit).")
        else:
            resource.setrlimit(resource.RLIMIT_AS, (6 * 1024 ** 3, 6 * 1024 ** 3))


def inspect_pair(before, after, cache_directory=None):
    from submissions.services.rapidocr_comparison.result import VERSION, build_result, validate_result
    from submissions.services.rapidocr_comparison.runtime import build_recognizer, extract_document

    try:
        recognizer = build_recognizer()
    except Exception:
        return {"version": VERSION, "error": "configuration"}
    try:
        from submissions.services.rapidocr_comparison.cache import (
            FORMAT, MAX_DOCUMENT_BYTES, MAX_RESULT_BYTES, read_artifact,
            runtime_fingerprint, validate_document, write_artifact,
        )
        fingerprint = None
        if cache_directory:
            try:
                fingerprint = runtime_fingerprint()
            except Exception:
                cache_directory = None
        hashes = [hashlib.sha256(data).hexdigest() for data in (before, after)]
        meta = {"runtime": fingerprint, "pair_hit": False, "document_hits": []}
        if cache_directory:
            write_artifact(cache_directory, "runtime.json", meta, 16384)
            cached = read_artifact(cache_directory, "pair.input.json", MAX_RESULT_BYTES + 65536)
            try:
                if (
                    isinstance(cached, dict) and cached.get("format") == FORMAT
                    and cached.get("kind") == "pair" and cached.get("runtime") == fingerprint
                    and cached.get("hashes") == hashes
                    and cached["value"]["identical_bytes"] == (before == after)
                ):
                    result = validate_result(cached["value"])
                    meta["pair_hit"] = True
                    write_artifact(cache_directory, "runtime.json", meta, 16384)
                    return {"version": VERSION, "result": result}
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        documents, memo = [], {}
        for side, data, digest in zip(("before", "after"), (before, after), hashes):
            document = None
            if cache_directory:
                cached = read_artifact(cache_directory, f"{side}.input.json", MAX_DOCUMENT_BYTES + 65536)
                try:
                    if (
                        isinstance(cached, dict) and cached.get("format") == FORMAT
                        and cached.get("kind") == "document" and cached.get("runtime") == fingerprint
                        and cached.get("hashes") == [digest]
                    ):
                        document = validate_document(cached["value"])
                        meta["document_hits"].append(side)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            if document is None:
                if digest in memo:
                    document = copy.deepcopy(memo[digest])
                else:
                    pages, native = extract_document(data, recognizer)
                    document = {"pages": pages, "native": native}
                memo[digest] = document
                if cache_directory:
                    write_artifact(cache_directory, f"{side}.completed.json", {
                        "format": FORMAT, "kind": "document", "runtime": fingerprint,
                        "hashes": [digest], "value": document,
                    }, MAX_DOCUMENT_BYTES)
            documents.append(document)
            if cache_directory:
                write_artifact(cache_directory, "runtime.json", meta, 16384)
        pa, na = documents[0]["pages"], documents[0]["native"]
        pb, nb = documents[1]["pages"], documents[1]["native"]
        result = build_result(pa, pb, na, nb, identical_bytes=before == after)
        validate_result(result)
        return {"version": VERSION, "result": result}
    except Exception:
        return {"version": VERSION, "error": "ocr_failed"}


if __name__ == "__main__":
    # Keep library diagnostics away from the structured response.
    fitz.set_messages(stream=sys.stderr)
    resource_limits()
    with redirect_stdout(sys.stderr):
        before, after = read_pair(sys.stdin.buffer)
        cache_directory = None
        if len(sys.argv) == 3 and sys.argv[1] == "--cache-inputs":
            cache_directory = Path(sys.argv[2])
        elif len(sys.argv) != 1:
            raise ValueError("Invalid worker arguments")
        evidence = inspect_pair(before, after, cache_directory=cache_directory)
        encoded = json.dumps(evidence, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("Comparison evidence exceeds safe output bounds")
    sys.stdout.buffer.write(encoded)
