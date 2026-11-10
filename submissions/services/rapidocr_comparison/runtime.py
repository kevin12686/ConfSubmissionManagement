"""Local, pinned OCR models and bounded full-page recognition.

Native text strings never leave extraction: only geometry can guide OCR order.
"""
import hashlib
import importlib.metadata
import math
import os
from pathlib import Path

import fitz

MODEL_HASHES = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
}
MODEL_ENV = "CFM_RAPIDOCR_MODEL_DIR"
MAX_PAGES = 40
MAX_PIXELS = 16_000_000
MAX_SIDE = 4000
MAX_NATIVE_WORDS = 60_000
MAX_LINES = 2000
MAX_CHARACTERS = 300_000
WALL_SECONDS = 240
MAX_THREADS = 8
THREAD_ENV = "CFM_RAPIDOCR_THREADS"


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
    for name, expected in MODEL_HASHES.items():
        path = root / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError(f"Required local OCR model is missing or invalid: {name}.")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"Local OCR model checksum mismatch: {name}.")
    return root


def build_recognizer():
    root = validate_models()
    for name, version in (("rapidocr", "3.10.0"), ("onnxruntime", "1.31.1")):
        if importlib.metadata.version(name) != version:
            raise ValueError(f"OCR requires {name}=={version}; install requirements.txt.")
    from rapidocr import RapidOCR
    import onnxruntime
    onnxruntime.disable_telemetry_events()

    # Explicit model paths avoid RapidOCR's model-directory download branch.
    return RapidOCR(params={
        "Global.model_root_dir": str(root), "Global.text_score": 0.0,
        "Global.max_side_len": MAX_SIDE, "Global.use_cls": False,
        "Det.model_path": str(root / "PP-OCRv6_det_small.onnx"),
        "Rec.model_path": str(root / "PP-OCRv6_rec_small.onnx"),
        "Det.limit_side_len": 2560, "Det.limit_type": "max",
        "EngineConfig.onnxruntime.intra_op_num_threads": ocr_thread_count(),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })


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
    pages, native = [], []
    characters = native_count = 0
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
            ocr = recognize_page(recognizer, pix.tobytes("png"), pix.width, pix.height)
            characters += sum(len(line["text"]) for line in ocr["lines"])
            if characters > MAX_CHARACTERS:
                raise ValueError("OCR character work limit exceeded.")
            pages.append({"page": number, "width": pix.width, "height": pix.height,
                          "pdf_width": page.rect.width, "pdf_height": page.rect.height, "ocr": ocr})
            sx, sy = pix.width / page.rect.width, pix.height / page.rect.height
            words = []
            for w in page.get_text("words", sort=False):
                native_count += 1
                if native_count > MAX_NATIVE_WORDS:
                    raise ValueError("Native location work limit exceeded.")
                rect = fitz.Rect(w[:4]) * page.rotation_matrix
                box = rectangle([rect.x0 * sx, rect.y0 * sy, rect.x1 * sx, rect.y1 * sy], pix.width, pix.height)
                if box[0] < box[2] and box[1] < box[3]:
                    words.append({"box": box, "block": w[5], "line": w[6], "word": w[7]})
            native.append({"page": number, "words": words})
    return pages, native
