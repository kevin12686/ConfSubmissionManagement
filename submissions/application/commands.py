from dataclasses import dataclass

from submissions.services.import_preview import apply_import_preview
from submissions.services.pdf_processor import process_all_pdfs


@dataclass(frozen=True)
class ImportApplyResult:
    kind: str
    totals: dict

    @property
    def message(self):
        if self.kind == "paper_master":
            return (
                "Paper Master preview applied: "
                f"{self.totals['new']} new, "
                f"{self.totals['metadata_updated']} changed, "
                f"{self.totals['paper_id_review_reset']} Paper ID reviews reset."
            )
        return (
            "Final Submission preview applied: "
            f"{self.totals['new']} new, "
            f"{self.totals['metadata_updated']} changed, "
            f"{self.totals['pdf_reset']} PDF resets, "
            f"{self.totals['source_reset']} source resets, "
            f"{self.totals['corrected_files_archived']} corrected file sets archived."
        )


def apply_paper_master_preview(token, notes_policy="preserve_existing_notes"):
    return ImportApplyResult(
        "paper_master",
        apply_import_preview(token, notes_policy=notes_policy),
    )


def apply_final_submission_preview(token):
    return ImportApplyResult("final_submission", apply_import_preview(token))


def run_pdf_processing_action(action):
    if action == "process":
        return {"process_result": process_all_pdfs()}
    if action == "reprocess":
        return {"process_result": process_all_pdfs(force=True)}
    return {"process_result": None}
