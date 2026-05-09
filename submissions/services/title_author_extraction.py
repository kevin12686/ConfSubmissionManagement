from pathlib import Path

from django.conf import settings as django_settings
from django.db.models import Q
from django.utils import timezone

from submissions.models import FinalSubmission
from submissions.services.checks import reset_author_number_exception
from submissions.services.builtin_title_author_extractor import get_title_author
from submissions.services.file_manager import publication_pdf_info, sanitize_filename_part, source_pdf_path
from submissions.services.verification import text_diff_html, title_similarity, titles_identical

TITLE_AUTHOR_REVIEW_STATUSES = {"pending", "red_flag", "review_ok"}


def verification_root():
    path = django_settings.MEDIA_ROOT / "title_author_verification"
    path.mkdir(parents=True, exist_ok=True)
    return path


def verification_image_url(submission):
    if not submission.title_author_verification_image:
        return ""
    image_path = Path(submission.title_author_verification_image)
    if not image_path.exists():
        return ""
    try:
        relative_path = image_path.relative_to(django_settings.MEDIA_ROOT)
    except ValueError:
        return ""
    return f"{django_settings.MEDIA_URL}{relative_path.as_posix()}"


def extract_title_author_for_submission(submission, refresh_author_cache=True):
    pdf_path = source_pdf_path(submission)
    if not pdf_path:
        submission.title_author_extraction_status = "error"
        submission.title_author_extraction_message = "Missing PDF file."
        submission.title_author_review_status = "pending"
        submission.title_author_verified = False
        submission.title_author_verified_at = None
        submission.duplicate_author_review_status = "pending"
        submission.duplicate_author_review_notes = ""
        submission.duplicate_author_reviewed_at = None
        submission.save(
            update_fields=[
                "title_author_extraction_status",
                "title_author_extraction_message",
                "title_author_review_status",
                "title_author_verified",
                "title_author_verified_at",
                "duplicate_author_review_status",
                "duplicate_author_review_notes",
                "duplicate_author_reviewed_at",
                "updated_at",
            ]
        )
        return False

    target_dir = verification_root() / sanitize_filename_part(submission.final_submission_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        title, authors, author_count = get_title_author(
            str(pdf_path), verify=True, verify_folder=str(target_dir)
        )
        image_path = target_dir / f"{Path(pdf_path).name}.png"

        submission.extracted_title = title or ""
        submission.extracted_authors = authors or ""
        submission.title_author_source = "built_in_extractor"
        submission.title_author_imported_at = timezone.now()
        submission.title_author_extraction_status = "extracted"
        submission.title_author_extraction_message = (
            f"Extracted title, authors, and {author_count} author name(s)."
        )
        submission.title_author_verification_image = str(image_path) if image_path.exists() else ""
        submission.title_author_review_status = "pending"
        submission.title_author_verified = False
        submission.title_author_verified_at = None
        submission.duplicate_author_review_status = "pending"
        submission.duplicate_author_review_notes = ""
        submission.duplicate_author_reviewed_at = None
        reset_author_number_exception(submission)
        submission.extracted_title_verified = False
        submission.extracted_title_verified_at = None
        submission.extracted_title_auto_verify_blocked = False
        evaluate_extracted_title_match(submission, save=False)
        submission.save(
            update_fields=[
                "extracted_title",
                "extracted_authors",
                "title_author_source",
                "title_author_imported_at",
                "title_author_extraction_status",
                "title_author_extraction_message",
                "title_author_verification_image",
                "title_author_review_status",
                "title_author_verified",
                "title_author_verified_at",
                "duplicate_author_review_status",
                "duplicate_author_review_notes",
                "duplicate_author_reviewed_at",
                "author_number_exception_approved",
                "author_number_exception_reason",
                "author_number_exception_author_count",
                "author_number_exception_approved_at",
                "extracted_title_match_status",
                "extracted_title_match_score",
                "extracted_title_match_message",
                "extracted_title_verified",
                "extracted_title_auto_verify_blocked",
                "extracted_title_verified_at",
                "updated_at",
            ]
        )
        if refresh_author_cache:
            from submissions.services.checks import rebuild_paper_authors

            rebuild_paper_authors()
        return True
    except Exception as exc:
        submission.title_author_extraction_status = "error"
        submission.title_author_extraction_message = f"Title/author extraction failed: {exc}"
        submission.title_author_review_status = "pending"
        submission.title_author_verified = False
        submission.title_author_verified_at = None
        submission.duplicate_author_review_status = "pending"
        submission.duplicate_author_review_notes = ""
        submission.duplicate_author_reviewed_at = None
        submission.save(
            update_fields=[
                "title_author_extraction_status",
                "title_author_extraction_message",
                "title_author_review_status",
                "title_author_verified",
                "title_author_verified_at",
                "duplicate_author_review_status",
                "duplicate_author_review_notes",
                "duplicate_author_reviewed_at",
                "updated_at",
            ]
        )
        return False


def _needs_title_author_extraction_review(submission):
    title_match = evaluate_extracted_title_match(submission)
    has_extraction = bool(submission.extracted_title or submission.extracted_authors)
    review_status = submission.title_author_review_status
    return bool(
        not has_extraction
        or review_status in {"pending", "red_flag"}
        or title_match["needs_verification"]
        or submission.title_author_extraction_status == "error"
        or title_match["status"] == "title_mismatch"
    )


def extraction_overwrite_summary():
    active = FinalSubmission.objects.filter(active_version=True, discarded=False)
    return {
        "active_count": active.count(),
        "with_extraction": active.filter(
            Q(extracted_title__gt="") | Q(extracted_authors__gt="")
        ).count(),
        "title_author_reviewed": active.filter(title_author_review_status="review_ok").count(),
        "title_matched": active.filter(extracted_title_verified=True).count(),
    }


def extract_active_title_authors(mode="needs_review"):
    extracted = 0
    errors = 0
    skipped = 0
    submissions = FinalSubmission.objects.filter(active_version=True, discarded=False)
    for submission in submissions:
        if mode != "all" and not _needs_title_author_extraction_review(submission):
            skipped += 1
            continue
        if extract_title_author_for_submission(submission, refresh_author_cache=False):
            extracted += 1
        else:
            errors += 1
    if extracted:
        from submissions.services.checks import rebuild_paper_authors

        rebuild_paper_authors()
    return {"extracted": extracted, "errors": errors, "skipped": skipped, "mode": mode}


def verify_title_author(submission):
    set_title_author_review_status(submission, "review_ok")


def unverify_title_author(submission):
    set_title_author_review_status(submission, "pending")


def set_title_author_review_status(submission, status):
    if status not in TITLE_AUTHOR_REVIEW_STATUSES:
        raise ValueError(f"Unsupported title/author review status: {status}")
    submission.title_author_review_status = status
    if status == "review_ok":
        submission.title_author_verified = True
        submission.title_author_verified_at = timezone.now()
    else:
        submission.title_author_verified = False
        submission.title_author_verified_at = None
    submission.save(
        update_fields=[
            "title_author_review_status",
            "title_author_verified",
            "title_author_verified_at",
            "updated_at",
        ]
    )


def evaluate_extracted_title_match(submission, save=True, apply=True):
    final_title = submission.final_submission_title or ""
    extracted_title = submission.extracted_title or ""
    score = title_similarity(final_title, extracted_title)
    extracted_title_verified = submission.extracted_title_verified
    extracted_title_verified_at = submission.extracted_title_verified_at

    if not final_title or not extracted_title:
        status = "missing"
        message = "Missing Final Submission Title or extracted title."
        extracted_title_verified = False
        extracted_title_verified_at = None
    elif titles_identical(final_title, extracted_title):
        if submission.extracted_title_auto_verify_blocked:
            status = "pending"
            message = "Titles are identical, but this record was manually moved back to unverified."
        else:
            status = "verified"
            message = "Extracted title is identical to Final Submission Title. Auto-verified."
            extracted_title_verified = True
            extracted_title_verified_at = extracted_title_verified_at or timezone.now()
    elif score is not None and score >= 90:
        status = "verified" if extracted_title_verified else "pending"
        message = f"Extracted title similarity with Final Submission Title: {score}%."
    else:
        status = "title_mismatch"
        message = f"Extracted title similarity with Final Submission Title: {score or 0}%."
        if not extracted_title_verified:
            extracted_title_verified_at = None

    if apply or save:
        submission.extracted_title_match_status = status
        submission.extracted_title_match_score = score
        submission.extracted_title_match_message = message
        submission.extracted_title_verified = extracted_title_verified
        submission.extracted_title_verified_at = extracted_title_verified_at

    if save:
        submission.save(
            update_fields=[
                "extracted_title_match_status",
                "extracted_title_match_score",
                "extracted_title_match_message",
                "extracted_title_verified",
                "extracted_title_verified_at",
                "updated_at",
            ]
        )

    is_identical = titles_identical(final_title, extracted_title)
    is_verified = bool(extracted_title_verified)
    return {
        "status": status,
        "score": score,
        "message": message,
        "is_identical": is_identical,
        "is_verified": is_verified,
        "verified_with_diff": bool(is_verified and not is_identical),
        "needs_verification": bool(extracted_title and final_title and not is_verified),
        "diff_html": text_diff_html(final_title, extracted_title),
    }


def verify_extracted_title(submission):
    submission.extracted_title_verified = True
    submission.extracted_title_auto_verify_blocked = False
    submission.extracted_title_verified_at = timezone.now()
    evaluate_extracted_title_match(submission, save=False)
    submission.save(
        update_fields=[
            "extracted_title_verified",
            "extracted_title_auto_verify_blocked",
            "extracted_title_verified_at",
            "extracted_title_match_status",
            "extracted_title_match_score",
            "extracted_title_match_message",
            "updated_at",
        ]
    )


def unverify_extracted_title(submission):
    submission.extracted_title_verified = False
    submission.extracted_title_auto_verify_blocked = True
    submission.extracted_title_verified_at = None
    evaluate_extracted_title_match(submission, save=False)
    submission.save(
        update_fields=[
            "extracted_title_verified",
            "extracted_title_auto_verify_blocked",
            "extracted_title_verified_at",
            "extracted_title_match_status",
            "extracted_title_match_score",
            "extracted_title_match_message",
            "updated_at",
        ]
    )


def title_author_extraction_rows(query="", status_filter="pending"):
    submissions = FinalSubmission.objects.filter(active_version=True, discarded=False).order_by(
        "paper_id_filled", "final_submission_id"
    )
    if query:
        submissions = submissions.filter(
            Q(final_submission_id__icontains=query)
            | Q(paper_id_filled__icontains=query)
            | Q(final_submission_title__icontains=query)
            | Q(final_submission_authors__icontains=query)
            | Q(extracted_title__icontains=query)
            | Q(extracted_authors__icontains=query)
        )
        submissions = submissions.distinct()

    rows = []
    for submission in submissions:
        title_match = evaluate_extracted_title_match(submission, save=False, apply=False)
        has_extraction = bool(submission.extracted_title or submission.extracted_authors)
        review_status = submission.title_author_review_status
        needs_verification = has_extraction and (
            review_status in {"pending", "red_flag"} or title_match["needs_verification"]
        )
        missing_extraction = not has_extraction
        needs_attention = bool(
            missing_extraction
            or needs_verification
            or submission.title_author_extraction_status == "error"
            or title_match["status"] == "title_mismatch"
        )
        row = (
            {
                "submission": submission,
                "publication_pdf": publication_pdf_info(submission),
                "image_url": verification_image_url(submission),
                "has_extraction": has_extraction,
                "needs_verification": needs_verification,
                "needs_attention": needs_attention,
                "missing_extraction": missing_extraction,
                "title_match": title_match,
            }
        )
        if _title_author_row_matches(row, status_filter):
            rows.append(row)
    return rows


def _title_author_row_matches(row, status_filter):
    submission = row["submission"]
    review_status = submission.title_author_review_status
    title_match = row["title_match"]
    if status_filter == "needs_verification":
        return row["needs_attention"]
    if status_filter == "pending":
        return review_status == "pending"
    if status_filter == "red_flag":
        return review_status == "red_flag"
    if status_filter == "review_ok":
        return review_status == "review_ok"
    if status_filter == "missing":
        return row["missing_extraction"]
    if status_filter == "verified":
        return review_status == "review_ok"
    if status_filter == "errors":
        return submission.title_author_extraction_status == "error"
    if status_filter == "title_mismatch":
        return title_match["status"] == "title_mismatch"
    return True


def filter_title_author_extraction_rows(rows, status_filter):
    return [row for row in rows if _title_author_row_matches(row, status_filter)]
