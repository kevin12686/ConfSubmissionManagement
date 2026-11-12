"""One bounded execution policy for native, CPU and CUDA workers."""
import os

PAGE_ENV = "CFM_RAPIDOCR_PAGES"
DETECTION_ENV = "CFM_RAPIDOCR_DET_MODEL"
RECOGNITION_ENV = "CFM_RAPIDOCR_REC_MODEL"
TIMEOUT_ENV = "CFM_RAPIDOCR_TIMEOUT"
GPU_IDLE_ENV = "CFM_GPU_MODEL_IDLE_SECONDS"
MAX_PARALLEL_PAGES = 8
DEFAULT_TIMEOUT = 300


def integer_setting(name, default, minimum, maximum):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def parallel_pages():
    # CFM_SLAVE_ROOT also identifies standalone workers in the cache runtime.
    # Embedded/native execution retains its single-page default.
    default = 4 if os.environ.get("CFM_OCR_BACKEND", "cpu") == "cuda" else (
        2 if os.environ.get("CFM_SLAVE_ROOT") else 1)
    return integer_setting(PAGE_ENV, default, 1, MAX_PARALLEL_PAGES)


def model_size(kind):
    name = {"det": DETECTION_ENV, "rec": RECOGNITION_ENV}[kind]
    value = os.environ.get(name, "small")
    if value not in {"small", "medium"}:
        raise ValueError(f"{name} must be small or medium.")
    return value


def timeout_seconds():
    return integer_setting(TIMEOUT_ENV, DEFAULT_TIMEOUT, 30, 3600)


def supervisor_seconds():
    return timeout_seconds() + 30


def gpu_idle_seconds():
    if os.environ.get("CFM_OCR_BACKEND", "cpu") != "cuda":
        return 0
    return integer_setting(GPU_IDLE_ENV, 1800, 0, 86400)


def execution_identity():
    return {"parallel_pages": parallel_pages(), "det": model_size("det"),
            "rec": model_size("rec"), "dpi": 300, "batch": 6}
