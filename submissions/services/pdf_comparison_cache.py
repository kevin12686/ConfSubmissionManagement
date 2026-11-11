"""Shared best-effort, signed CPU evidence cache; all live binding stays upstream."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from django.conf import settings
from django.core import signing
from django.core.files import locks

from .rapidocr_comparison.cache import (
    FORMAT, MAX_DOCUMENT_BYTES, MAX_RESULT_BYTES, read_artifact, validate_document,
)
from .rapidocr_comparison.result import validate_result

logger = logging.getLogger(__name__)
TTL = timedelta(days=7)
MAX_DISK_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 512
MAX_SIGNED_BYTES = 12 * 1024 * 1024
SALT = "submissions.cpu-comparison-cache.v1"
ENTRY = re.compile(r"[dp]-[a-f0-9]{64}\.json\Z")


class ComparisonCacheBusy(ValueError):
    pass


def _root():
    root = Path(settings.BASE_DIR) / "data" / "pdf_comparison_cache"
    if root.parent.is_symlink() or root.is_symlink():
        raise OSError("Comparison cache cannot be a symbolic link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("entries", "locks"):
        path = root / name
        if path.is_symlink():
            raise OSError("Comparison cache cannot contain symbolic directories")
        path.mkdir(exist_ok=True, mode=0o700)
    return root


def _load(path, kind, hashes):
    try:
        if path.is_symlink() or path.stat().st_size > MAX_SIGNED_BYTES:
            return None
        with path.open("r", encoding="utf-8") as stream:
            signed = stream.read(MAX_SIGNED_BYTES + 1)
        if len(signed) > MAX_SIGNED_BYTES or signed.startswith("."):
            return None
        payload = signing.loads(signed, salt=SALT, max_age=int(TTL.total_seconds()))
        if (
            payload.get("format") != FORMAT or payload.get("kind") != kind
            or payload.get("hashes") != hashes or not re.fullmatch(r"[a-f0-9]{64}", payload.get("runtime", ""))
        ):
            return None
        if kind == "document":
            validate_document(payload["value"])
        else:
            validate_result(payload["value"])
        return payload
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, signing.BadSignature):
        return None


def _store(path, payload):
    temporary = path.parent / (uuid.uuid4().hex + ".tmp")
    try:
        signed = signing.dumps(payload, salt=SALT, compress=False)
        if len(signed.encode()) > MAX_SIGNED_BYTES or path.is_symlink() or path.parent.is_symlink():
            return
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(signed)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _prune(root):
    if root.is_symlink() or (root / "entries").is_symlink():
        return
    files = []
    for path in (root / "entries").iterdir():
        if path.is_symlink() or not ENTRY.fullmatch(path.name) or not path.is_file():
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime, stat.st_size, path))
    size = sum(row[1] for row in files)
    count = len(files)
    for modified, length, path in sorted(files):
        if time.time() - modified > TTL.total_seconds() or size > MAX_DISK_BYTES or count > MAX_ENTRIES:
            path.unlink(missing_ok=True)
            size -= length
            count -= 1


def _open_lock(path):
    if path.is_symlink():
        raise OSError("Invalid comparison cache lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    return os.fdopen(fd, "a+b")


@contextmanager
def _index_lock(root):
    with _open_lock(root / "index.lock") as guard:
        if not locks.lock(guard, locks.LOCK_EX):
            raise OSError("Comparison cache index is busy")
        try:
            yield
        finally:
            locks.unlock(guard)


def _prune_locks(root):
    """Call under index.lock: no builder can open an inode being retired."""
    files = []
    for path in (root / "locks").iterdir():
        if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}\.lock", path.name):
            continue
        try:
            files.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    remaining = len(files)
    for modified, path in sorted(files):
        if time.time() - modified <= TTL.total_seconds() and remaining <= MAX_ENTRIES:
            continue
        idle = False
        with _open_lock(path) as handle:
            if locks.lock(handle, locks.LOCK_EX | locks.LOCK_NB):
                idle = True
                locks.unlock(handle)
        # index.lock excludes new openers; Windows requires closing before unlink.
        if idle:
            path.unlink(missing_ok=True)
            remaining -= 1


class CacheJob:
    def __init__(self, root, directory, hashes):
        self.root, self.directory, self.hashes = root, directory, hashes
        self.pair = hashlib.sha256(json.dumps(hashes).encode()).hexdigest()
        self.stats = {}

    def prepare(self):
        for side, digest in zip(("before", "after"), self.hashes):
            value = _load(self.root / "entries" / f"d-{digest}.json", "document", [digest])
            if value:
                (self.directory / f"{side}.input.json").write_text(json.dumps(value), encoding="utf-8")
        value = _load(self.root / "entries" / f"p-{self.pair}.json", "pair", self.hashes)
        if value:
            (self.directory / "pair.input.json").write_text(json.dumps(value), encoding="utf-8")

    def collect(self, result=None):
        if (
            not self.root.exists() or self.root.is_symlink()
            or any((self.root / name).is_symlink() for name in ("entries", "locks"))
        ):
            return
        meta = read_artifact(self.directory, "runtime.json", 16384)
        if not isinstance(meta, dict) or not re.fullmatch(r"[a-f0-9]{64}", meta.get("runtime", "")):
            return
        self.stats = {key: meta[key] for key in ("runtime", "pair_hit", "document_hits") if key in meta}
        for side, digest in zip(("before", "after"), self.hashes):
            payload = read_artifact(self.directory, f"{side}.completed.json", MAX_DOCUMENT_BYTES)
            if (
                not isinstance(payload, dict) or payload.get("format") != FORMAT
                or payload.get("kind") != "document"
                or payload.get("runtime") != meta["runtime"] or payload.get("hashes") != [digest]
            ):
                continue
            try:
                validate_document(payload["value"])
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            _store(self.root / "entries" / f"d-{digest}.json", payload)
        if result is not None:
            validate_result(result)
            _store(self.root / "entries" / f"p-{self.pair}.json", {
                "format": FORMAT, "kind": "pair", "runtime": meta["runtime"],
                "hashes": self.hashes, "value": result,
            })
        with _index_lock(self.root):
            _prune(self.root)
            _prune_locks(self.root)


@contextmanager
def comparison_cache_job(before, after):
    """A failed/unwritable cache never prevents fresh comparison; busy work does."""
    handle = None
    try:
        root = _root()
        hashes = [hashlib.sha256(data).hexdigest() for data in (before, after)]
        pair = hashlib.sha256(json.dumps(hashes).encode()).hexdigest()
        path = root / "locks" / (pair + ".lock")
        with _index_lock(root):
            handle = _open_lock(path)
            if not locks.lock(handle, locks.LOCK_EX | locks.LOCK_NB):
                raise ComparisonCacheBusy("This PDF pair is already being compared. Retry after the running comparison finishes.")
            os.utime(path, None)
            _prune(root)
            _prune_locks(root)
        temporary = tempfile.TemporaryDirectory(prefix="cfm-ocr-job-", ignore_cleanup_errors=True)
    except ComparisonCacheBusy:
        if handle:
            handle.close()
        raise
    except OSError:
        if handle:
            handle.close()
        handle = None
        logger.warning("Comparison cache unavailable; calculating fresh evidence.", exc_info=True)
    if handle is None:
        yield None
        return
    try:
        job = CacheJob(root, Path(temporary.name), hashes)
        try:
            job.prepare()
        except (OSError, ValueError, TypeError):
            logger.warning("Could not prepare OCR cache inputs; calculating fresh evidence.", exc_info=True)
        yield job
    finally:
        try:
            temporary.cleanup()
        except OSError:
            logger.warning("Could not remove private OCR job directory.", exc_info=True)
        try:
            try:
                locks.unlock(handle)
            finally:
                handle.close()
        except OSError:
            logger.warning("Could not close OCR cache lock.", exc_info=True)
