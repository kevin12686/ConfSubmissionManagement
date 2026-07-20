import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from django.core import signing
from django.db import models


EVIDENCE_SALT = "submissions.workflow-evidence.v1"
EVIDENCE_MAX_AGE_SECONDS = 2 * 60 * 60


class StaleWorkflowEvidence(ValueError):
    pass


def make_evidence_token(kind, evidence):
    payload = {
        "kind": kind,
        "digest": _evidence_digest(evidence),
    }
    return signing.dumps(payload, salt=EVIDENCE_SALT, compress=True)


def require_evidence_token(token, kind, evidence):
    if not token:
        raise StaleWorkflowEvidence(
            "This action is missing its review evidence. Reload the page and review "
            "the current data before trying again."
        )
    try:
        payload = signing.loads(
            token,
            salt=EVIDENCE_SALT,
            max_age=EVIDENCE_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired as exc:
        raise StaleWorkflowEvidence(
            "This review page expired. Reload it and review the current data before "
            "trying again."
        ) from exc
    except signing.BadSignature as exc:
        raise StaleWorkflowEvidence(
            "This review evidence is invalid. Reload the page before trying again."
        ) from exc
    if (
        payload.get("kind") != kind
        or payload.get("digest") != _evidence_digest(evidence)
    ):
        raise StaleWorkflowEvidence(
            "The record changed after this page was loaded. No change was applied; "
            "reload and review the current data."
        )


def final_submission_edit_evidence(submission):
    return final_submission_state_evidence(submission)


def final_submission_state_evidence(submission):
    """Return semantic persisted row state without reading referenced files."""
    state = {}
    for field in submission._meta.concrete_fields:
        if field.name in {"created_at", "updated_at"}:
            continue
        value = getattr(submission, field.attname)
        if isinstance(field, models.FileField):
            value = value.name or ""
        elif isinstance(field, models.DecimalField) and value is not None:
            value = format(
                Decimal(str(value)).quantize(
                    Decimal(1).scaleb(-field.decimal_places)
                ),
                "f",
            )
        elif isinstance(field, models.FloatField) and value is not None:
            value = float(value)
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()
        state[field.attname] = value
    return state


def app_setting_evidence(settings_obj):
    state = {}
    for field in settings_obj._meta.concrete_fields:
        value = getattr(settings_obj, field.attname)
        state[field.attname] = value
    return state


def paper_id_review_evidence(
    submission,
    paper_candidates,
    suggested_paper=None,
    suggested_score=None,
    *,
    paper_master_digest=None,
):
    return {
        "submission": final_submission_state_evidence(submission),
        "paper_master_digest": (
            paper_master_digest
            if paper_master_digest is not None
            else paper_master_review_digest(paper_candidates)
        ),
        "suggested_paper_id": suggested_paper.paper_id if suggested_paper else "",
        "suggested_score": suggested_score,
    }


def paper_master_review_digest(paper_candidates):
    evidence = [
        {
            "pk": paper.pk,
            "paper_id": paper.paper_id,
            "acceptance_status": paper.acceptance_status,
            "title": paper.title,
            "authors": paper.authors,
        }
        for paper in sorted(
            paper_candidates,
            key=lambda item: (item.paper_id, item.pk),
        )
    ]
    return _evidence_digest(evidence)


def submission_group_evidence(submission, group):
    return {
        "target_pk": submission.pk,
        "paper_id": (submission.paper_id_filled or "").strip(),
        "members": [
            final_submission_state_evidence(item)
            for item in sorted(group, key=lambda member: member.pk)
        ],
    }


def duplicate_author_review_evidence(submission):
    return final_submission_state_evidence(submission)


def title_author_manual_override_evidence(submission):
    return final_submission_state_evidence(submission)


def paper_master_edit_evidence(paper):
    return {
        "pk": paper.pk,
        "paper_id": paper.paper_id,
        "acceptance_status": paper.acceptance_status,
        "title": paper.title,
        "authors": paper.authors,
        "notes": paper.notes,
    }


def paper_master_delete_evidence(paper, mapped_submissions):
    return {
        "paper": paper_master_edit_evidence(paper),
        "mapped_submissions": [
            final_submission_state_evidence(submission)
            for submission in sorted(
                mapped_submissions,
                key=lambda item: item.pk,
            )
        ],
    }


def title_author_review_evidence(submission):
    return {
        "pk": submission.pk,
        "paper_id_filled": submission.paper_id_filled,
        "final_submission_id": submission.final_submission_id,
        "final_submission_title": submission.final_submission_title,
        "extracted_title": submission.extracted_title,
        "extracted_authors": submission.extracted_authors,
        "title_author_source": submission.title_author_source,
        "title_author_extraction_status": submission.title_author_extraction_status,
        "title_author_verification_image": submission.title_author_verification_image,
        "pdf_hash": submission.pdf_hash,
        "active_version": submission.active_version,
        "discarded": submission.discarded,
        "excluded_from_publication": submission.excluded_from_publication,
    }


def formatting_issue_evidence(submission):
    return {
        "pk": submission.pk,
        "paper_id_filled": submission.paper_id_filled,
        "final_submission_id": submission.final_submission_id,
        "active_version": submission.active_version,
        "discarded": submission.discarded,
        "excluded_from_publication": submission.excluded_from_publication,
        "page_count": submission.page_count,
        "pdf_hash": submission.pdf_hash,
        "format_status": submission.format_status,
        "format_notes": submission.format_notes,
        "source_hash": submission.source_hash,
    }


def exception_review_evidence(row):
    submission = row.get("submission")
    evidence = {
        "key": row.get("key"),
        "type": row.get("type"),
        "status": row.get("status"),
        "current_value": row.get("current_value"),
        "approved_value": row.get("approved_value"),
        "limit_label": row.get("limit_label"),
        "paper_ids": row.get("paper_ids"),
        "normalized_author_name": row.get("normalized_author_name"),
        "submission_pk": submission.pk if submission else None,
        "final_submission_id": (
            submission.final_submission_id if submission else ""
        ),
    }
    if row.get("type") == "author_number" and submission:
        from submissions.services.checks import normalize_author_name, split_authors

        evidence["normalized_author_list"] = [
            normalize_author_name(author)
            for author in split_authors(submission.extracted_authors)
        ]
    return evidence


def attach_exception_evidence_token(row):
    row["evidence_token"] = make_evidence_token(
        "exception-review",
        exception_review_evidence(row),
    )
    return row


def _evidence_digest(evidence):
    encoded = json.dumps(
        evidence,
        default=_json_default,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "name"):
        return value.name
    return str(value)
