import importlib.util
import sys
import types
from pathlib import Path

from django.conf import settings as django_settings
from django.db.models import Q
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission
from submissions.services.file_manager import publication_pdf_info, sanitize_filename_part, source_pdf_path
from submissions.services.verification import text_diff_html, title_similarity, titles_identical


def _load_external_function(script_path):
    path = Path(script_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Title/author script not found: {path}")

    def load_module():
        spec = importlib.util.spec_from_file_location("utd_export_title_author", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    try:
        module = load_module()
    except ModuleNotFoundError as exc:
        if exc.name != "tqdm":
            raise
        shim = types.ModuleType("tqdm")
        shim.tqdm = lambda iterable, *args, **kwargs: iterable
        original = sys.modules.get("tqdm")
        sys.modules["tqdm"] = shim
        try:
            module = load_module()
        finally:
            if original is None:
                sys.modules.pop("tqdm", None)
            else:
                sys.modules["tqdm"] = original

    if not hasattr(module, "get_title_author"):
        raise AttributeError("Script does not expose get_title_author().")
    return module.get_title_author


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


def extract_title_author_for_submission(submission):
    setting = AppSetting.load()
    pdf_path = source_pdf_path(submission)
    if not pdf_path:
        submission.title_author_extraction_status = "error"
        submission.title_author_extraction_message = "Missing PDF file."
        submission.title_author_verified = False
        submission.title_author_verified_at = None
        submission.save(
            update_fields=[
                "title_author_extraction_status",
                "title_author_extraction_message",
                "title_author_verified",
                "title_author_verified_at",
                "updated_at",
            ]
        )
        return False

    target_dir = verification_root() / sanitize_filename_part(submission.final_submission_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        get_title_author = _load_external_function(setting.title_author_script_path)
        title, authors, author_count = get_title_author(
            str(pdf_path), verify=True, verify_folder=str(target_dir)
        )
        image_path = target_dir / f"{Path(pdf_path).name}.png"

        submission.extracted_title = title or ""
        submission.extracted_authors = authors or ""
        submission.title_author_source = "external_script"
        submission.title_author_imported_at = timezone.now()
        submission.title_author_extraction_status = "extracted"
        submission.title_author_extraction_message = (
            f"Extracted title, authors, and {author_count} author name(s)."
        )
        submission.title_author_verification_image = str(image_path) if image_path.exists() else ""
        submission.title_author_verified = False
        submission.title_author_verified_at = None
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
                "title_author_verified",
                "title_author_verified_at",
                "extracted_title_match_status",
                "extracted_title_match_score",
                "extracted_title_match_message",
                "extracted_title_verified",
                "extracted_title_auto_verify_blocked",
                "extracted_title_verified_at",
                "updated_at",
            ]
        )
        return True
    except Exception as exc:
        submission.title_author_extraction_status = "error"
        submission.title_author_extraction_message = f"Title/author extraction failed: {exc}"
        submission.title_author_verified = False
        submission.title_author_verified_at = None
        submission.save(
            update_fields=[
                "title_author_extraction_status",
                "title_author_extraction_message",
                "title_author_verified",
                "title_author_verified_at",
                "updated_at",
            ]
        )
        return False


def extract_active_title_authors():
    extracted = 0
    errors = 0
    for submission in FinalSubmission.objects.filter(active_version=True):
        if extract_title_author_for_submission(submission):
            extracted += 1
        else:
            errors += 1
    return {"extracted": extracted, "errors": errors}


def verify_title_author(submission):
    submission.title_author_verified = True
    submission.title_author_verified_at = timezone.now()
    submission.save(update_fields=["title_author_verified", "title_author_verified_at", "updated_at"])


def unverify_title_author(submission):
    submission.title_author_verified = False
    submission.title_author_verified_at = None
    submission.save(update_fields=["title_author_verified", "title_author_verified_at", "updated_at"])


def evaluate_extracted_title_match(submission, save=True):
    final_title = submission.final_submission_title or ""
    extracted_title = submission.extracted_title or ""
    score = title_similarity(final_title, extracted_title)

    if not final_title or not extracted_title:
        status = "missing"
        message = "Missing Final Submission Title or extracted title."
        submission.extracted_title_verified = False
        submission.extracted_title_verified_at = None
    elif titles_identical(final_title, extracted_title):
        if submission.extracted_title_auto_verify_blocked:
            status = "pending"
            message = "Titles are identical, but this record was manually moved back to unverified."
        else:
            status = "verified"
            message = "Extracted title is identical to Final Submission Title. Auto-verified."
            submission.extracted_title_verified = True
            submission.extracted_title_verified_at = submission.extracted_title_verified_at or timezone.now()
    elif score is not None and score >= 90:
        status = "verified" if submission.extracted_title_verified else "pending"
        message = f"Extracted title similarity with Final Submission Title: {score}%."
    else:
        status = "title_mismatch"
        message = f"Extracted title similarity with Final Submission Title: {score or 0}%."
        if not submission.extracted_title_verified:
            submission.extracted_title_verified_at = None

    submission.extracted_title_match_status = status
    submission.extracted_title_match_score = score
    submission.extracted_title_match_message = message

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
    is_verified = bool(submission.extracted_title_verified)
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


def title_author_extraction_rows(query="", status_filter="needs_verification"):
    submissions = FinalSubmission.objects.filter(active_version=True).order_by(
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
        title_match = evaluate_extracted_title_match(submission)
        has_extraction = bool(submission.extracted_title or submission.extracted_authors)
        needs_verification = has_extraction and (
            not submission.title_author_verified or title_match["needs_verification"]
        )
        missing_extraction = not has_extraction
        if status_filter == "needs_verification" and not needs_verification:
            continue
        if status_filter == "missing" and not missing_extraction:
            continue
        if status_filter == "verified" and not (
            submission.title_author_verified and submission.extracted_title_verified
        ):
            continue
        if status_filter == "errors" and submission.title_author_extraction_status != "error":
            continue
        if status_filter == "title_mismatch" and title_match["status"] != "title_mismatch":
            continue
        rows.append(
            {
                "submission": submission,
                "publication_pdf": publication_pdf_info(submission),
                "image_url": verification_image_url(submission),
                "has_extraction": has_extraction,
                "needs_verification": needs_verification,
                "missing_extraction": missing_extraction,
                "title_match": title_match,
            }
        )
    return rows
