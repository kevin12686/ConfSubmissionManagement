import re
from difflib import SequenceMatcher

from django.utils.html import conditional_escape, format_html
from django.utils.safestring import mark_safe
from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper
from submissions.services.audit import audit_success
from submissions.services.checks import resolve_official_paper_id
from submissions.services.file_manager import publication_pdf_info
from submissions.services.import_export import _mark_duplicate_submissions
from submissions.services.pdf_processor import determine_active_versions


def normalize_title(value):
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(left, right):
    left_normalized = normalize_title(left)
    right_normalized = normalize_title(right)
    if not left_normalized or not right_normalized:
        return None
    return round(SequenceMatcher(None, left_normalized, right_normalized).ratio() * 100, 2)


def titles_identical(left, right):
    left_normalized = normalize_title(left)
    right_normalized = normalize_title(right)
    return bool(left_normalized and right_normalized and left_normalized == right_normalized)


def _diff_html(initial_text, final_text, *, word_level=False):
    initial = initial_text or ""
    final = final_text or ""
    if word_level:
        initial_units = re.findall(r"\s+|\w+|[^\w\s]", initial, flags=re.UNICODE)
        final_units = re.findall(r"\s+|\w+|[^\w\s]", final, flags=re.UNICODE)
    else:
        initial_units = list(initial)
        final_units = list(final)
    matcher = SequenceMatcher(None, initial_units, final_units)
    parts = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        final_chunk = "".join(final_units[j1:j2])
        initial_chunk = "".join(initial_units[i1:i2])
        if tag == "equal":
            parts.append(conditional_escape(final_chunk))
        elif tag == "insert":
            parts.append(format_html('<mark class="diff-insert">{}</mark>', final_chunk))
        elif tag == "replace":
            parts.append(
                format_html(
                    '<mark class="diff-replace" title="{}">{}</mark>',
                    f'Replaces "{initial_chunk}"',
                    final_chunk,
                )
            )
        elif tag == "delete":
            parts.append(
                format_html(
                    '<mark class="diff-delete" title="{}">[-{}-]</mark>',
                    "Missing from uploaded PDF title",
                    initial_chunk,
                )
            )
    return mark_safe("".join(str(part) for part in parts))


def text_diff_html(initial_text, final_text):
    return _diff_html(initial_text, final_text)


def word_diff_html(initial_text, final_text):
    return _diff_html(initial_text, final_text, word_level=True)


def build_title_guard_context(
    *,
    extracted_title,
    references,
    extraction_status="extracted",
    extraction_message="",
):
    extracted_title = (extracted_title or "").strip()
    grouped_references = []
    grouped_by_title = {}
    for reference in references:
        title = (reference.get("title") or "").strip()
        label = reference.get("label") or "Reference Title"
        if title and title in grouped_by_title:
            grouped_by_title[title]["labels"].append(label)
            continue
        grouped = {"labels": [label], "title": title}
        grouped_references.append(grouped)
        if title:
            grouped_by_title[title] = grouped

    comparisons = []
    extraction_available = extraction_status == "extracted" and bool(extracted_title)
    for reference in grouped_references:
        reference_title = reference["title"]
        exact_match = bool(
            extraction_available
            and reference_title
            and reference_title == extracted_title
        )
        normalized_match = bool(
            extraction_available
            and reference_title
            and titles_identical(reference_title, extracted_title)
        )
        if not extraction_available:
            level = "danger"
            status_label = "PDF title unavailable"
        elif not reference_title:
            level = "danger"
            status_label = "Reference title missing"
        elif exact_match:
            level = "success"
            status_label = "Exact match"
        elif normalized_match:
            level = "info"
            status_label = "Formatting-only difference"
        else:
            level = "warning"
            status_label = "Content differs"
        comparisons.append(
            {
                "label": " = ".join(reference["labels"]),
                "title": reference_title,
                "level": level,
                "status_label": status_label,
                "exact_match": exact_match,
                "normalized_match": normalized_match,
                "score": title_similarity(reference_title, extracted_title)
                if reference_title and extracted_title
                else None,
                "word_diff_html": word_diff_html(reference_title, extracted_title)
                if reference_title and extracted_title and not exact_match
                else "",
                "character_diff_html": text_diff_html(reference_title, extracted_title)
                if reference_title and extracted_title and not exact_match
                else "",
            }
        )

    if not extraction_available:
        summary_level = "danger"
        summary_label = "PDF title could not be checked"
    elif any(item["level"] == "danger" for item in comparisons):
        summary_level = "danger"
        summary_label = "Title reference is incomplete"
    elif any(item["level"] == "warning" for item in comparisons):
        summary_level = "warning"
        summary_label = "Possible wrong paper"
    elif any(item["level"] == "info" for item in comparisons):
        summary_level = "info"
        summary_label = "Formatting-only title difference"
    else:
        summary_level = "success"
        summary_label = "Titles match"

    return {
        "extracted_title": extracted_title,
        "extraction_available": extraction_available,
        "extraction_message": extraction_message,
        "summary_level": summary_level,
        "summary_label": summary_label,
        "comparisons": comparisons,
    }


