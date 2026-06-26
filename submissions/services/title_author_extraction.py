from pathlib import Path

from django.conf import settings as django_settings
from django.db.models import Q
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.audit import audit_failure, audit_success
from submissions.services.checks import reset_author_number_exception, split_authors
from submissions.services.builtin_title_author_extractor import get_title_author
from submissions.services.file_manager import publication_pdf_info, sanitize_filename_part, source_pdf_path
from submissions.services.grobid_extractor import (
    GrobidExtractionError,
    check_grobid_api,
    extract_header_with_grobid,
    is_grobid_service_unavailable_error,
)
from submissions.services.verification import text_diff_html, title_similarity, titles_identical

TITLE_AUTHOR_REVIEW_STATUSES = {"pending", "red_flag", "review_ok"}


class ManualOverrideError(ValueError):
    pass


def grobid_availability_status(settings_obj=None):
    settings_obj = settings_obj or AppSetting.load()
    if not settings_obj.grobid_enabled:
        return {
            "available": False,
            "level": "secondary",
            "label": "Disabled",
            "message": "GROBID fallback is disabled in Settings.",
        }
    return check_grobid_api(
        settings_obj.grobid_api_url,
        min(settings_obj.grobid_timeout_seconds or 2, 2),
    )


def grobid_unavailable_message(status):
    message = (status or {}).get("message") or "Check Settings before running GROBID extraction."
    if (status or {}).get("label") == "Disabled":
        return message
    return f"GROBID API is unavailable. {message}"


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
    version = image_path.stat().st_mtime_ns
    return f"{django_settings.MEDIA_URL}{relative_path.as_posix()}?v={version}"


def generate_text_verification_image(pdf_path, extracted_title, extracted_authors, source_label, target_dir):
    import fitz

    pdf_path = Path(pdf_path)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    source_slug = sanitize_filename_part(source_label).lower() or "extraction"
    output_path = target_dir / f"{pdf_path.stem}-{source_slug}.png"
    missing_authors = []

    with fitz.open(pdf_path) as document:
        if not document.page_count:
            raise ValueError("PDF has no pages.")
        page = document[0]
        page_rect = page.rect
        clip = fitz.Rect(
            page_rect.x0,
            page_rect.y0,
            page_rect.x1,
            page_rect.y0 + (page_rect.height / 3),
        )

        if extracted_title:
            _highlight_matches(page, extracted_title, clip, color=(1, 1, 0))

        for author in split_authors(extracted_authors):
            if not _highlight_matches(page, author, clip, color=(0, 1, 0)):
                missing_authors.append(author)

        _add_extraction_overlay(page, pdf_path.name, extracted_title, extracted_authors, source_label, clip)
        page.set_cropbox(clip)
        pixmap = page.get_pixmap(dpi=300, alpha=False)
        pixmap.save(output_path)
    return output_path, missing_authors


def _highlight_matches(page, text, clip, color):
    matches = page.search_for(text, clip=clip)
    for match in matches:
        annotation = page.add_highlight_annot(match)
        annotation.set_colors(stroke=color)
        annotation.update()
    return bool(matches)


