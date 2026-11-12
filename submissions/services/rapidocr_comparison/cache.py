"""Bounded, runtime-bound OCR job artifacts; never publication authority."""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import uuid

from . import runtime

FORMAT = 1
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024


def worker_identity():
    """A service-volume identity, never the ephemeral container hostname/MAC."""
    value = os.environ.get("CFM_SLAVE_ROOT")
    if not value:
        return None
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("CFM_SLAVE_ROOT must be an existing absolute worker-data directory.")
    path = root / "worker-identity"
    if not path.exists():
        temporary = root / (".worker-identity-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(uuid.uuid4().hex.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != 32:
        raise ValueError("Invalid persisted worker identity; preserve the worker volume.")
    value = path.read_text(encoding="ascii")
    if uuid.UUID(hex=value).hex != value:
        raise ValueError("Invalid persisted worker identity.")
    return value


def runtime_fingerprint():
    import cv2
    import onnxruntime
    code = sorted(Path(__file__).parent.glob("*.py"))
    code.append(Path(__file__).parents[1] / "pdf_comparison_worker.py")
    worker = worker_identity()
    node = None if worker else uuid.getnode()
    # UUID's fallback is random per process, not a stable hardware identifier.
    node = node if node is not None and not node & (1 << 40) else None
    cpu = Path("/proc/cpuinfo")
    features = []
    if cpu.is_file():
        features = sorted({line.strip() for line in cpu.read_text().splitlines() if line.startswith(
            ("model name", "flags", "Features", "CPU implementer", "CPU part")
        )})
    identity = {
        "format": FORMAT, "backend": runtime.backend_identity(), "models": runtime.selected_model_hashes(),
        "execution": runtime.execution_identity(),
        "threads": runtime.ocr_thread_count(), "python": sys.version,
        "platform": platform.platform(), "machine": platform.machine(),
        "host": None if worker else platform.node(), "node": node, "worker": worker, "cpu": features,
        "opencv_cpu": cv2.getCPUFeaturesLine(),
        "versions": {name: importlib.metadata.version(name) for name in (
            "rapidocr", "onnxruntime-gpu" if runtime.ocr_backend() == "cuda" else "onnxruntime",
            "PyMuPDF", "numpy", "Shapely", "Pillow",
        )},
        "opencv": cv2.getBuildInformation(), "onnx": onnxruntime.get_build_info(),
        "code": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in code},
        "environment": {k: v for k, v in os.environ.items() if k.startswith(
            ("ORT_", "OMP_", "OPENBLAS_", "MKL_", "VECLIB_", "CUDA_", "NVIDIA_")
        )},
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _box(value, width, height):
    return (
        isinstance(value, list) and len(value) == 4 and all(_number(v) for v in value)
        and 0 <= value[0] < value[2] <= width and 0 <= value[1] < value[3] <= height
    )


def validate_document(value):
    """Reject cache data outside the same per-document extraction work bounds."""
    if not isinstance(value, dict) or set(value) != {"pages", "native"}:
        raise ValueError("Invalid OCR document cache")
    pages, native = value["pages"], value["native"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= runtime.MAX_PAGES:
        raise ValueError("Invalid cached page count")
    if not isinstance(native, list) or len(native) != len(pages):
        raise ValueError("Incomplete cached native geometry")
    characters = native_count = 0
    for number, (page, geometry) in enumerate(zip(pages, native), 1):
        if not isinstance(page, dict) or not isinstance(geometry, dict):
            raise ValueError("Invalid cached page")
        width, height = page.get("width"), page.get("height")
        if (
            type(width) is not int or type(height) is not int
            or min(width, height) <= 0 or max(width, height) > runtime.MAX_SIDE
            or width * height > runtime.MAX_PIXELS or page.get("page") != number
            or geometry.get("page") != number
            or any(not _number(page.get(k)) or page[k] <= 0 for k in ("pdf_width", "pdf_height"))
        ):
            raise ValueError("Invalid cached page dimensions")
        ocr = page.get("ocr")
        if not isinstance(ocr, dict) or ocr.get("status") != "completed" or not isinstance(ocr.get("lines"), list):
            raise ValueError("Incomplete cached OCR")
        if len(ocr["lines"]) > runtime.MAX_LINES:
            raise ValueError("Cached OCR line limit exceeded")
        for line in ocr["lines"]:
            if not isinstance(line, dict):
                raise ValueError("Invalid cached OCR line")
            text, score = line.get("text"), line.get("score")
            if (
                not isinstance(text, str) or not text.strip() or len(text) > 10000
                or not _number(score) or not 0 <= score <= 1
                or not _box(line.get("box"), width, height) or not isinstance(line.get("words"), list)
            ):
                raise ValueError("Invalid cached OCR line evidence")
            characters += len(text)
            for word in line["words"]:
                if (
                    not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"]
                    or not _number(word.get("score")) or not 0 <= word["score"] <= 1
                    or not _box(word.get("box"), width, height)
                ):
                    raise ValueError("Invalid cached word evidence")
        if not isinstance(geometry.get("words"), list):
            raise ValueError("Invalid cached native geometry")
        for word in geometry["words"]:
            if (
                not isinstance(word, dict) or set(word) != {"box", "block", "line", "word"}
                or not _box(word["box"], width, height)
                or any(type(word[k]) is not int or word[k] < 0 for k in ("block", "line", "word"))
            ):
                raise ValueError("Invalid cached native word")
        native_count += len(geometry["words"])
    if characters > runtime.MAX_CHARACTERS or native_count > runtime.MAX_NATIVE_WORDS:
        raise ValueError("Cached document work limit exceeded")
    return value


def read_artifact(directory, name, limit):
    if not directory or not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        return None
    path = directory / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            return None
        return json.loads(data)
    except (OSError, ValueError, RecursionError):
        return None


def write_artifact(directory, name, value, limit):
    """Only fixed-name artifacts in the private parent-created job directory."""
    if not directory or not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        return
    temporary = directory / (uuid.uuid4().hex + ".tmp")
    try:
        data = json.dumps(value, allow_nan=False).encode()
        if len(data) > limit:
            return
        with temporary.open("xb") as stream:
            stream.write(data)
        if (directory / name).is_symlink():
            return
        temporary.replace(directory / name)
    except (OSError, ValueError, RecursionError):
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
