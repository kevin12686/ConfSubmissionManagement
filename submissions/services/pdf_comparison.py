"""Read-only, hash-bound comparisons; never publication or review evidence."""

import hashlib
import json
import math
import re
import shutil
import unicodedata
import uuid
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path

import fitz
from django.conf import settings
from django.core import signing
from django.utils import timezone

from submissions.models import FinalSubmission, FormattingRevision
from submissions.services.audit import audit_failure, audit_success
from submissions.services.file_inspection import FileChangedDuringInspection, FileInspectionContext
from submissions.services.file_manager import publication_pdf_info
from submissions.services.formatting_history import open_revision_file
from submissions.services.preview_storage import PREVIEW_TOKEN_PATTERN, purge_expired_preview_directories
from submissions.services.publication_decisions import publication_master_paper_ids


TTL = timedelta(hours=2)
SALT = "submissions.pdf-comparison.v1"
COMPARISON_VERSION = 2
MAX_BYTES = 64 * 1024 * 1024
MAX_PAGES = 100
MAX_TOKENS = 30000
MAX_FINDINGS = 300
MAX_BOXES = 160
MAX_PAIRING_STEPS = 250000
TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
ERROR_PATTERN = re.compile(r"Error!\s*(?:Reference source not found|Bookmark not defined)\.?", re.I)
PLACEHOLDER_PATTERN = re.compile(r"\[\s*\?\s*\]|\?\?")


class PDFComparisonError(ValueError):
    pass


class StaleUploadComparison(PDFComparisonError):
    pass


def comparison_root(*, create=False):
    root = Path(settings.BASE_DIR) / "data" / "formatting_comparison_previews"
    if root.is_symlink():
        raise PDFComparisonError("The comparison staging folder cannot be a symbolic link.")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def comparison_submission(submission_id):
    submission = FinalSubmission.objects.filter(
        pk=submission_id, active_version=True, discarded=False,
        paper_id_filled__in=publication_master_paper_ids(),
    ).first()
    if not submission:
        raise PDFComparisonError("This submission is no longer a current Publishing paper. No other version was substituted.")
    if FinalSubmission.objects.filter(
        paper_id_filled=submission.paper_id_filled, active_version=True, discarded=False,
    ).exclude(pk=submission.pk).exists():
        raise PDFComparisonError("Multiple active submissions exist for this paper. Resolve the version ambiguity before comparing publication files.")
    return submission


def comparison_identity(submission):
    return {
        "submission_id": submission.pk, "paper_id": submission.paper_id_filled,
        "final_id": submission.final_submission_id, "origin": submission.submission_origin,
        "original_name": submission.pdf_file.name or "",
        "corrected_name": submission.formatted_pdf_file.name or "",
    }


def validate_upload_binding(submission, binding, *, pdf_sha256=None):
    revision = FormattingRevision.objects.filter(
        pk=binding["revision_id"], submission_id=submission.pk,
        revision_key=binding["revision_key"], event_type="upload",
    ).first()
    if (
        not revision or "pdf" not in revision.changed_files
        or comparison_identity(submission) != binding["identity"]
        or revision.pdf_metadata.get("sha256") != binding["pdf_sha256"]
        or revision.pdf_metadata.get("live_name") != submission.formatted_pdf_file.name
        or revision.pdf_metadata.get("uploaded_at") != (
            submission.formatted_pdf_uploaded_at.isoformat() if submission.formatted_pdf_uploaded_at else None
        )
        or any("pdf" in changed for changed in FormattingRevision.objects.filter(
            submission_id=submission.pk, number__gt=revision.number,
        ).values_list("changed_files", flat=True))
        or (pdf_sha256 is not None and pdf_sha256 != binding["pdf_sha256"])
    ):
        raise StaleUploadComparison("This upload is no longer the current saved revision. No newer PDF was substituted. Open Compare PDF for the current files.")
    return revision


