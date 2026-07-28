from django.db import transaction
from django.utils import timezone

from submissions.models import (
    FinalSubmission,
    InitialPaper,
    PUBLICATION_EXCLUSION_REASON_CHOICES,
)
from submissions.services.audit import audit_success
from submissions.services.final_submission_state import bulk_update_submissions
from submissions.services.text_utils import clean_note_text
from submissions.services.workflow_evidence import (
    paper_publication_decision_evidence,
    require_evidence_token,
)


PUBLISHING = "publishing"
NOT_PUBLISHING = "not_publishing"
DECISION_REQUIRED = "decision_required"

_VALID_EXCLUSION_REASONS = {
    value
    for value, _label in PUBLICATION_EXCLUSION_REASON_CHOICES
    if value
}
_VALID_MASTER_EXCLUSION_REASONS = {
    "unpaid",
    "withdrawn",
    "other",
}


def publication_master_papers():
    return InitialPaper.objects.filter(publication_decision_status=PUBLISHING)


def publication_master_paper_ids():
    return publication_master_papers().values_list("paper_id", flat=True)


def paper_is_not_publishing(paper):
    return bool(paper and paper.publication_decision_status == NOT_PUBLISHING)


def paper_requires_publication_decision(paper):
    return bool(paper and paper.publication_decision_status == DECISION_REQUIRED)


def master_paper_for_submission(submission):
    paper_id = (getattr(submission, "paper_id_filled", "") or "").strip()
    if not paper_id:
        return None
    return InitialPaper.objects.filter(paper_id=paper_id).first()


def submission_is_not_publishing(submission, *, master_by_id=None):
    paper_id = (getattr(submission, "paper_id_filled", "") or "").strip()
    if master_by_id is None:
        paper = master_paper_for_submission(submission)
    else:
        paper = master_by_id.get(paper_id)
    if paper is not None:
        return paper_is_not_publishing(paper)
    return bool(getattr(submission, "excluded_from_publication", False))


def new_master_publication_transition(paper_id, *, submissions=None):
    """Classify a new Master record without silently reviving an exclusion."""
    paper_id = (paper_id or "").strip()
    if submissions is None:
        submissions = FinalSubmission.objects.filter(
            paper_id_filled=paper_id
        ).order_by("pk")
    submissions = list(submissions)
    excluded = [
        submission
        for submission in submissions
        if submission.excluded_from_publication
    ]
    return {
        "publication_decision_status": (
            DECISION_REQUIRED if excluded else PUBLISHING
        ),
        "requires_decision": bool(excluded),
        "affected_final_count": len(submissions),
        "excluded_final_count": len(excluded),
        "excluded_final_ids": [
            submission.final_submission_id for submission in excluded
        ],
    }


@transaction.atomic
def create_paper_master_with_publication_guard(**values):
    """Create a Master row while preserving prior orphan decision evidence."""
    paper_id = (values.get("paper_id") or "").strip()
    if not paper_id:
        raise ValueError("Paper ID is required.")
    if InitialPaper.objects.select_for_update().filter(
        paper_id__iexact=paper_id
    ).exists():
        raise ValueError("Paper ID already exists.")
    submissions = list(
        FinalSubmission.objects.select_for_update()
        .filter(paper_id_filled=paper_id)
        .order_by("pk")
    )
    transition = new_master_publication_transition(
        paper_id,
        submissions=submissions,
    )
    values["paper_id"] = paper_id
    values["publication_decision_status"] = transition[
        "publication_decision_status"
    ]
    return InitialPaper.objects.create(**values), transition


def apply_master_decision_mirror(submission, paper=None):
    paper = paper or master_paper_for_submission(submission)
    if paper is None:
        return submission
    if paper_is_not_publishing(paper):
        submission.excluded_from_publication = True
        submission.publication_exclusion_reason = paper.publication_exclusion_reason
        submission.publication_exclusion_notes = paper.publication_exclusion_notes
        submission.publication_excluded_at = paper.publication_excluded_at
    elif paper.publication_decision_status == PUBLISHING:
        submission.excluded_from_publication = False
        submission.publication_exclusion_reason = ""
        submission.publication_exclusion_notes = ""
        submission.publication_excluded_at = None
    return submission