def _add_extraction_overlay(page, filename, title, authors, source_label, clip):
    import fitz

    anchor = _extraction_overlay_anchor(page, title, clip)
    title_rect = anchor
    author_rect = anchor + fitz.Rect(0, 24, 0, 28)
    page.add_freetext_annot(
        title_rect,
        title or "Missing extracted title",
        fontsize=9,
        text_color=(0, 0, 1),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.add_freetext_annot(
        author_rect,
        authors or "Missing extracted authors",
        fontsize=9,
        text_color=(1, 0, 0),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    page.add_freetext_annot(
        fitz.Rect(15, 30, 100, 45),
        filename,
        fontsize=7,
        text_color=(0, 0, 0),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    source_rect = fitz.Rect(15, 8, 100, 28)
    source_annot = page.add_freetext_annot(
        source_rect,
        source_label.upper(),
        fontsize=9,
        text_color=(1, 0, 0),
        align=fitz.TEXT_ALIGN_CENTER,
    )
    source_annot.set_border(width=0.75)
    source_annot.update()


def _extraction_overlay_anchor(page, title, clip):
    import fitz

    title_matches = page.search_for(title, clip=clip, quads=True) if title else []
    if title_matches and title_matches[0].rect.x1 - title_matches[0].rect.x0 > 300:
        rect = title_matches[0].rect + fitz.Rect(0, -42, 0, -12)
        if rect.x0 < 70:
            rect.x0 = 70
        if rect.y0 < 12:
            rect = fitz.Rect(70, 20, 570, 48)
        return rect
    return fitz.Rect(70, 20, 570, 48)


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
        submission.title_author_manual_override_reason = ""
        submission.title_author_manual_override_at = None
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
                "title_author_manual_override_reason",
                "title_author_manual_override_at",
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
        audit_success(
            "title_author_extract",
            "Title/author extraction completed.",
            submission=submission,
            reset_flags={
                "title_author_review": True,
                "extracted_title_match": True,
                "duplicate_author_review": True,
                "author_number_exception": True,
            },
            after={
                "extraction_status": submission.title_author_extraction_status,
                "extracted_title": submission.extracted_title,
                "extracted_authors": submission.extracted_authors,
            },
            file_changes={"verification_image": submission.title_author_verification_image},
        )
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
        audit_failure(
            "title_author_extract",
            exc,
            "Title/author extraction failed.",
            submission=submission,
            after={"extraction_status": "error", "message": submission.title_author_extraction_message},
        )
        return False


def extract_title_author_with_grobid(submission, refresh_author_cache=True, skip_health_check=False):
    submission._last_grobid_service_unavailable = False
    settings_obj = AppSetting.load()
    if not skip_health_check:
        health_status = grobid_availability_status(settings_obj)
        if not health_status["available"]:
            error = GrobidExtractionError(grobid_unavailable_message(health_status))
            submission._last_grobid_error = str(error)
            submission._last_grobid_service_unavailable = True
            audit_failure(
                "grobid_title_author_extract",
                error,
                "GROBID title/author extraction skipped because the API is unavailable.",
                submission=submission,
                result_counts={"processed": 0},
                extra={"grobid_health": health_status},
            )
            return False

    if not settings_obj.grobid_enabled:
        error = GrobidExtractionError("GROBID fallback is disabled in Settings.")
        submission._last_grobid_error = str(error)
        submission._last_grobid_service_unavailable = False
        audit_failure(
            "grobid_title_author_extract",
            error,
            "GROBID title/author extraction skipped.",
            submission=submission,
        )
        return False

    pdf_path = source_pdf_path(submission)
    if not pdf_path:
        error = GrobidExtractionError("Missing PDF file.")
        submission._last_grobid_error = str(error)
        submission._last_grobid_service_unavailable = False
        audit_failure(
            "grobid_title_author_extract",
            error,
            "GROBID title/author extraction failed.",
            submission=submission,
        )
        return False

    target_dir = verification_root() / sanitize_filename_part(submission.final_submission_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = extract_header_with_grobid(
            pdf_path,
            settings_obj.grobid_api_url,
            settings_obj.grobid_timeout_seconds,
        )
        image_path, missing_authors = generate_text_verification_image(
            pdf_path,
            result.title,
            result.authors,
            "GROBID",
            target_dir,
        )
        message = f"Extracted by GROBID: title, authors, and {result.author_count} author name(s)."
        if missing_authors:
            message += " Some extracted authors could not be highlighted on first page."

        submission.extracted_title = result.title or ""
        submission.extracted_authors = result.authors or ""
        submission.title_author_source = "grobid"
        submission.title_author_imported_at = timezone.now()
        submission.title_author_extraction_status = "extracted"
        submission.title_author_extraction_message = message
        submission.title_author_verification_image = str(image_path) if image_path.exists() else ""
        submission.title_author_manual_override_reason = ""
        submission.title_author_manual_override_at = None
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
                "title_author_manual_override_reason",
                "title_author_manual_override_at",
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
        audit_success(
            "grobid_title_author_extract",
            "GROBID title/author extraction completed.",
            submission=submission,
            reset_flags={
                "title_author_review": True,
                "extracted_title_match": True,
                "duplicate_author_review": True,
                "author_number_exception": True,
            },
            after={
                "extraction_status": submission.title_author_extraction_status,
                "extracted_title": submission.extracted_title,
                "extracted_authors": submission.extracted_authors,
                "missing_highlighted_authors": missing_authors,
            },
            file_changes={"verification_image": submission.title_author_verification_image},
        )
        return True
    except Exception as exc:
        submission._last_grobid_error = str(exc)
        submission._last_grobid_service_unavailable = is_grobid_service_unavailable_error(exc)
        audit_failure(
            "grobid_title_author_extract",
            exc,
            "GROBID title/author extraction failed without changing existing extraction.",
            submission=submission,
            extra={"service_unavailable": submission._last_grobid_service_unavailable},
        )
        return False


def apply_title_author_manual_override(submission, title, authors, reason, refresh_author_cache=True):
    title = (title or "").strip()
    authors = (authors or "").strip()
    reason = (reason or "").strip()
    if not reason:
        raise ManualOverrideError("Manual override reason is required.")
    if not title and not authors:
        raise ManualOverrideError("Manual override requires an extracted title or extracted authors.")

    before = {
        "extracted_title": submission.extracted_title,
        "extracted_authors": submission.extracted_authors,
        "title_author_source": submission.title_author_source,
        "title_author_review_status": submission.title_author_review_status,
        "extracted_title_verified": submission.extracted_title_verified,
    }
    pdf_path = source_pdf_path(submission)
    image_path = ""
    image_message = ""
    if pdf_path:
        try:
            generated_image, missing_authors = generate_text_verification_image(
                pdf_path,
                title,
                authors,
                "MANUAL OVERRIDE",
                verification_root() / sanitize_filename_part(submission.final_submission_id),
            )
            image_path = str(generated_image) if generated_image.exists() else ""
            if missing_authors:
                image_message = " Some manually entered authors could not be highlighted on first page."
        except Exception as exc:
            image_message = f" Verification image generation failed: {exc}"

    submission.extracted_title = title
    submission.extracted_authors = authors
    submission.title_author_source = "manual_override"
    submission.title_author_imported_at = timezone.now()
    submission.title_author_extraction_status = "extracted"
    submission.title_author_extraction_message = (
        "Title/authors manually overridden by editor." + image_message
    )
    submission.title_author_manual_override_reason = reason
    submission.title_author_manual_override_at = timezone.now()
    submission.title_author_verification_image = image_path
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
            "title_author_manual_override_reason",
            "title_author_manual_override_at",
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
    audit_success(
        "title_author_manual_override",
        "Title/author extracted metadata manually overridden.",
        submission=submission,
        changed_fields=[
            "extracted_title",
            "extracted_authors",
            "title_author_source",
            "title_author_manual_override_reason",
        ],
        before=before,
        after={
            "extracted_title": submission.extracted_title,
            "extracted_authors": submission.extracted_authors,
            "title_author_source": submission.title_author_source,
            "title_author_review_status": submission.title_author_review_status,
            "extracted_title_verified": submission.extracted_title_verified,
            "reason": reason,
        },
        reset_flags={
            "title_author_review": True,
            "duplicate_author_review": True,
            "author_number_exception": True,
        },
        file_changes={"verification_image": submission.title_author_verification_image},
    )
    return submission


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


def is_grobid_suspicious(submission):
    return bool(
        submission.title_author_extraction_status == "error"
        or submission.title_author_review_status == "red_flag"
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


def extract_grobid_for_suspicious_rows():
    extracted = 0
    errors = 0
    submissions = list(FinalSubmission.objects.filter(active_version=True, discarded=False).order_by("pk"))
    candidates = [submission for submission in submissions if is_grobid_suspicious(submission)]
    skipped = len(submissions) - len(candidates)
    health_status = grobid_availability_status()
    if not health_status["available"]:
        result = {
            "extracted": 0,
            "errors": 0,
            "skipped": len(candidates),
            "mode": "grobid_suspicious",
            "aborted": True,
            "message": grobid_unavailable_message(health_status),
        }
        audit_failure(
            "grobid_title_author_extract_batch",
            GrobidExtractionError(result["message"]),
            "GROBID suspicious-row extraction was not started because the API is unavailable.",
            result_counts=result,
            extra={"grobid_health": health_status, "candidate_count": len(candidates)},
        )
        return result

    stopped = False
    stop_message = ""
    for index, submission in enumerate(candidates):
        if extract_title_author_with_grobid(
            submission,
            refresh_author_cache=False,
            skip_health_check=True,
        ):
            extracted += 1
        elif getattr(submission, "_last_grobid_service_unavailable", False):
            stopped = True
            stop_message = (
                getattr(submission, "_last_grobid_error", "")
                or "GROBID became unavailable during batch extraction."
            )
            skipped += len(candidates) - index
            break
        else:
            errors += 1
    if extracted:
        from submissions.services.checks import rebuild_paper_authors

        rebuild_paper_authors()
    result = {
        "extracted": extracted,
        "errors": errors,
        "skipped": skipped,
        "mode": "grobid_suspicious",
        "stopped": stopped,
        "message": stop_message,
    }
    if stopped:
        audit_failure(
            "grobid_title_author_extract_batch",
            GrobidExtractionError(stop_message),
            "GROBID suspicious-row extraction stopped because the API became unavailable.",
            result_counts=result,
        )
    else:
        audit_success(
            "grobid_title_author_extract_batch",
            "GROBID extraction completed for suspicious rows.",
            result_counts=result,
        )
    return result


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
    audit_success(
        "title_author_review_status",
        f"Title/author review status changed to {status}.",
        submission=submission,
        after={"title_author_review_status": status},
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
    audit_success(
        "verify_extracted_title_match",
        "Extracted title match verified.",
        submission=submission,
        after={
            "extracted_title_match_status": submission.extracted_title_match_status,
            "extracted_title_match_score": submission.extracted_title_match_score,
        },
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
    audit_success(
        "unverify_extracted_title_match",
        "Extracted title match moved back to unverified.",
        submission=submission,
        reset_flags={"extracted_title_match": True},
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
                "grobid_suspicious": is_grobid_suspicious(submission),
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
    if status_filter == "title_match":
        return bool(submission.extracted_title_verified)
    if status_filter == "title_needs_match":
        return bool(
            submission.final_submission_title
            and submission.extracted_title
            and not submission.extracted_title_verified
        )
    if status_filter == "manual_override":
        return submission.title_author_source == "manual_override"
    if status_filter == "title_mismatch":
        return title_match["status"] == "title_mismatch"
    return True


def filter_title_author_extraction_rows(rows, status_filter):
    return [row for row in rows if _title_author_row_matches(row, status_filter)]