def _live_bytes(info):
    if not info["exists"]:
        raise PDFComparisonError("The selected PDF is missing. No other version was substituted.")
    path = Path(info["path"])
    if path.stat().st_size > MAX_BYTES:
        raise PDFComparisonError("This PDF exceeds the 64 MiB comparison limit. Use the original PDF viewer for manual review.")
    try:
        data = FileInspectionContext().read_snapshot_bytes(path)
    except FileChangedDuringInspection as exc:
        raise PDFComparisonError("The PDF changed while being read. Reload the current files and compare again.") from exc
    if len(data) > MAX_BYTES:
        raise PDFComparisonError("The PDF grew beyond the comparison limit. Reload the current files.")
    return data


def _original_info(submission):
    field = submission.pdf_file
    return {
        "exists": bool(field and field.storage.exists(field.name)),
        "path": field.path if field else "",
    }


def _read_baseline(submission, baseline):
    if baseline == "original":
        data = _live_bytes(_original_info(submission))
        return data, {
            "key": "original", "label": "Original submission",
            "filename": submission.original_file_name or Path(submission.pdf_file.name).name,
        }
    if not re.fullmatch(r"revision-[1-9][0-9]*", baseline):
        raise PDFComparisonError("Choose an original or a revision belonging to this submission.")
    revision = FormattingRevision.objects.filter(pk=int(baseline[9:]), submission=submission).first()
    if not revision:
        raise PDFComparisonError("This revision does not belong to the selected submission.")
    if (revision.pdf_metadata.get("size") or 0) > MAX_BYTES:
        raise PDFComparisonError("The revision PDF exceeds the comparison size limit.")
    with open_revision_file(revision, "pdf") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise PDFComparisonError("The revision PDF exceeds the comparison size limit.")
    return data, {
        "key": baseline, "label": f"Revision {revision.number}",
        "filename": revision.pdf_metadata.get("filename") or f"revision-{revision.number}.pdf",
    }


def _extract(data):
    words, tokens, pages = [], [], []
    with fitz.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            raise PDFComparisonError("Password-protected PDFs cannot be compared. No file was decrypted or modified.")
        if not 0 < len(document) <= MAX_PAGES:
            raise PDFComparisonError("Comparison supports PDFs with 1-100 pages. Open these PDFs manually instead.")
        for page_number, page in enumerate(document, 1):
            rect = page.rect
            if not all(math.isfinite(v) and v > 0 for v in (rect.width, rect.height)):
                raise PDFComparisonError("The PDF has invalid page dimensions.")
            pages.append({"number": page_number, "width": rect.width, "height": rect.height})
            for word in page.get_text("words", sort=True):
                text = unicodedata.normalize("NFC", word[4])
                box = fitz.Rect(word[:4]) * page.rotation_matrix
                if not all(math.isfinite(value) for value in box):
                    raise PDFComparisonError("The PDF has invalid text coordinates.")
                geometry = [
                    max(0, min(1, box.x0 / rect.width)), max(0, min(1, box.y0 / rect.height)),
                    max(0, min(1, box.x1 / rect.width)), max(0, min(1, box.y1 / rect.height)),
                ]
                item = {"text": text, "page": page_number, "box": geometry}
                words.append(item)
                for match in TOKEN_PATTERN.finditer(text):
                    tokens.append({**item, "text": match.group(), "word": text})
                if len(tokens) > MAX_TOKENS:
                    raise PDFComparisonError("This PDF exceeds the 30,000-token comparison limit. No incomplete comparison was presented as complete.")
    return {"words": words, "tokens": tokens, "pages": pages}


def _locations(items):
    seen, result = set(), []
    for item in items:
        key = (item["page"], tuple(item["box"]))
        if key not in seen:
            result.append({"page": item["page"], "box": item["box"]})
            seen.add(key)
        if len(result) >= MAX_BOXES:
            break
    return result


def _snippet(items):
    words, previous = [], None
    for item in items:
        key = (item["page"], tuple(item["box"]))
        if key != previous:
            words.append(item.get("word", item["text"]))
        previous = key
    text = " ".join(words)
    return text[:1200] + ("..." if len(text) > 1200 else "")


