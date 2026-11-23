"""Read-only, request-scoped comparison. No preview storage, ledger or cache."""

import hashlib

from django.core import signing

from .file_manager import publication_pdf_info
from .pdf_comparison import comparison_identity, comparison_submission, _live_bytes, _read_baseline
from .pdf_comparison_errors import PDFComparisonError
from .pdf_comparison_execution import local_pdf_evidence


BASELINE_SALT = "submissions.ephemeral-pdf-comparison.baseline.v1"


def read_baseline(submission_id, key):
    submission = comparison_submission(submission_id)
    if key == "original":
        data, info = _read_baseline(submission, key)
    elif key == "current":
        selected = publication_pdf_info(submission, include_url=False)
        data = _live_bytes(selected)
        info = {"key": key, "label": f"Current saved PDF ({selected['label']})", "filename": selected["filename"]}
    else:
        raise PDFComparisonError("Choose the Original or Current saved PDF as the baseline.")
    info["sha256"] = hashlib.sha256(data).hexdigest()
    binding = {"identity": comparison_identity(submission), "baseline": info}
    return data, binding


def compare_once(submission_id, token, draft, filename):
    try:
        expected = signing.loads(token, salt=BASELINE_SALT, max_age=900)
        key = expected["baseline"]["key"]
    except (signing.BadSignature, ValueError, TypeError, KeyError) as exc:
        raise PDFComparisonError("The baseline selection is invalid or expired. Choose it again.") from exc
    if expected["identity"]["submission_id"] != submission_id:
        raise PDFComparisonError("This baseline belongs to another submission.")
    before, binding = read_baseline(submission_id, key)
    if binding != expected:
        raise PDFComparisonError("The selected baseline changed. Compare again to use the new baseline.")
    if b"%PDF-" not in draft[:1024]:
        raise PDFComparisonError("Choose a valid PDF draft.")
    result = local_pdf_evidence(before, draft, ephemeral=True)
    # Revalidate the same source after OCR, never substitute a different file.
    del before
    if read_baseline(submission_id, key)[1] != expected:
        raise PDFComparisonError("The selected baseline changed during comparison. Compare again.")
    return {
        **result, "before": binding["baseline"],
        "after": {"label": "Unsaved draft", "filename": filename, "sha256": hashlib.sha256(draft).hexdigest()},
        "execution": {"label": "Local CPU"},
    }
