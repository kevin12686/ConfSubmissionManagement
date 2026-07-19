import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class FileStatus:
    path: Path
    exists: bool
    signature: tuple[int, int, int, int, int] | None


class FileChangedDuringInspection(RuntimeError):
    pass


class FileInspectionContext:
    """Request-scoped filesystem observations with content-hash reuse."""

    def __init__(self):
        self._statuses = {}
        self._hashes = {}

    def status(self, path):
        canonical = _canonical_path(path)
        if canonical not in self._statuses:
            self._statuses[canonical] = _read_status(canonical)
        return self._statuses[canonical]

    def exists(self, path):
        return self.status(path).exists

    def sha256(self, path, *, fresh=False):
        canonical = _canonical_path(path)
        for _attempt in range(2):
            status = self.status(canonical)
            if fresh:
                current_status = _read_status(canonical)
                if current_status.signature != status.signature:
                    self._statuses[canonical] = current_status
                    status = current_status
            if not status.exists:
                raise FileNotFoundError(canonical)
            cache_key = (canonical, status.signature)
            if cache_key in self._hashes:
                return self._hashes[cache_key]
            try:
                if fresh:
                    digest = _read_sha256(canonical)
                    _assert_signature(canonical, status.signature)
                else:
                    digest = _sha256_for_signature(
                        str(canonical),
                        status.signature,
                    )
                self._hashes[cache_key] = digest
                return digest
            except FileChangedDuringInspection:
                self._statuses.pop(canonical, None)
        raise FileChangedDuringInspection(
            f"File changed repeatedly while being inspected: {canonical}"
        )

    def read_snapshot_bytes(self, path):
        """Read bytes only when the path still matches this context's snapshot."""
        canonical = _canonical_path(path)
        status = self.status(canonical)
        if not status.exists:
            raise FileNotFoundError(canonical)
        try:
            contents = canonical.read_bytes()
        except OSError as exc:
            raise FileChangedDuringInspection(str(canonical)) from exc
        _assert_signature(canonical, status.signature)
        return contents


def clear_file_hash_cache():
    _sha256_for_signature.cache_clear()


def _canonical_path(path):
    return Path(os.path.abspath(os.fspath(path)))


def _read_status(path):
    try:
        stat = path.stat()
    except OSError:
        return FileStatus(path=path, exists=False, signature=None)
    return FileStatus(path=path, exists=True, signature=_stat_signature(stat))


def _stat_signature(stat):
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _assert_signature(path, expected):
    try:
        current = _stat_signature(path.stat())
    except OSError as exc:
        raise FileChangedDuringInspection(str(path)) from exc
    if current != expected:
        raise FileChangedDuringInspection(str(path))


@lru_cache(maxsize=32768)
def _sha256_for_signature(path_value, signature):
    path = Path(path_value)
    digest = _read_sha256(path)
    _assert_signature(path, signature)
    return digest


def _read_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