def _scanned_findings(before, after):
    findings = []
    for category, label, pattern in (
        ("reference", "New reference error", ERROR_PATTERN),
        ("placeholder", "Possible unresolved reference", PLACEHOLDER_PATTERN),
        ("character", "New replacement character", re.compile("\ufffd")),
    ):
        texts, offsets = [], []
        for document in (before, after):
            parts, starts, offset = [], [], 0
            for word in document["words"]:
                starts.append(offset)
                parts.append(word["text"])
                offset += len(word["text"]) + 1
            texts.append(" ".join(parts))
            offsets.append(starts)
        previous = Counter(" ".join(match.group().lower().split()) for match in pattern.finditer(texts[0]))
        for match in pattern.finditer(texts[1]):
            key = " ".join(match.group().lower().split())
            if previous[key]:
                previous[key] -= 1
                continue
            first = max(0, bisect_right(offsets[1], match.start()) - 1)
            last = bisect_right(offsets[1], match.end() - 1)
            items = after["words"][first:last]
            findings.append({
                "category": category, "label": label if before["words"] else {
                    "reference": "Reference error pattern", "placeholder": "Possible unresolved reference",
                    "character": "Replacement character pattern",
                }[category], "before_text": "",
                "after_text": match.group(), "before_locations": [],
                "after_locations": _locations(items),
                "before_page": None, "after_page": items[0]["page"] if items else 1,
                "before_pages": [], "after_pages": sorted({item["page"] for item in items}),
            })
            if len(findings) >= MAX_FINDINGS:
                return findings
    return findings


def _word_key(token):
    return token["page"], tuple(token["box"])


def _unique_seeds(tokens):
    """Count seeds over the whole document, including already aligned text."""
    positions = {}
    for index in range(len(tokens) - 2):
        key = tuple(item["text"] for item in tokens[index:index + 3])
        positions[key] = -1 if key in positions else index
    return positions


def _sequence_pairs(a, b, changes):
    if not changes:
        return [], set(), set(), False
    owners_a, owners_b = [None] * len(a), [None] * len(b)
    for _, a0, a1, b0, b1 in changes:
        owners_a[a0:a1] = [(a0, a1)] * (a1 - a0)
        owners_b[b0:b1] = [(b0, b1)] * (b1 - b0)
    used_a, used_b, pairs = set(), set(), []
    seeds_a, seeds_b = _unique_seeds(a), _unique_seeds(b)
    steps = 0
    for key, i in seeds_a.items():
        j = seeds_b.get(key, -1)
        if i < 0 or j < 0 or i in used_a or j in used_b:
            continue
        left, right = owners_a[i], owners_b[j]
        if not left or not right or i + 3 > left[1] or j + 3 > right[1]:
            continue
        a0, b0, a1, b1 = i, j, i + 3, j + 3
        while a0 > left[0] and b0 > right[0] and a0 - 1 not in used_a and b0 - 1 not in used_b:
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            if a[a0 - 1]["text"] != b[b0 - 1]["text"]:
                break
            a0, b0 = a0 - 1, b0 - 1
        while a1 < left[1] and b1 < right[1] and a1 not in used_a and b1 not in used_b:
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            if a[a1]["text"] != b[b1]["text"]:
                break
            a1, b1 = a1 + 1, b1 + 1
        # Do not reclassify a matching prefix/suffix inside a changed PDF word.
        while a0 < a1 and (
            (a0 > 0 and _word_key(a[a0]) == _word_key(a[a0 - 1]))
            or (b0 > 0 and _word_key(b[b0]) == _word_key(b[b0 - 1]))
        ):
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            a0, b0 = a0 + 1, b0 + 1
        while a1 > a0 and (
            (a1 < len(a) and _word_key(a[a1]) == _word_key(a[a1 - 1]))
            or (b1 < len(b) and _word_key(b[b1]) == _word_key(b[b1 - 1]))
        ):
            steps += 1
            if steps > MAX_PAIRING_STEPS:
                return pairs, used_a, used_b, True
            a1, b1 = a1 - 1, b1 - 1
        if a1 - a0 < 3 or not (a0 <= i and i + 3 <= a1):
            continue
        if len({_word_key(item) for item in a[a0:a1] if any(c.isalpha() for c in item["text"])}) < 2:
            continue
        if any(index in used_a for index in range(a0, a1)) or any(index in used_b for index in range(b0, b1)):
            continue
        pairs.append((a0, a1, b0, b1))
        used_a.update(range(a0, a1))
        used_b.update(range(b0, b1))
    return pairs, used_a, used_b, False


