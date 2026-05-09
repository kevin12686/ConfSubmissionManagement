from pathlib import Path

from django.utils import timezone

from submissions.models import FinalSubmission, InitialPaper


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
        "pdf_hash": f"hash-{final_id}",
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
    values.setdefault(
        "title_author_review_status",
        "review_ok" if values.get("title_author_verified") else "pending",
    )
    return FinalSubmission.objects.create(**values)
