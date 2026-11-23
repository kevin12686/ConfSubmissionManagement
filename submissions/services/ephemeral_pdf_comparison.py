"""Read-only, request-scoped comparison. No preview storage, ledger or cache."""

import hashlib

from django.conf import settings
from django.core import signing

from .comparison_execution_choice import confirm_local_token, validate_local_token
from .comparison_protocol import contract_id
from .file_manager import publication_pdf_info
from .pdf_comparison import comparison_identity, comparison_submission, _live_bytes, _read_baseline
from .pdf_comparison_errors import LocalComparisonConfirmationRequired, PDFComparisonError
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


def compare_once(submission_id, token, draft, filename, *, local_confirmation=""):
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
    draft_hash = hashlib.sha256(draft).hexdigest()
    consent_binding = {"mode": "one_time", "baseline": binding, "draft_sha256": draft_hash, "contract": contract_id()}
    allow_local = validate_local_token(local_confirmation, consent_binding)
    use_gpu = bool(settings.CFM_COMPARISON_REMOTE_URL.strip()) and not allow_local
    if use_gpu:
        from .comparison_client import one_time_gpu_evidence
        try:
            result = one_time_gpu_evidence(before, draft)
        except LocalComparisonConfirmationRequired as exc:
            if exc.can_confirm:
                exc.confirmation_token = confirm_local_token(consent_binding)
            raise
    else:
        result = local_pdf_evidence(before, draft, ephemeral=True, force_cpu=True)
    # Revalidate the same source after OCR, never substitute a different file.
    del before
    if read_baseline(submission_id, key)[1] != expected:
        raise PDFComparisonError("The selected baseline changed during comparison. Compare again.")
    return {
        **result, "before": binding["baseline"],
        "after": {"label": "Unsaved draft", "filename": filename, "sha256": draft_hash},
        "execution": {"label": "GPU" if use_gpu else "Local CPU"},
    }
