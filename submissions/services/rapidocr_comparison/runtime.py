"""Local, pinned OCR models and bounded full-page recognition.

Native text strings never leave extraction: only geometry can guide OCR order.
"""
import copy
import ctypes
import hashlib
import importlib.metadata
import math
import os
from pathlib import Path

import fitz

from .config import DEFAULT_TIMEOUT, execution_identity, model_size, parallel_pages, timeout_seconds

MODEL_HASHES = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
}
MEDIUM_MODEL_HASHES = {
    "PP-OCRv6_det_medium.onnx": "92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2",
    "PP-OCRv6_rec_medium.onnx": "eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b",
}
MODEL_ENV = "CFM_RAPIDOCR_MODEL_DIR"
MAX_PAGES = 40
MAX_PIXELS = 16_000_000
MAX_SIDE = 4000
MAX_NATIVE_WORDS = 60_000
MAX_LINES = 2000
MAX_CHARACTERS = 300_000
WALL_SECONDS = DEFAULT_TIMEOUT
MAX_THREADS = 8
THREAD_ENV = "CFM_RAPIDOCR_THREADS"
BACKEND_ENV = "CFM_OCR_BACKEND"
_validated_cuda_identity = None


def ocr_backend():
    value = os.environ.get(BACKEND_ENV, "cpu")
    if value not in ("cpu", "cuda"):
        raise ValueError(f"{BACKEND_ENV} must be cpu or cuda.")
    return value


def cuda_device_identity():
    """Query the selected visible device through the real NVIDIA driver."""
    driver = ctypes.CDLL("libcuda.so.1")

    def call(name, *args):
        if getattr(driver, name)(*args) != 0:
            raise ValueError(f"Cannot identify CUDA device: {name} failed.")

    call("cuInit", 0)
    device, version, major, minor = (ctypes.c_int() for _ in range(4))
    call("cuDeviceGet", ctypes.byref(device), 0)
    call("cuDriverGetVersion", ctypes.byref(version))
    call("cuDeviceComputeCapability", ctypes.byref(major), ctypes.byref(minor), device)
    name = ctypes.create_string_buffer(256)
    identifier = ctypes.create_string_buffer(16)
    call("cuDeviceGetName", name, len(name), device)
    uuid_api = "cuDeviceGetUuid_v2" if hasattr(driver, "cuDeviceGetUuid_v2") else "cuDeviceGetUuid"
    call(uuid_api, ctypes.byref(identifier), device)
    return {"name": name.value.decode("utf-8"), "uuid": identifier.raw.hex(),
            "driver": version.value, "compute_capability": [major.value, minor.value]}


def backend_identity():
    backend = ocr_backend()
    if backend == "cpu":
        return {"backend": backend, "providers": ["CPUExecutionProvider"]}
    if _validated_cuda_identity is None:
        raise ValueError("CUDA runtime identity requires validated detection and recognition sessions.")
    return copy.deepcopy(_validated_cuda_identity)


def ocr_thread_count():
    available = os.cpu_count() or 1
    value = os.environ.get(THREAD_ENV, str(min(4, available)))
    try:
        count = int(value)
    except ValueError as exc:
        raise ValueError(f"{THREAD_ENV} must be an integer from 1 to {MAX_THREADS}.") from exc
    if not 1 <= count <= min(MAX_THREADS, available):
        raise ValueError(f"{THREAD_ENV} must be between 1 and {min(MAX_THREADS, available)} for this runtime.")
    return count


def validate_models(directory=None):
    value = directory if directory is not None else os.environ.get(MODEL_ENV)
    if not value or not Path(value).is_absolute():
        raise ValueError(f"Set {MODEL_ENV} to an absolute directory prepared with prepare_rapidocr_models.")
    root = Path(value)
    for name, expected in selected_model_hashes().items():
        path = root / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError(f"Required local OCR model is missing or invalid: {name}.")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"Local OCR model checksum mismatch: {name}.")
    return root