def title_diff_html(initial_title, final_title):
    return text_diff_html(initial_title, final_title)


def best_title_match(final_title, papers=None):
    scored = []
    for paper in papers if papers is not None else InitialPaper.objects.all():
        score = title_similarity(final_title, paper.title)
        if score is not None:
            scored.append((score, paper))
    if not scored:
        return None, None
    scored.sort(key=lambda item: item[0], reverse=True)
    score, paper = scored[0]
    return paper, score


def evaluate_submission(
    submission,
    save=False,
    initial_paper_by_id=None,
    paper_candidates=None,
):
    if submission.excluded_from_publication:
        status = "invalid_paper_id"
        score = None
        message = "Excluded from publication."
        initial = (
            initial_paper_by_id.get(submission.paper_id_filled)
            if initial_paper_by_id is not None
            else InitialPaper.objects.filter(paper_id=submission.paper_id_filled).first()
        )
        suggested_paper, suggested_score = None, None
        if save:
            submission.verification_status = status
            submission.title_match_score = score
            submission.verification_message = message
            submission.paper_id_verified = False
            submission.save(
                update_fields=[
                    "verification_status",
                    "title_match_score",
                    "verification_message",
                    "paper_id_verified",
                    "updated_at",
                ]
            )
        return {
            "submission": submission,
            "publication_pdf": publication_pdf_info(submission),
            "initial_paper": initial,
            "suggested_paper": suggested_paper,
            "suggested_score": suggested_score,
            "status": status,
            "score": score,
            "message": message,
            "is_identical": False,
            "is_verified": False,
            "verified_with_diff": False,
            "needs_verification": False,
            "final_title_diff_html": title_diff_html(
                initial.title if initial else "", submission.final_submission_title
            ),
            "final_authors_diff_html": text_diff_html(
                initial.authors if initial else "", submission.final_submission_authors
            ),
        }

    initial = (
        initial_paper_by_id.get(submission.paper_id_filled)
        if initial_paper_by_id is not None
        else InitialPaper.objects.filter(paper_id=submission.paper_id_filled).first()
    )
    suggested_paper, suggested_score = best_title_match(
        submission.final_submission_title, paper_candidates
    ) if not initial else (None, None)

    if not submission.paper_id_filled or not initial:
        status = "invalid_paper_id"
        score = suggested_score
        if suggested_paper:
            message = f"Paper ID not found. Best title match is {suggested_paper.paper_id} ({suggested_score}%)."
        else:
            message = "Paper ID not found and no title match is available."
    else:
        score = title_similarity(submission.final_submission_title, initial.title)
        if score is None:
            status = "verified" if submission.paper_id_verified else "pending"
            if submission.paper_id_verified:
                message = "Paper ID manually verified; title comparison is incomplete because a title is missing."
            else:
                message = "Missing final title or Paper Master title."
        elif titles_identical(submission.final_submission_title, initial.title):
            status = "verified"
            if submission.auto_verify_blocked:
                status = "pending"
                message = (
                    f"Final Title is identical to Paper Master title for {initial.paper_id}, "
                    "but this record was manually moved back to unverified."
                )
            else:
                message = f"Final Title is identical to Paper Master title for {initial.paper_id}. Auto-verified."
        elif score >= 80:
            status = "verified" if submission.paper_id_verified else "pending"
            message = f"Title similarity with {initial.paper_id}: {score}%."
        else:
            if submission.paper_id_verified:
                status = "verified"
                message = f"Paper ID manually verified; title differs from {initial.paper_id} ({score}%)."
            else:
                suggested_paper, suggested_score = best_title_match(
                    submission.final_submission_title, paper_candidates
                )
                status = "title_mismatch"
                if suggested_paper and suggested_paper.paper_id != initial.paper_id:
                    message = (
                        f"Title similarity with current Paper ID is {score}%. "
                        f"Best title match is {suggested_paper.paper_id} ({suggested_score}%)."
                    )
                else:
                    message = f"Title similarity with {initial.paper_id}: {score}%."

    if save:
        submission.verification_status = status
        submission.title_match_score = score
        submission.verification_message = message
        update_fields = [
            "verification_status",
            "title_match_score",
            "verification_message",
            "updated_at",
        ]
        if (
            status == "verified"
            and not submission.auto_verify_blocked
            and titles_identical(
            submission.final_submission_title, initial.title if initial else ""
            )
        ):
            submission.paper_id_verified = True
            submission.verified_at = submission.verified_at or timezone.now()
            update_fields.extend(["paper_id_verified", "verified_at"])
        submission.save(
            update_fields=update_fields
        )

    is_identical = bool(initial and titles_identical(submission.final_submission_title, initial.title))
    is_verified = bool(submission.paper_id_verified or (status == "verified" and is_identical))
    verified_with_diff = bool(is_verified and not is_identical)
    needs_verification = not is_verified

    return {
        "submission": submission,
        "publication_pdf": publication_pdf_info(submission),
        "initial_paper": initial,
        "suggested_paper": suggested_paper,
        "suggested_score": suggested_score,
        "status": status,
        "score": score,
        "message": message,
        "is_identical": is_identical,
        "is_verified": is_verified,
        "verified_with_diff": verified_with_diff,
        "needs_verification": needs_verification,
        "final_title_diff_html": title_diff_html(
            initial.title if initial else "", submission.final_submission_title
        ),
        "final_authors_diff_html": text_diff_html(
            initial.authors if initial else "", submission.final_submission_authors
        ),
    }


