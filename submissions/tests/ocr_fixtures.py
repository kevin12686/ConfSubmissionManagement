"""Explicit synthetic OCR for workflow tests, never native-text recognition.

Method 7 retires glyph mapping, outline equality, render-source proof and exact
script-role contracts. OCR accounting is covered in test_rapidocr_acceptance;
recognition itself is exercised by scripts/accept_rapidocr_real3pairs.py.
"""

import json
import struct
import subprocess

import fitz


def ocr_pages(text, *, count=1, score=0.99):
    return [{"page": number, "width": 595, "height": 842, "ocr": {
        "status": "completed", "lines": ([{
            "text": text, "score": score, "box": [55, 65, 540, 90], "words": [],
        }] if text and number == 1 else []),
    }} for number in range(1, count + 1)]


def synthetic_ocr_worker(command, *, input, **kwargs):
    """Use PDF page metadata only; returned OCR words are fixed test data."""
    from submissions.services.rapidocr_comparison.result import build_result

    left_length, right_length = struct.unpack(">QQ", input[:16])
    before = input[16:16 + left_length]
    after = input[16 + left_length:]
    if len(after) != right_length:
        raise AssertionError("Invalid worker framing")
    try:
        with fitz.open(stream=before, filetype="pdf") as document:
            before_count = len(document)
        with fitz.open(stream=after, filetype="pdf") as document:
            after_count = len(document)
    except Exception as exc:
        raise subprocess.CalledProcessError(1, command, stderr=b"Invalid PDF") from exc
    context = " This independent research protocol preserves detailed evidence for comparison."
    left = "The results are shown in Table 2. Participation was voluntary." + context
    right = "The results are shown in Error! Reference source not found. Participation was voluntary." + context
    result = build_result(ocr_pages(left, count=before_count),
                          ocr_pages(left if before == after else right, count=after_count),
                          identical_bytes=before == after)
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"version": 7, "result": result}).encode())
