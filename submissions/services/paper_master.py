from django.db import transaction

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.audit import audit_success
from submissions.services.verification import (
    reset_paper_id_review_for_master_title_change,
)
from submissions.services.workflow_evidence import (
    paper_master_delete_evidence,
    paper_master_edit_evidence,
    require_evidence_token,
)


@transaction.atomic
def apply_initial_paper_manual_edit(
    paper,
    form,
    *,
    expected_evidence_token,
    request=None,
):
    current = InitialPaper.objects.select_for_update().get(pk=paper.pk)
    require_evidence_token(
        expected_evidence_token,
        "paper-master-edit",
        paper_master_edit_evidence(current),
    )
    updated = form.save(commit=False)
    if (
        updated.paper_id != current.paper_id
        and FinalSubmission.objects.filter(
            paper_id_filled=current.paper_id
        ).exists()
    ):
        raise ValueError(
            "Paper ID cannot be renamed while Final Submissions are mapped to it. "
            "Create the new Paper Master ID, remap and verify the Final Submissions, "
            "then remove the old record."
        )

    before = paper_master_edit_evidence(current)
    title_changed = updated.title != current.title
    updated.save()
    affected_final_count = 0
    if title_changed:
        affected_final_count = reset_paper_id_review_for_master_title_change(
            updated.paper_id,
            "Master Title changed; Paper ID review required again.",
        )
    audit_success(
        "paper_master_save",
        "Paper master record saved.",
        request=request,
        object_type="InitialPaper",
        paper_id=updated.paper_id,
        changed_fields=form.changed_data,
        before=before,
        after=paper_master_edit_evidence(updated),
        reset_flags={
            "paper_id_review": title_changed,
            "affected_final_count": affected_final_count,
        },
    )
    return updated, {
        "paper_id_review_reset": title_changed,
        "affected_final_count": affected_final_count,
    }


@transaction.atomic
def delete_initial_paper(
    paper,
    *,
    expected_evidence_token,
    request=None,
):
    current = InitialPaper.objects.select_for_update().get(pk=paper.pk)
    mapped_submissions = list(
        FinalSubmission.objects.select_for_update()
        .filter(paper_id_filled=current.paper_id)
        .order_by("pk")
    )
    evidence = paper_master_delete_evidence(
        current,
        mapped_submissions,
    )
    require_evidence_token(
        expected_evidence_token,
        "paper-master-delete",
        evidence,
    )
    if mapped_submissions:
        raise ValueError(
            "Paper Master record cannot be deleted while Final Submissions are "
            "mapped to it. Remap those records first; keep old Final versions for "
            "traceability."
        )
    paper_id = current.paper_id
    current.delete()
    audit_success(
        "paper_master_delete",
        "Paper master record deleted.",
        request=request,
        object_type="InitialPaper",
        paper_id=paper_id,
        before=evidence,
    )
    return paper_id