def _remaining_ranges(start, end, used):
    ranges = []
    for index in range(start, end):
        if index in used:
            continue
        if ranges and ranges[-1][1] == index:
            ranges[-1] = (ranges[-1][0], index + 1)
        else:
            ranges.append((index, index + 1))
    return ranges


def _text_finding(a, b, a0, a1, b0, b1, *, sequence=False):
    left, right = a[a0:a1], b[b0:b1]
    pages_a, pages_b = sorted({item["page"] for item in left}), sorted({item["page"] for item in right})
    return {
        "category": "sequence" if sequence else "text",
        "label": "Same text, different sequence position" if sequence else (
            "Changed text" if left and right else "Unmatched baseline text" if left else "Added text"
        ),
        "before_text": _snippet(left), "after_text": _snippet(right),
        "before_context": _snippet(a[max(0, a0 - 12):min(len(a), a1 + 12)]),
        "after_context": _snippet(b[max(0, b0 - 12):min(len(b), b1 + 12)]),
        "before_locations": _locations(left), "after_locations": _locations(right),
        "before_pages": pages_a, "after_pages": pages_b,
        "before_page": pages_a[0] if pages_a else None,
        "after_page": pages_b[0] if pages_b else None,
        "before_token_range": [a0, a1], "after_token_range": [b0, b1],
        "locations_truncated": len({_word_key(item) for item in left}) > MAX_BOXES or len({_word_key(item) for item in right}) > MAX_BOXES,
    }


def _page_maps(a, b, opcodes):
    votes = defaultdict(Counter)
    for operation, a0, a1, b0, b1 in opcodes:
        if operation == "equal":
            for left, right in zip(a[a0:a1], b[b0:b1]):
                votes[left["page"]][right["page"]] += 1
    page_map = {}
    inverse_map = {}
    before_page_sizes = Counter(item["page"] for item in a)
    after_page_sizes = Counter(item["page"] for item in b)
    for number, counts in votes.items():
        candidate, count = counts.most_common(1)[0]
        # Avoid inventing a page correspondence from a page number or a lone header.
        if (
            count >= 12 and count / sum(counts.values()) >= 0.6
            and count / before_page_sizes[number] >= 0.35
            and count / after_page_sizes[candidate] >= 0.35
        ):
            page_map[str(number)] = candidate
    for target_page in sorted(after_page_sizes):
        candidates = [(votes[int(left)][target_page], int(left)) for left in page_map if page_map[left] == target_page]
        if candidates:
            inverse_map[str(target_page)] = max(candidates)[1]
    return page_map, inverse_map


