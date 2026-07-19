from pathlib import Path
import hashlib

from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_master_paper(
    paper_id="P001",
    title="Ready Paper",
    authors="Ada Lovelace; Alan Turing",
    **overrides,
):
    values = {
        "paper_id": paper_id,
        "acceptance_status": "accepted",
        "title": title,
        "authors": authors,
    }
    values.update(overrides)
    return InitialPaper.objects.create(**values)


def create_final_submission(root, **overrides):
    paper_id = overrides.get("paper_id_filled", "P001")
    final_id = overrides.get("final_submission_id", "100")
    title = overrides.get("extracted_title", overrides.get("final_submission_title", "Ready Paper"))
    root = Path(root)
    files_dir = root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = files_dir / f"{final_id}.pdf"
    source_path = files_dir / f"{final_id}.docx"
    pdf_path.write_bytes(overrides.pop("pdf_bytes", f"PDF {final_id}".encode()))
    source_path.write_bytes(overrides.pop("source_bytes", f"SOURCE {final_id}".encode()))
    thumbnail_folder = root / "thumbnails" / str(final_id)
    thumbnail_folder.mkdir(parents=True, exist_ok=True)
    (thumbnail_folder / "page-1.png").write_bytes(b"thumbnail")
    values = {
        "final_submission_id": final_id,
        "start2_paper_id_raw": paper_id,
        "paper_id_filled": paper_id,
        "final_submission_title": title,
        "final_submission_authors": "Ada Lovelace; Alan Turing",
        "upload_date": timezone.now(),
        "current_file_path": str(pdf_path),
        "source_current_file_path": str(source_path),
        "extracted_title": title,
        "extracted_authors": "Ada Lovelace; Alan Turing",
        "page_count": 8,
        "processing_status": "processed",
        "processing_message": "Ready.",
        "pdf_hash": _file_hash(pdf_path),
        "thumbnail_folder": str(thumbnail_folder),
        "thumbnail_status": "processed",
        "active_version": True,
        "paper_id_verified": True,
        "verification_status": "verified",
        "title_author_verified": True,
        "extracted_title_verified": True,
        "format_status": "review_ok",
        "similarity_score": 1,
        "single_similarity_score": 1,
    }
    values.update(overrides)
    if "pdf_file" not in overrides:
        current_path_value = values.get("current_file_path") or ""
        current_path = Path(current_path_value) if current_path_value else None
        if current_path and current_path.exists():
            media_pdf = root / "media" / "final_submissions" / f"{final_id}_{current_path.name}"
            media_pdf.parent.mkdir(parents=True, exist_ok=True)
            media_pdf.write_bytes(current_path.read_bytes())
            values["pdf_file"] = f"final_submissions/{media_pdf.name}"
            values.setdefault("original_file_name", current_path.name)
            if "pdf_hash" not in overrides:
                values["pdf_hash"] = _file_hash(media_pdf)
    if "source_file" not in overrides:
        current_source_value = values.get("source_current_file_path") or ""
        current_source = Path(current_source_value) if current_source_value else None
        if current_source and current_source.exists():
            media_source = root / "media" / "source_submissions" / f"{final_id}_{current_source.name}"
            media_source.parent.mkdir(parents=True, exist_ok=True)
            media_source.write_bytes(current_source.read_bytes())
            values["source_file"] = f"source_submissions/{media_source.name}"
            values.setdefault("source_original_file_name", current_source.name)
            values.setdefault("source_hash", _file_hash(media_source))
    values.setdefault(
        "title_author_review_status",
        "review_ok" if values.get("title_author_verified") else "pending",
    )
    return FinalSubmission.objects.create(**values)