def verify_submission(submission, corrected_paper_id=None):
    if submission.excluded_from_publication:
        raise ValueError("Cannot verify: this final submission is marked Not Publishing.")
    if corrected_paper_id:
        submission.paper_id_filled = corrected_paper_id
    elif submission.start2_paper_id_raw and not submission.paper_id_filled:
        submission.paper_id_filled = resolve_official_paper_id(
            submission.start2_paper_id_raw,
            submission.final_submission_title,
        )

    if not InitialPaper.objects.filter(paper_id=submission.paper_id_filled).exists():
        raise ValueError("Cannot verify: ID not in Paper Master List.")

    submission.paper_id_verified = True
    submission.auto_verify_blocked = False
    submission.verified_at = timezone.now()
    submission.save(
        update_fields=[
            "paper_id_filled",
            "paper_id_verified",
            "auto_verify_blocked",
            "verified_at",
            "updated_at",
        ]
    )
    determine_active_versions()
    _mark_duplicate_submissions()
    result = evaluate_submission(submission, save=True)
    audit_success(
        "verify_paper_id",
        "Paper ID verified.",
        submission=submission,
        after={
            "paper_id_filled": submission.paper_id_filled,
            "verification_status": submission.verification_status,
            "title_match_score": submission.title_match_score,
        },
    )
    return result