def compare_pdf_bytes(before_data, after_data):
    before, after = _extract(before_data), _extract(after_data)
    a, b = before["tokens"], after["tokens"]
    findings = _scanned_findings(before, after)
    patterns_limited = len(findings) >= MAX_FINDINGS
    total_findings = len(findings)

    def add_finding(a0, a1, b0, b1, *, sequence=False):
        nonlocal total_findings
        total_findings += 1
        if len(findings) < MAX_FINDINGS:
            findings.append(_text_finding(a, b, a0, a1, b0, b1, sequence=sequence))

    opcodes = SequenceMatcher(None, [item["text"] for item in a], [item["text"] for item in b]).get_opcodes() if a and b else []
    changes = [row for row in opcodes if row[0] != "equal"]
    pairs, used_a, used_b, pairing_limited = _sequence_pairs(a, b, changes)
    remaining_a = remaining_b = 0
    for _, a0, a1, b0, b1 in changes:
        ranges_a, ranges_b = _remaining_ranges(a0, a1, used_a), _remaining_ranges(b0, b1, used_b)
        remaining_a += sum(end - start for start, end in ranges_a)
        remaining_b += sum(end - start for start, end in ranges_b)
        if len(ranges_a) == len(ranges_b) == 1:
            add_finding(*ranges_a[0], *ranges_b[0])
        else:
            for start, end in ranges_a:
                add_finding(start, end, b0, b0)
            for start, end in ranges_b:
                add_finding(a0, a0, start, end)
    for a0, a1, b0, b1 in pairs:
        add_finding(a0, a1, b0, b1, sequence=True)
    equal = sum(a1 - a0 for operation, a0, a1, _, _ in opcodes if operation == "equal")
    if a and b and (equal + len(used_a) + remaining_a != len(a) or equal + len(used_b) + remaining_b != len(b)):
        raise PDFComparisonError("Text accounting failed. No incomplete comparison was presented as complete.")
    page_map, inverse_map = _page_maps(a, b, opcodes)
    for index, finding in enumerate(findings):
        finding["id"] = index
        finding["before_reference_page"] = inverse_map.get(str(finding["after_page"])) if not finding["before_pages"] else None
        finding["after_reference_page"] = page_map.get(str(finding["before_page"])) if not finding["after_pages"] else None
    return {
        "findings": findings, "counts": dict(Counter(item["category"] for item in findings)),
        "before_pages": before["pages"], "after_pages": after["pages"],
        "page_map": page_map, "inverse_map": inverse_map,
        "text_available": bool(a and b), "truncated": patterns_limited or total_findings > MAX_FINDINGS,
        "pairing_limited": pairing_limited,
        "attention_count": sum(item["category"] != "sequence" for item in findings),
        "sequence_count": sum(item["category"] == "sequence" for item in findings),
        "token_coverage": {"comparable": bool(a and b), "aligned": equal, "paired": len(used_a), "unmatched_before": remaining_a, "unmatched_after": remaining_b, "before_total": len(a), "after_total": len(b)},
        "identical_bytes": before_data == after_data,
    }


def create_pdf_comparison(submission_id, baseline="original", *, request=None, expected_upload=None):
    token = uuid.uuid4().hex
    directory = None
    submission = None
    try:
        submission = comparison_submission(submission_id)
        identity = comparison_identity(submission)
        if expected_upload is not None:
            if baseline != "original":
                raise PDFComparisonError("Automatic upload comparison must use the Original PDF.")
            validate_upload_binding(submission, expected_upload)
        target_info = publication_pdf_info(submission, include_url=False)
        after = _live_bytes(target_info)
        if expected_upload is not None:
            validate_upload_binding(submission, expected_upload, pdf_sha256=hashlib.sha256(after).hexdigest())
        before, baseline_info = _read_baseline(submission, baseline)
        result = compare_pdf_bytes(before, after)
        current = comparison_submission(submission_id)
        current_info = publication_pdf_info(current, include_url=False)
        if comparison_identity(current) != identity or hashlib.sha256(_live_bytes(current_info)).digest() != hashlib.sha256(after).digest():
            raise PDFComparisonError("The publication PDF changed during comparison. Reload the current files and compare again.")
        new_before, _ = _read_baseline(current, baseline)
        if hashlib.sha256(new_before).digest() != hashlib.sha256(before).digest():
            raise PDFComparisonError("The baseline changed during comparison. Compare again.")
        if expected_upload is not None:
            validate_upload_binding(current, expected_upload, pdf_sha256=hashlib.sha256(after).hexdigest())
        root = comparison_root(create=True)
        purge_expired_preview_directories(root, TTL)
        candidate_directory = root / token
        candidate_directory.mkdir(exist_ok=False)
        directory = candidate_directory
        payload = {
            "version": COMPARISON_VERSION, "token": token, "created_at": timezone.now().isoformat(),
            "identity": identity, "baseline": baseline_info,
            "target": {"label": target_info["label"], "filename": target_info["filename"]},
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(), "result": result,
        }
        if expected_upload is not None:
            payload["upload_binding"] = expected_upload
        # A killed writer still leaves a dated staging folder that can expire.
        (directory / "payload.json").write_text(json.dumps({"created_at": payload["created_at"]}), encoding="utf-8")
        (directory / "before.pdf").write_bytes(before)
        (directory / "after.pdf").write_bytes(after)
        (directory / "payload.json").write_text(json.dumps({
            "created_at": payload["created_at"], "signed": signing.dumps(payload, salt=SALT, compress=True),
        }), encoding="utf-8")
        audit_success(
            "formatting_pdf_compare", "Read-only PDF comparison created; publication files and review state unchanged.",
            request=request, submission=submission,
            file_hashes={"before": payload["before_sha256"], "after": payload["after_sha256"]},
            result_counts={"findings": len(result["findings"]), "limited": result["truncated"]},
            extra={"baseline": baseline_info, "publication_pdf_source": target_info["source"], **({"automatic": True, "revision_id": expected_upload["revision_id"]} if expected_upload else {})},
        )
        return payload
    except Exception as exc:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        audit_failure("formatting_pdf_compare", exc, "PDF comparison could not be created; no publication or review data changed.", request=request, submission=submission)
        if isinstance(exc, PDFComparisonError):
            raise
        if isinstance(exc, (OSError, ValueError, RuntimeError)):
            raise PDFComparisonError(str(exc)) from exc
        raise


