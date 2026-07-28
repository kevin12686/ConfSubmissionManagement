"""Canonical user-facing labels shared across editorial worklists.

These labels are presentation vocabulary only. Database values, filter query
keys, readiness rules, and workflow transitions must not depend on this module.
"""

UI_LABELS = {
    "version": {
        "current_final": "Current final",
        "replaced": "Replaced",
        "discarded": "Discarded",
        "other_inactive": "Other inactive",
        "conflict": "Version conflict",
    },
    "origin": {
        "start2": "Start2",
        "editor_upload": "Editor Upload",
    },
    "paper_id": {
        "verified": "Verified",
        "auto_verified": "Auto-verified by title",
        "verified_title_differs": "Verified, title differs",
        "needs_review": "Paper ID needs review",
        "title_mismatch": "Paper ID title mismatch",
        "not_in_master": "Paper ID not in Master List",
    },
    "publication": {
        "publishing": "Publishing",
        "not_publishing": "Not Publishing",
        "decision_required": "Decision required",
        "integrity_conflict": "Integrity conflict",
        "excluded": "Excluded from publication",
    },
    "review": {
        "pending": "Pending",
        "red_flag": "Red Flag",
        "review_ok": "Review OK",
        "needs_edit": "Needs edit",
    },
    "processing": {
        "needs_processing": "Needs processing",
        "processed": "Processed",
        "error": "PDF error",
    },
    "exception": {
        "not_allowed": "Not allowed",
        "allowed": "Allowed exception",
        "stale": "Stale allowed exception",
    },
    "file": {
        "original": "Original",
        "corrected": "Corrected",
        "no_pdf": "No PDF",
        "no_source": "No source",
    },
}


def ui_label(group, key):
    return UI_LABELS[group][key]
