import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings as django_settings
from django.urls import reverse

from submissions.services.file_inspection import FileInspectionContext


@dataclass(frozen=True)
class PublicationDebugPdfContext:
    folder: Path
    title_words_for_filename: int

    @classmethod
    def load(cls):
        from submissions.models import AppSetting

        return cls.from_settings(AppSetting.load())

    @classmethod
    def from_settings(cls, settings_obj):
        folder = Path(settings_obj.publication_pdf_debug_folder).expanduser()
        if not folder.is_absolute():
            folder = django_settings.BASE_DIR / folder
        return cls(
            folder=folder,
            title_words_for_filename=settings_obj.title_words_for_filename,
        )


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


def publication_title_filename(title, word_limit):
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    if not words:
        return "UNTITLED"
    cleaned = " ".join(words[:word_limit]).strip()
    return cleaned or "UNTITLED"


def publication_file_base_name(paper_id, title, word_limit):
    return f"{sanitize_filename_part(paper_id)}-{publication_title_filename(title, word_limit)}"


def publication_pdf_filename(paper_id, title, word_limit):
    return f"{publication_file_base_name(paper_id, title, word_limit)}.pdf"


def source_pdf_path(submission, inspection=None):
    inspection = inspection or FileInspectionContext()
    if submission.formatted_pdf_file:
        return (
            Path(submission.formatted_pdf_file.path)
            if inspection.exists(submission.formatted_pdf_file.path)
            else None
        )
    if submission.pdf_file and inspection.exists(submission.pdf_file.path):
        return Path(submission.pdf_file.path)
    return None


def publication_pdf_info(submission, inspection=None, *, include_url=True):
    inspection = inspection or FileInspectionContext()
    if submission.formatted_pdf_file:
        path = Path(submission.formatted_pdf_file.path)
        exists = inspection.exists(path)
        return _publication_file_info(
            path=path,
            label="Corrected" if exists else "Corrected missing",
            source="corrected" if exists else "corrected_missing",
            url=(
                reverse("submissions:publication_pdf", args=[submission.pk])
                if include_url and exists
                else ""
            ),
            exists=exists,
        )
    if submission.pdf_file and inspection.exists(submission.pdf_file.path):
        path = Path(submission.pdf_file.path)
        return _publication_file_info(
            path=path,
            label="Original",
            source="original",
            url=(
                reverse("submissions:publication_pdf", args=[submission.pk])
                if include_url
                else ""
            ),
            exists=True,
        )
    return _publication_file_info(
        path=None,
        label="No PDF",
        source="missing",
        url="",
        exists=False,
    )


def publication_source_info(submission, inspection=None, *, include_url=True):
    inspection = inspection or FileInspectionContext()
    if submission.formatted_source_file:
        path = Path(submission.formatted_source_file.path)
        exists = inspection.exists(path)
        return _publication_file_info(
            path=path,
            label="Corrected" if exists else "Corrected missing",
            source="corrected" if exists else "corrected_missing",
            url=(
                getattr(submission.formatted_source_file, "url", "")
                if include_url and exists
                else ""
            ),
            exists=exists,
        )
    if submission.source_file and inspection.exists(submission.source_file.path):
        return _publication_file_info(
            path=Path(submission.source_file.path),
            label="Original",
            source="original",
            url=getattr(submission.source_file, "url", "") if include_url else "",
            exists=True,
        )
    return _publication_file_info(
        path=None,
        label="No source",
        source="missing",
        url="",
        exists=False,
    )


def publication_debug_pdf_info(
    submission,
    paper=None,
    context=None,
    inspection=None,
    *,
    include_url=True,
):
    context = context or PublicationDebugPdfContext.load()
    inspection = inspection or FileInspectionContext()
    paper_id = paper.paper_id if paper else submission.paper_id_filled
    path = context.folder / publication_pdf_filename(
        paper_id,
        submission.extracted_title,
        context.title_words_for_filename,
    )
    exists = inspection.exists(path)
    return _publication_file_info(
        path=path,
        label="Debug copy",
        source="debug",
        url=(
            reverse("submissions:publication_debug_pdf", args=[submission.pk])
            if exists and include_url
            else ""
        ),
        exists=exists,
    )


