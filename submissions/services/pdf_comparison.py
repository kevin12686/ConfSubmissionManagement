"""Read-only, hash-bound comparisons; never publication or review evidence."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import struct
import tempfile
import uuid
from datetime import timedelta
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
COMPARISON_VERSION = 7
MAX_BYTES = 64 * 1024 * 1024


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



def _pdf_evidence(before, after):
    from submissions.services.rapidocr_comparison.result import VERSION, validate_result
    from submissions.services.rapidocr_comparison.runtime import WALL_SECONDS

    if not all(0 < len(data) <= MAX_BYTES for data in (before, after)):
        raise PDFComparisonError("Comparison requires two non-empty PDFs of at most 64 MiB each.")
    environment = os.environ.copy()
    environment.setdefault("CFM_RAPIDOCR_MODEL_DIR", str(Path(settings.BASE_DIR) / "runtime" / "rapidocr-models"))
    executable = environment.get("CFM_RAPIDOCR_PYTHON", sys.executable)
    if not Path(executable).is_absolute():
        raise PDFComparisonError("Comparison unavailable: CFM_RAPIDOCR_PYTHON must be an absolute executable path.")
    try:
        with tempfile.TemporaryFile() as diagnostics:
            response = subprocess.run(
                [executable, "-B", str(Path(__file__).with_name("pdf_comparison_worker.py"))],
                input=struct.pack(">QQ", len(before), len(after)) + before + after,
                stdout=subprocess.PIPE, stderr=diagnostics, timeout=WALL_SECONDS, check=True,
                env=environment,
            )
        if len(response.stdout) > 8 * 1024 * 1024:
            raise ValueError("OCR response exceeds the safe output limit")
        payload = json.loads(response.stdout)
        if not isinstance(payload, dict) or payload.get("version") != VERSION:
            raise ValueError("Invalid OCR response version")
        if "error" in payload:
            errors = {
                "configuration": "Local OCR models or dependencies are unavailable. Run prepare_rapidocr_models and install requirements.txt.",
                "ocr_failed": "OCR could not complete safely. Review both PDFs manually; no native-text fallback was used.",
            }
            raise PDFComparisonError("Comparison unavailable: " + errors.get(payload["error"], "OCR worker failed."))
        result = validate_result(payload.get("result"))
        if result["identical_bytes"] != (before == after):
            raise ValueError("Incorrect snapshot identity")
        return result
    except PDFComparisonError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        raise PDFComparisonError(
            "Comparison unavailable: OCR failed, exceeded its 240-second runtime or input limits, "
            "or returned invalid evidence. No complete comparison is available; review both PDFs manually."
        ) from exc


def compare_pdf_bytes(before_data, after_data):
    return _pdf_evidence(before_data, after_data)


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
        signed_payload = json.dumps({
            "created_at": payload["created_at"], "signed": signing.dumps(payload, salt=SALT, compress=True),
        })
        if len(signed_payload.encode("utf-8")) > 8 * 1024 * 1024:
            raise PDFComparisonError("The comparison result exceeds the safe snapshot limit. No complete comparison is available.")
        (directory / "payload.json").write_text(signed_payload, encoding="utf-8")
        audit_success(
            "formatting_pdf_compare", "Read-only PDF comparison created; publication files and review state unchanged.",
            request=request, submission=submission,
            file_hashes={"before": payload["before_sha256"], "after": payload["after_sha256"]},
            result_counts={"findings": result["total_findings"], "content": result["content_count"], "uncertain": result["uncertain_count"], "limited": result["coverage_status"] == "limited"},
            extra={"baseline": baseline_info, "publication_pdf_source": target_info["source"],
                   "comparison_method": COMPARISON_VERSION, "coverage_status": result["coverage_status"],
                   **({"automatic": True, "revision_id": expected_upload["revision_id"]} if expected_upload else {})},
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