def publication_decision_integrity_rows(context):
    """Return fail-closed findings for Master/Final decision disagreement."""
    if not context.valid_paper_ids:
        return []
    grouped = {}
    submissions = (
        FinalSubmission.objects.filter(
            paper_id_filled__in=context.valid_paper_ids
        )
        .order_by("paper_id_filled", "final_submission_id", "pk")
    )
    for submission in submissions:
        grouped.setdefault(submission.paper_id_filled, []).append(submission)

    rows = []
    for paper in context.papers:
        if paper.publication_decision_status == DECISION_REQUIRED:
            continue
        expected_excluded = paper.publication_decision_status == NOT_PUBLISHING
        mismatches = [
            submission
            for submission in grouped.get(paper.paper_id, [])
            if submission.excluded_from_publication != expected_excluded
        ]
        if not mismatches:
            continue
        final_ids = ", ".join(
            submission.final_submission_id for submission in mismatches
        )
        expected_label = (
            "Not Publishing" if expected_excluded else "Publishing"
        )
        rows.append(
            {
                "category": "Publication Decision Integrity Conflict",
                "paper_id": paper.paper_id,
                "final_submission_id": "",
                "message": (
                    f"Paper Master is {expected_label}, but Final Submission "
                    f"decision mirrors disagree for: {final_ids}. Resolve the "
                    "publication decision before any publication export."
                ),
            }
        )
    return rows


def _validate_reason(reason, *, master_decision=False):
    reason = (reason or "").strip()
    valid_reasons = (
        _VALID_MASTER_EXCLUSION_REASONS
        if master_decision
        else _VALID_EXCLUSION_REASONS
    )
    if reason not in valid_reasons:
        raise ValueError("Select a valid Not Publishing reason.")
    return reason


def _mapped_submissions_for_update(paper):
    return list(
        FinalSubmission.objects.select_for_update()
        .filter(paper_id_filled=paper.paper_id)
        .order_by("pk")
    )


def _sync_legacy_mirrors(
    submissions,
    *,
    excluded,
    reason="",
    notes="",
    excluded_at=None,
):
    for submission in submissions:
        submission.excluded_from_publication = excluded
        submission.publication_exclusion_reason = reason if excluded else ""
        submission.publication_exclusion_notes = notes if excluded else ""
        submission.publication_excluded_at = excluded_at if excluded else None
    if submissions:
        bulk_update_submissions(
            submissions,
            [
                "excluded_from_publication",
                "publication_exclusion_reason",
                "publication_exclusion_notes",
                "publication_excluded_at",
            ],
        )


@transaction.atomic
def mark_paper_not_publishing(
    paper,
    reason,
    notes="",
    *,
    expected_evidence_token,
    request=None,
):
    from submissions.services.recompute import recompute_active_and_duplicate_state

    reason = _validate_reason(reason, master_decision=True)
    notes = clean_note_text(notes)
    current = InitialPaper.objects.select_for_update().get(pk=paper.pk)
    submissions = _mapped_submissions_for_update(current)
    before = paper_publication_decision_evidence(current, submissions)
    require_evidence_token(
        expected_evidence_token,
        "paper-publication-decision",
        before,
    )
    excluded_at = timezone.now()
    current.publication_decision_status = NOT_PUBLISHING
    current.publication_exclusion_reason = reason
    current.publication_exclusion_notes = notes
    current.publication_excluded_at = excluded_at
    current.save(
        update_fields=[
            "publication_decision_status",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
            "publication_excluded_at",
            "updated_at",
        ]
    )
    _sync_legacy_mirrors(
        submissions,
        excluded=True,
        reason=reason,
        notes=notes,
        excluded_at=excluded_at,
    )
    recompute_active_and_duplicate_state()
    after = paper_publication_decision_evidence(current, submissions)
    audit_success(
        "publication_exclusion_apply",
        "Paper Master record marked Not Publishing.",
        request=request,
        object_type="InitialPaper",
        paper_id=current.paper_id,
        changed_fields=[
            "publication_decision_status",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
            "publication_excluded_at",
        ],
        before=before,
        after=after,
        result_counts={"affected_final_versions": len(submissions)},
    )
    return current


@transaction.atomic
def undo_paper_not_publishing(
    paper,
    *,
    expected_evidence_token,
    request=None,
):
    from submissions.services.recompute import recompute_active_and_duplicate_state

    current = InitialPaper.objects.select_for_update().get(pk=paper.pk)
    submissions = _mapped_submissions_for_update(current)
    before = paper_publication_decision_evidence(current, submissions)
    require_evidence_token(
        expected_evidence_token,
        "paper-publication-decision",
        before,
    )
    current.publication_decision_status = PUBLISHING
    current.publication_exclusion_reason = ""
    current.publication_exclusion_notes = ""
    current.publication_excluded_at = None
    current.save(
        update_fields=[
            "publication_decision_status",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
            "publication_excluded_at",
            "updated_at",
        ]
    )
    _sync_legacy_mirrors(submissions, excluded=False)
    recompute_active_and_duplicate_state()
    after = paper_publication_decision_evidence(current, submissions)
    audit_success(
        "publication_exclusion_undo",
        "Paper Master record returned to publication scope.",
        request=request,
        object_type="InitialPaper",
        paper_id=current.paper_id,
        changed_fields=[
            "publication_decision_status",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
            "publication_excluded_at",
        ],
        before=before,
        after=after,
        result_counts={"affected_final_versions": len(submissions)},
    )
    return current