def load_pdf_comparison(submission_id, token, *, validate_current=True):
    if not isinstance(token, str) or not PREVIEW_TOKEN_PATTERN.fullmatch(token):
        raise PDFComparisonError("Invalid PDF comparison token.")
    root = comparison_root()
    directory = root / token
    if directory.is_symlink():
        raise PDFComparisonError("Invalid PDF comparison folder.")
    try:
        path = directory / "payload.json"
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            raise PDFComparisonError("Invalid PDF comparison payload.")
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        payload = signing.loads(wrapper["signed"], salt=SALT, max_age=int(TTL.total_seconds()))
    except (OSError, KeyError, ValueError, signing.BadSignature) as exc:
        raise PDFComparisonError("Comparison expired or unavailable. Build a new comparison.") from exc
    if payload.get("version") != COMPARISON_VERSION:
        raise PDFComparisonError("This comparison uses an older comparison method. Build a new comparison; the saved PDFs are unchanged.")
    if payload["token"] != token or payload["identity"]["submission_id"] != submission_id:
        raise PDFComparisonError("This comparison does not belong to the selected submission.")
    if validate_current:
        current = comparison_submission(submission_id)
        info = publication_pdf_info(current, include_url=False)
        if comparison_identity(current) != payload["identity"] or hashlib.sha256(_live_bytes(info)).hexdigest() != payload["after_sha256"]:
            raise PDFComparisonError("The publication PDF changed after this comparison. Build a new comparison of the current files.")
        data, _ = _read_baseline(current, payload["baseline"]["key"])
        if hashlib.sha256(data).hexdigest() != payload["before_sha256"]:
            raise PDFComparisonError("The baseline changed after this comparison. Build a new comparison.")
        if payload.get("upload_binding"):
            validate_upload_binding(current, payload["upload_binding"], pdf_sha256=payload["after_sha256"])
    return payload


def comparison_pdf_bytes(submission_id, token, side):
    if side not in {"before", "after"}:
        raise PDFComparisonError("Unknown comparison side.")
    payload = load_pdf_comparison(submission_id, token)
    path = comparison_root() / token / f"{side}.pdf"
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise PDFComparisonError("Invalid comparison snapshot.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != payload[f"{side}_sha256"]:
        raise PDFComparisonError("The comparison snapshot failed its integrity check. Build a new comparison.")
    return data


def comparison_page_png(submission_id, token, side, page_number):
    data = comparison_pdf_bytes(submission_id, token, side)
    with fitz.open(stream=data, filetype="pdf") as document:
        if not 1 <= page_number <= len(document):
            raise PDFComparisonError("This page does not exist in the compared PDF.")
        page = document[page_number - 1]
        scale = min(1400 / page.rect.width, 2000 / page.rect.height, 2)
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