def final_submission_display_pdf_info(submission, inspection=None):
    inspection = inspection or FileInspectionContext()
    if submission.formatted_pdf_file and inspection.exists(
        submission.formatted_pdf_file.path
    ):
        return _submission_file_info(
            path=Path(submission.formatted_pdf_file.path),
            label="Corrected",
            source="corrected",
            url=reverse("submissions:final_submission_display_pdf", args=[submission.pk]),
            exists=True,
        )
    if submission.pdf_file and inspection.exists(submission.pdf_file.path):
        return _submission_file_info(
            path=Path(submission.pdf_file.path),
            label="Original",
            source="original",
            url=reverse("submissions:final_submission_display_pdf", args=[submission.pk]),
            filename=submission.original_file_name,
            exists=True,
        )
    return _submission_file_info(
        path=None,
        label="No PDF",
        source="missing",
        url="",
        exists=False,
    )


def final_submission_display_source_info(submission, inspection=None):
    inspection = inspection or FileInspectionContext()
    if submission.formatted_source_file and inspection.exists(
        submission.formatted_source_file.path
    ):
        return _submission_file_info(
            path=Path(submission.formatted_source_file.path),
            label="Corrected",
            source="corrected",
            url=reverse("submissions:final_submission_display_source", args=[submission.pk]),
            exists=True,
        )
    if submission.source_file and inspection.exists(submission.source_file.path):
        return _submission_file_info(
            path=Path(submission.source_file.path),
            label="Original",
            source="original",
            url=reverse("submissions:final_submission_display_source", args=[submission.pk]),
            filename=submission.source_original_file_name,
            exists=True,
        )
    return _submission_file_info(
        path=None,
        label="No source",
        source="missing",
        url="",
        exists=False,
    )


def corrected_pdf_needs_processing(
    submission,
    inspection=None,
    *,
    source_path=None,
    fresh_hash=False,
):
    inspection = inspection or FileInspectionContext()
    path = source_path or source_pdf_path(submission, inspection)
    if not path:
        return False
    if submission.processing_status != "processed":
        return True
    try:
        return inspection.sha256(path, fresh=fresh_hash) != submission.pdf_hash
    except Exception:
        return True


def pdf_available_for_processing(submission, inspection=None):
    return bool(source_pdf_path(submission, inspection))


def active_pdf_needs_processing(
    submission,
    inspection=None,
    *,
    fresh_hash=False,
):
    inspection = inspection or FileInspectionContext()
    source_path = source_pdf_path(submission, inspection)
    if not source_path:
        return False
    return bool(
        submission.processing_status != "processed"
        or submission.page_count is None
        or not submission.pdf_hash
        or not (
            submission.thumbnail_folder
            and inspection.exists(submission.thumbnail_folder)
        )
        or corrected_pdf_needs_processing(
            submission,
            inspection,
            source_path=source_path,
            fresh_hash=fresh_hash,
        )
    )


def active_pdfs_needing_processing(inspection=None, *, submissions=None):
    from submissions.models import FinalSubmission, InitialPaper

    inspection = inspection or FileInspectionContext()
    if submissions is None:
        submissions = FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
            paper_id_filled__in=InitialPaper.objects.values("paper_id"),
        )
    return [
        submission
        for submission in submissions
        if active_pdf_needs_processing(submission, inspection)
    ]


def _publication_file_info(path, label, source, url, *, exists):
    return {
        "path": str(path) if path else "",
        "filename": path.name if path else "",
        "label": label,
        "source": source,
        "url": url,
        "exists": exists,
    }


def _submission_file_info(path, label, source, url, filename="", *, exists):
    return {
        **_publication_file_info(path, label, source, url, exists=exists),
        "filename": filename or (path.name if path else ""),
    }


def copy_pdf_to_folder(submission, folder, filename):
    source = source_pdf_path(submission)
    if not source:
        return None
    target = folder / filename
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target
