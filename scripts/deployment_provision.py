"""Explicit deployment-time model provisioning; standard library only."""

import ast
from contextlib import contextmanager
import hashlib
import http.client
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.parse
import urllib.request


RUNTIME_SOURCE = Path(__file__).resolve().parents[1] / "submissions/services/rapidocr_comparison/runtime.py"
MODEL_URLS = {
    f"PP-OCRv6_{kind}_{size}.onnx":
    f"https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6/{kind}/PP-OCRv6_{kind}_{size}.onnx"
    for kind in ("det", "rec") for size in ("small", "medium")
}
MAX_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
SOCKET_TIMEOUT = 15
DOWNLOAD_SECONDS = 120
LOCK_NAME = ".cfm-model-provision.lock"


class ProvisionError(RuntimeError):
    """Provisioning failed without authorizing application startup."""


def _model_hashes(det_model="small", rec_model="small"):
    # Read policy without importing runtime's native dependencies (including fitz).
    try:
        tree = ast.parse(RUNTIME_SOURCE.read_text(encoding="utf-8"))
        hashes = {}
        for name in ("MODEL_HASHES", "MEDIUM_MODEL_HASHES"):
            assignments = [node.value for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)]
            if len(assignments) != 1:
                raise ValueError("expected one MODEL_HASHES literal per model size")
            hashes.update(ast.literal_eval(assignments[0]))
        if (not isinstance(hashes, dict) or hashes.keys() != MODEL_URLS.keys()
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in hashes.values())):
            raise ValueError("unexpected model policy")
        if any(size not in {"small", "medium"} for size in (det_model, rec_model)):
            raise ValueError("model sizes must be small or medium")
        return {name: hashes[name] for name in (f"PP-OCRv6_det_{det_model}.onnx", f"PP-OCRv6_rec_{rec_model}.onnx")}
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        raise ProvisionError("Cannot read pinned MODEL_HASHES. Restore the complete matching checkout; do not substitute models.") from exc


def _plain_stat(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ProvisionError(f"Refusing symlink or reparse path: {path}. Use a real directory and regular model files.")
    return info


def _directory(path, *, create=False):
    for component in (*reversed(path.parents), path):
        info = _plain_stat(component)
        if info is None and create:
            try:
                component.mkdir(mode=0o755)
            except FileExistsError:
                pass
            info = _plain_stat(component)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise ProvisionError(f"Model directory component is not a directory: {component}. Choose a separate model directory.")


def _valid_existing(path, expected):
    info = _plain_stat(path)
    if info is None:
        return False
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
        raise ProvisionError(f"Invalid existing model: {path}. Move it aside explicitly and retry; it will not be overwritten.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ProvisionError(f"Model changed while opening: {path}. Stop concurrent changes and retry.")
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ProvisionError(f"Model exceeds 100 MiB: {path}. Existing file was preserved.")
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ProvisionError(f"Checksum mismatch for existing model: {path}. Move it aside explicitly and retry; it will not be overwritten.")
    return True


@contextmanager
def _lock(directory):
    path = directory / LOCK_NAME
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ProvisionError(f"Model provisioning lock exists: {path}. Wait for the other provisioner; after an interrupted run, remove only this lock once no provisioner is running.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"{os.getpid()}\n")
        yield
    finally:
        path.unlink()


class _HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ProvisionError("Model download refused a non-HTTPS redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(name, expected, destination):
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    opener = urllib.request.build_opener(_HTTPSRedirects())
    try:
        with opener.open(MODEL_URLS[name], timeout=SOCKET_TIMEOUT) as response:
            if response.status != 200 or urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ProvisionError("Expected a successful HTTPS model response.")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not length.isdecimal() or not 0 < int(length) <= MAX_BYTES:
                    raise ProvisionError("Invalid or oversized model Content-Length.")
                length = int(length)
            digest, size = hashlib.sha256(), 0
            with destination.open("xb") as stream:
                while True:
                    if time.monotonic() >= deadline:
                        raise ProvisionError("Model download exceeded its 120-second deadline.")
                    chunk = response.read1(min(CHUNK_BYTES, MAX_BYTES - size + 1))
                    if time.monotonic() >= deadline:
                        raise ProvisionError("Model download exceeded its 120-second deadline.")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ProvisionError("Model download exceeds 100 MiB.")
                    digest.update(chunk)
                    stream.write(chunk)
                if length is not None and size != length:
                    raise ProvisionError("Truncated model download (Content-Length mismatch).")
                if digest.hexdigest() != expected:
                    raise ProvisionError("Downloaded model checksum does not match the pinned SHA256.")
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, ValueError, http.client.HTTPException, ProvisionError) as exc:
        raise ProvisionError(f"Cannot provision {name}: {exc}. Retry online or transfer the exact checksum-verified file from {MODEL_URLS[name]} into the selected directory, then retry offline.") from exc


def provision_models(directory, *, offline=False, dry_run=False, det_model="small", rec_model="small"):
    """Return filename -> absolute Path; dry-run paths may not exist yet.

    Existing files are always validated and never replaced. Offline requires all
    models; dry-run only reports missing downloads, without network or writes.
    Concurrent provisioners fail immediately rather than wait or steal a lock.
    """
    hashes = _model_hashes(det_model, rec_model)
    path = Path(directory).expanduser()
    if ".." in path.parts:
        raise ProvisionError("Model directory must not contain '..'; supply a direct absolute directory path.")
    path = Path(os.path.abspath(path))
    paths = {name: path / name for name in hashes}
    try:
        _directory(path)
        missing = [name for name in hashes if not _valid_existing(paths[name], hashes[name])]
        if offline and missing:
            instructions = "; ".join(f"{name} (SHA256 {hashes[name]}) from {MODEL_URLS[name]}" for name in missing)
            raise ProvisionError(f"Offline models missing in {path}: {instructions}. Transfer these verified files here, or rerun without offline mode.")
        if dry_run:
            for name in missing:
                print(f"Would download and verify {name} from {MODEL_URLS[name]} into {path}")
            return paths
        if not missing:
            return paths
        _directory(path, create=True)
        with _lock(path):
            missing = [name for name in hashes if not _valid_existing(paths[name], hashes[name])]
            with tempfile.TemporaryDirectory(prefix=".cfm-model-stage-", dir=path) as temporary:
                stage = Path(temporary)
                for name in missing:
                    _download(name, hashes[name], stage / name)
                _directory(path)
                for name in missing:
                    # Hard-link publication is atomic AND refuses an existing target
                    # on POSIX/Windows; os.rename would overwrite on POSIX.
                    os.link(stage / name, paths[name])
        return paths
    except OSError as exc:
        raise ProvisionError(f"Cannot safely provision models in {path}: {exc}. Existing models are not replaced; check directory permissions and filesystem hard-link support, then retry.") from exc
