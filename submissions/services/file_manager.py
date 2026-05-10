import re
import shutil
from pathlib import Path

from django.conf import settings as django_settings
from django.urls import reverse


def resolve_folder(path_value):
    path = Path(path_value)
    if not path.is_absolute():
        path = django_settings.BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename_part(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned or "UNTITLED"


def title_short_name(title, word_limit):
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    if not words:
        return "UNTITLED"
    return sanitize_filename_part("_".join(words[:word_limit]))


def source_pdf_path(submission):
    if submission.formatted_pdf_file and Path(submission.formatted_pdf_file.path).exists():
        return Path(submission.formatted_pdf_file.path)
    if submission.pdf_file and Path(submission.pdf_file.path).exists():
        return Path(submission.pdf_file.path)
    if submission.current_file_path and Path(submission.current_file_path).exists():
        return Path(submission.current_file_path)
    return None


def publication_pdf_info(submission):
    if submission.formatted_pdf_file and Path(submission.formatted_pdf_file.path).exists():
        path = Path(submission.formatted_pdf_file.path)
        return _publication_file_info(
            path=path,
            label="Corrected",
            source="corrected",
            url=reverse("submissions:publication_pdf", args=[submission.pk]),
        )
    if (
        submission.processing_status == "processed"
        and submission.current_file_path
        and Path(submission.current_file_path).exists()
    ):
        path = Path(submission.current_file_path)
        return _publication_file_info(
            path=path,
            label="Active-final",
            source="processed",
            url=reverse("submissions:publication_pdf", args=[submission.pk]),
        )
    if submission.pdf_file and Path(submission.pdf_file.path).exists():
        path = Path(submission.pdf_file.path)
        return _publication_file_info(
            path=path,
            label="Original",
            source="original",
            url=reverse("submissions:publication_pdf", args=[submission.pk]),
        )
    return _publication_file_info(path=None, label="No PDF", source="missing", url="")


def publication_source_info(submission):
    if submission.formatted_source_file and Path(submission.formatted_source_file.path).exists():
        return _publication_file_info(
            path=Path(submission.formatted_source_file.path),
            label="Corrected",
            source="corrected",
            url=getattr(submission.formatted_source_file, "url", ""),
        )
    if submission.source_current_file_path and Path(submission.source_current_file_path).exists():
        return _publication_file_info(
            path=Path(submission.source_current_file_path),
            label="Current",
            source="current",
            url="",
        )
    if submission.source_file and Path(submission.source_file.path).exists():
        return _publication_file_info(
            path=Path(submission.source_file.path),
            label="Original",
            source="original",
            url=getattr(submission.source_file, "url", ""),
        )
    return _publication_file_info(path=None, label="No source", source="missing", url="")


def corrected_pdf_needs_processing(submission):
    if not submission.formatted_pdf_file or not Path(submission.formatted_pdf_file.path).exists():
        return False
    if submission.processing_status != "processed":
        return True
    try:
        from submissions.services.pdf_processor import calculate_pdf_hash

        return calculate_pdf_hash(submission.formatted_pdf_file.path) != submission.pdf_hash
    except Exception:
        return True


def pdf_available_for_processing(submission):
    candidates = [
        getattr(submission.formatted_pdf_file, "path", "") if submission.formatted_pdf_file else "",
        submission.current_file_path,
        getattr(submission.pdf_file, "path", "") if submission.pdf_file else "",
    ]
    return any(candidate and Path(candidate).exists() for candidate in candidates)


def active_pdf_needs_processing(submission):
    if not pdf_available_for_processing(submission):
        return False
    return bool(
        submission.processing_status != "processed"
        or submission.page_count is None
        or not submission.pdf_hash
        or corrected_pdf_needs_processing(submission)
    )


def active_pdfs_needing_processing():
    from submissions.models import FinalSubmission

    return [
        submission
        for submission in FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
        )
        if active_pdf_needs_processing(submission)
    ]


def _publication_file_info(path, label, source, url):
    return {
        "path": str(path) if path else "",
        "label": label,
        "source": source,
        "url": url,
        "exists": bool(path and path.exists()),
    }


def copy_pdf_to_folder(submission, folder, filename):
    source = source_pdf_path(submission)
    if not source:
        return None
    target = folder / filename
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target
