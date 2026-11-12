"""Versioned, content-bound compute contract. No conference/database access."""
import hashlib
import json
from pathlib import Path

import fitz

from .rapidocr_comparison.result import VERSION
from .rapidocr_comparison.runtime import MODEL_HASHES, MEDIUM_MODEL_HASHES

PROTOCOL = 1
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_REPLY_BYTES = 10 * 1024 * 1024


def contract_id():
    root = Path(__file__).parent
    paths = sorted((root / "rapidocr_comparison").glob("*.py"))
    paths += [root / "pdf_comparison_worker.py", Path(__file__)]
    contract = {
        "protocol": PROTOCOL, "method": VERSION, "models": {**MODEL_HASHES, **MEDIUM_MODEL_HASHES},
        "code": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def job_key(hashes, contract):
    return hashlib.sha256(json.dumps({"contract": contract, "hashes": hashes}, sort_keys=True).encode()).hexdigest()


def page_pixmap(page):
    # Inspection overlays and transport rendering checks share exactly one renderer.
    scale = min(1400 / page.rect.width, 2000 / page.rect.height, 2)
    return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)


def page_render_hashes(data):
    from .rapidocr_comparison.runtime import MAX_PAGES, MAX_PIXELS, MAX_SIDE
    with fitz.open(stream=data, filetype="pdf") as document:
        if document.needs_pass or not 0 < len(document) <= MAX_PAGES:
            raise ValueError("Unsupported PDF rendering bounds")
        result = []
        for page in document:
            if min(page.rect.width, page.rect.height) <= 0:
                raise ValueError("Invalid PDF page dimensions")
            pix = page_pixmap(page)
            if max(pix.width, pix.height) > MAX_SIDE or pix.width * pix.height > MAX_PIXELS:
                raise ValueError("PDF inspection rendering exceeds bounds")
            digest = hashlib.sha256()
            digest.update(f"{pix.width}:{pix.height}:{pix.n}:".encode())
            digest.update(pix.samples)
            result.append(digest.hexdigest())
        return result