def selected_model_hashes():
    sizes = {kind: model_size(kind) for kind in ("det", "rec")}
    if all(size == "small" for size in sizes.values()):
        return dict(MODEL_HASHES)
    catalog = {**MODEL_HASHES, **MEDIUM_MODEL_HASHES}
    return {f"PP-OCRv6_{kind}_{size}.onnx": catalog[f"PP-OCRv6_{kind}_{size}.onnx"]
            for kind, size in sizes.items()}


def build_recognizer():
    # Keep the CPU single-page execution path unchanged; parallel execution is
    # bounded on standalone CPU, and CUDA owns four sessions by default.
    execution_identity()
    timeout_seconds()
    first = _build_recognizer()
    count = parallel_pages()
    if ocr_backend() == "cpu" and count == 1:
        return first
    from .scheduling import RecognizerPool
    engines = [first] + [_build_recognizer() for _ in range(count - 1)]
    if ocr_backend() == "cuda":
        from .gpu_reduction import ReductionPool, attach
        reduction = ReductionPool()
        for engine in engines:
            attach(engine, reduction)
    return RecognizerPool(engines)


def validate_dependencies():
    backend = ocr_backend()
    package = "onnxruntime-gpu" if backend == "cuda" else "onnxruntime"
    other = "onnxruntime" if backend == "cuda" else "onnxruntime-gpu"
    for name, version in (("rapidocr", "3.10.0"), (package, "1.31.1")):
        if importlib.metadata.version(name) != version:
            raise ValueError(f"OCR requires {name}=={version} in its backend-specific environment.")
    try:
        importlib.metadata.distribution(other)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise ValueError(f"OCR {backend} requires only {package}, not {other}; use separate environments.")
    import onnxruntime
    onnxruntime.disable_telemetry_events()
    if backend == "cuda":
        if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
            raise ValueError("CUDAExecutionProvider is unavailable; CPU fallback is not permitted.")
        # The GPU image installs ORT's CUDA/cuDNN extras, not a second ORT wheel.
        onnxruntime.preload_dlls(directory="")
    return onnxruntime