def mark_not_publishing(submission, reason="unpaid", notes=""):
    submission.excluded_from_publication = True
    submission.publication_exclusion_reason = reason or "unpaid"
    submission.publication_exclusion_notes = notes or ""
    submission.publication_excluded_at = timezone.now()
    submission.paper_id_verified = False
    submission.auto_verify_blocked = True
    submission.verified_at = None
    submission.verification_status = "invalid_paper_id"
    submission.verification_message = "Marked Not Publishing; excluded from publication readiness checks."
    submission.save(
        update_fields=[
            "excluded_from_publication",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
            "publication_excluded_at",
            "paper_id_verified",
            "auto_verify_blocked",
            "verified_at",
            "verification_status",
            "verification_message",
            "updated_at",
        ]
    )
    determine_active_versions()
    _mark_duplicate_submissions()
    result = evaluate_submission(submission, save=True)
    audit_success(
        "mark_not_publishing",
        "Final submission marked Not Publishing.",
        submission=submission,
        after={
            "excluded_from_publication": True,
            "publication_exclusion_reason": submission.publication_exclusion_reason,
            "publication_exclusion_notes": submission.publication_exclusion_notes,
        },
    )
    return result


def undo_not_publishing(submission):
    submission.excluded_from_publication = False
    submission.publication_exclusion_reason = ""
    submission.publication_exclusion_notes = ""
    submission.publication_excluded_at = None
    submission.paper_id_verified = False
    submission.auto_verify_blocked = True
    submission.verified_at = None
    submission.verification_message = "Not Publishing was undone; Paper ID review required again."
    submission.save(
        update_fields=[
            "excluded_from_publication",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
            "publication_excluded_at",
            "paper_id_verified",
            "auto_verify_blocked",
            "verified_at",
            "verification_message",
            "updated_at",
        ]
    )
    determine_active_versions()
    _mark_duplicate_submissions()
    result = evaluate_submission(submission, save=True)
    audit_success(
        "undo_not_publishing",
        "Final submission moved back to publication review.",
        submission=submission,
        after={"excluded_from_publication": False},
        reset_flags={"paper_id_review": True},
    )
    return result


def unverify_submission(submission):
    submission.paper_id_verified = False
    submission.auto_verify_blocked = True
    submission.verified_at = None
    submission.save(
        update_fields=[
            "paper_id_verified",
            "auto_verify_blocked",
            "verified_at",
            "updated_at",
        ]
    )
    result = evaluate_submission(submission, save=True)
    audit_success(
        "unverify_paper_id",
        "Paper ID moved back to unverified.",
        submission=submission,
        reset_flags={"paper_id_review": True},
    )
    return result


def verification_rows(queryset=None):
    queryset = queryset or FinalSubmission.objects.all()
    paper_candidates = list(InitialPaper.objects.all())
    initial_paper_by_id = {paper.paper_id: paper for paper in paper_candidates}
    rows = []
    for submission in queryset.select_related():
        result = evaluate_submission(
            submission,
            save=False,
            initial_paper_by_id=initial_paper_by_id,
            paper_candidates=paper_candidates,
        )
        rows.append(result)
    return sorted(rows, key=verification_sort_key)


def verification_sort_key(row):
    submission = row["submission"]
    if row["needs_verification"]:
        group = 0
    elif row["verified_with_diff"]:
        group = 1
    elif row["is_identical"]:
        group = 2
    else:
        group = 3
    return (group, _sortable_final_id(submission.final_submission_id), submission.created_at)


def _sortable_final_id(value):
    text = str(value or "")
    return (0, int(text)) if text.isdigit() else (1, text)