def cuda_library_versions():
    return {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name", "").lower().startswith("nvidia-")}


def _build_recognizer():
    global _validated_cuda_identity
    _validated_cuda_identity = None
    root = validate_models()
    backend = ocr_backend()
    onnxruntime = validate_dependencies()
    from rapidocr import RapidOCR

    # Explicit model paths avoid RapidOCR's model-directory download branch.
    params = {
        "Global.model_root_dir": str(root), "Global.text_score": 0.0,
        "Global.max_side_len": MAX_SIDE, "Global.use_cls": False,
        "Det.model_path": str(root / f"PP-OCRv6_det_{model_size('det')}.onnx"),
        "Rec.model_path": str(root / f"PP-OCRv6_rec_{model_size('rec')}.onnx"),
        "Det.limit_side_len": 2560, "Det.limit_type": "max",
        "EngineConfig.onnxruntime.intra_op_num_threads": ocr_thread_count(),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }
    if backend == "cuda":
        params.update({"EngineConfig.onnxruntime.use_cuda": True,
                       "EngineConfig.onnxruntime.cuda_ep_cfg.device_id": 0,
                       "EngineConfig.onnxruntime.cuda_ep_cfg.use_tf32": 0})
    recognizer = RapidOCR(params=params)
    if backend == "cuda":
        sessions = {}
        # RapidOCR 3.10.0 is lazy and its provider verifier only logs fallback.
        for name, load in (("det", recognizer._load_det_model), ("rec", recognizer._load_rec_model)):
            session = load().session.session
            providers = session.get_providers()
            if not providers or providers[0] != "CUDAExecutionProvider":
                raise ValueError(f"CUDA {name} session fell back to CPU; comparison unavailable.")
            session.disable_fallback()
            sessions[name] = {"providers": providers, "options": session.get_provider_options()}
        _validated_cuda_identity = {
            "backend": backend, "sessions": sessions, "device": cuda_device_identity(),
            "libraries": cuda_library_versions(),
        }
    return recognizer


def rectangle(value, width, height):
    if len(value) == 4 and isinstance(value[0], (int, float)):
        box = list(map(float, value))
    else:
        box = [float(min(p[0] for p in value)), float(min(p[1] for p in value)),
               float(max(p[0] for p in value)), float(max(p[1] for p in value))]
    if not all(math.isfinite(v) for v in box) or box[0] > box[2] or box[1] > box[3]:
        raise ValueError("OCR returned invalid coordinates.")
    return [min(max(v, 0.0), width if i % 2 == 0 else height) for i, v in enumerate(box)]


def recognize_page(recognizer, image, width, height):
    def visible_box(value):
        box = rectangle(value, width, height)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("OCR text has no visible location.")
        return box

    result = recognizer(image, use_cls=False, return_word_box=True)
    if result is None:
        raise ValueError("OCR did not return a page result.")
    if result.txts is None:
        return {"status": "completed", "lines": []}
    if len(result.txts) > MAX_LINES or not len(result.txts) == len(result.scores) == len(result.boxes):
        raise ValueError("OCR page response is incomplete or exceeds the line limit.")
    lines = []
    for index, (text, score, box) in enumerate(zip(result.txts, result.scores, result.boxes)):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000 or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            raise ValueError("OCR returned invalid text or confidence.")
        words = result.word_results[index] if index < len(result.word_results) else []
        regions = []
        for word, confidence, word_box in words:
            if word_box is not None:
                if not isinstance(word, str) or not word or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
                    raise ValueError("OCR returned invalid word evidence.")
                regions.append({"text": word, "score": float(confidence), "box": visible_box(word_box)})
        lines.append({"text": text, "score": float(score), "box": visible_box(box), "words": regions})
    return {"status": "completed", "lines": lines}


def extract_document(data, recognizer):
    from .scheduling import RecognizerPool
    if isinstance(recognizer, RecognizerPool):
        return recognizer.extract(data)
    return _extract_sequential(data, recognizer)


def rendered_pages(data):
    """Render/native geometry only on the caller thread, never OCR threads."""
    native_count = 0
    with fitz.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs cannot be compared.")
        if not 0 < len(document) <= MAX_PAGES:
            raise ValueError(f"OCR comparison permits 1-{MAX_PAGES} pages per PDF; review larger PDFs manually.")
        for number, page in enumerate(document, 1):
            width, height = math.ceil(page.rect.width * 300 / 72), math.ceil(page.rect.height * 300 / 72)
            if min(width, height) <= 0 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                raise ValueError("PDF page exceeds the full-resolution OCR raster limit.")
            pix = page.get_pixmap(dpi=300, alpha=False, colorspace=fitz.csRGB)
            import cv2
            import numpy as np
            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            sx, sy = pix.width / page.rect.width, pix.height / page.rect.height
            words = []
            for word in page.get_text("words", sort=False):
                native_count += 1
                if native_count > MAX_NATIVE_WORDS:
                    raise ValueError("Native location work limit exceeded.")
                rect = fitz.Rect(word[:4]) * page.rotation_matrix
                box = rectangle([rect.x0*sx, rect.y0*sy, rect.x1*sx, rect.y1*sy], pix.width, pix.height)
                if box[0] < box[2] and box[1] < box[3]:
                    words.append({"box": box, "block": word[5], "line": word[6], "word": word[7]})
            yield ({"page": number, "width": pix.width, "height": pix.height,
                    "pdf_width": page.rect.width, "pdf_height": page.rect.height},
                   {"page": number, "words": words}, image)


def _extract_sequential(data, recognizer):
    pages, native = [], []
    characters = 0
    for metadata, geometry, image in rendered_pages(data):
        ocr = recognize_page(recognizer, image, metadata["width"], metadata["height"])
        characters += sum(len(line["text"]) for line in ocr["lines"])
        if characters > MAX_CHARACTERS:
            raise ValueError("OCR character work limit exceeded.")
        pages.append({**metadata, "ocr": ocr})
        native.append(geometry)
    return pages, native
