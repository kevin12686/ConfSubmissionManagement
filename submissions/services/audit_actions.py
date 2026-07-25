from dataclasses import dataclass


@dataclass(frozen=True)
class AuditActionDefinition:
    code: str
    label: str
    category: str
    aliases: tuple[str, ...] = ()


AUDIT_CATEGORY_LABELS = {
    "paper_data": "Paper Data & Imports",
    "submissions": "Submissions & Versions",
    "reviews": "Editorial Review",
    "formatting": "Formatting",
    "processing": "PDF Processing",
    "plagiarism": "CrossCheck & Plagiarism",
    "exceptions": "Exceptions",
    "exports": "Reports & Exports",
    "administration": "Settings & Storage",
    "system_state": "System & Backup",
    "other": "Other",
}


_ACTION_DEFINITIONS = (
    # Paper data and imports.
    AuditActionDefinition("paper_master_save", "Save Paper Master", "paper_data"),
    AuditActionDefinition("paper_master_delete", "Delete Paper Master", "paper_data"),
    AuditActionDefinition(
        "paper_master_import_preview",
        "Preview Paper Master Import",
        "paper_data",
        ("initial_import_preview",),
    ),
    AuditActionDefinition(
        "paper_master_import_apply",
        "Apply Paper Master Import",
        "paper_data",
        ("initial_import_apply",),
    ),
    AuditActionDefinition(
        "final_submission_import_preview",
        "Preview Final Submission Import",
        "paper_data",
        ("final_import_preview",),
    ),
    AuditActionDefinition(
        "final_submission_import_apply",
        "Apply Final Submission Import",
        "paper_data",
        ("final_import_apply",),
    ),
    AuditActionDefinition(
        "import_apply",
        "Apply Import",
        "paper_data",
    ),
    AuditActionDefinition(
        "import_preview",
        "Preview Import",
        "paper_data",
    ),
    # Final Submission and publication-version decisions.
    AuditActionDefinition(
        "final_submission_create",
        "Create Final Submission",
        "submissions",
        ("final_submission_manual_create",),
    ),
    AuditActionDefinition(
        "final_submission_edit",
        "Edit Final Submission",
        "submissions",
        ("final_submission_manual_edit",),
    ),
    AuditActionDefinition(
        "final_submission_delete",
        "Delete Final Submission",
        "submissions",
        ("final_submission_delete_blocked",),
    ),
    AuditActionDefinition(
        "final_submission_version_action",
        "Final Submission Version Action",
        "submissions",
        ("final_submission_list_action", "final_submission_discard_action"),
    ),
    AuditActionDefinition(
        "final_submission_discard",
        "Discard Final Submission",
        "submissions",
        ("discard_submission",),
    ),
    AuditActionDefinition(
        "final_submission_discard_undo",
        "Undo Final Submission Discard",
        "submissions",
        ("undo_discard_submission",),
    ),
    AuditActionDefinition("editor_upload_preview", "Preview Editor Upload", "submissions"),
    AuditActionDefinition(
        "editor_upload_apply",
        "Apply Editor Upload",
        "submissions",
        ("editor_upload_confirm", "editor_upload_create"),
    ),
    AuditActionDefinition(
        "editor_upload_cancel",
        "Cancel Editor Upload",
        "submissions",
        ("editor_upload_preview_cancel", "editor_upload_preview_canceled"),
    ),
    AuditActionDefinition(
        "publication_exclusion_apply",
        "Mark Not Publishing",
        "submissions",
        ("mark_not_publishing",),
    ),
    AuditActionDefinition(
        "publication_exclusion_undo",
        "Undo Not Publishing",
        "submissions",
        ("undo_not_publishing",),
    ),
    # Review workflows.
    AuditActionDefinition(
        "paper_id_verify",
        "Verify Paper ID",
        "reviews",
        ("verify_paper_id",),
    ),
    AuditActionDefinition(
        "paper_id_unverify",
        "Move Paper ID Back to Review",
        "reviews",
        ("unverify_paper_id",),
    ),
    AuditActionDefinition(
        "title_author_extract_builtin",
        "Extract Title/Authors with Built-in Extractor",
        "reviews",
        ("title_author_extract",),
    ),
    AuditActionDefinition(
        "title_author_extract_builtin_batch",
        "Extract Title/Authors Needing Review",
        "reviews",
        ("title_author_extract_needs_review",),
    ),
    AuditActionDefinition(
        "title_author_extract_builtin_all",
        "Re-extract All Title/Authors",
        "reviews",
        ("title_author_reextract_all",),
    ),
    AuditActionDefinition(
        "title_author_extract_grobid",
        "Extract Title/Authors with GROBID",
        "reviews",
        ("grobid_title_author_extract",),
    ),
    AuditActionDefinition(
        "title_author_extract_grobid_batch",
        "Extract Suspicious Title/Authors with GROBID",
        "reviews",
        ("grobid_title_author_extract_batch",),
    ),
    AuditActionDefinition(
        "title_author_manual_override",
        "Override Extracted Title/Authors",
        "reviews",
    ),
    AuditActionDefinition(
        "title_author_review_update",
        "Update Title/Author Review",
        "reviews",
        ("title_author_review_status",),
    ),
    AuditActionDefinition(
        "title_match_verify",
        "Verify Extracted Title Match",
        "reviews",
        ("verify_extracted_title_match",),
    ),
    AuditActionDefinition(
        "title_match_unverify",
        "Move Extracted Title Match Back to Review",
        "reviews",
        ("unverify_extracted_title_match",),
    ),
    AuditActionDefinition(
        "duplicate_author_review",
        "Review Duplicate Author",
        "reviews",
    ),
    AuditActionDefinition(
        "duplicate_author_review_reset",
        "Reset Duplicate Author Review",
        "reviews",
    ),
    # Formatting.
    AuditActionDefinition(
        "formatting_review_update",
        "Update Formatting Review",
        "formatting",
        ("formatting_update",),
    ),
    AuditActionDefinition(
        "formatting_upload_apply",
        "Apply Formatting Upload",
        "formatting",
        ("formatting_upload_confirm",),
    ),
    AuditActionDefinition(
        "formatting_upload_preview",
        "Preview Formatting Upload",
        "formatting",
    ),
    AuditActionDefinition(
        "formatting_upload_cancel",
        "Cancel Formatting Upload",
        "formatting",
        ("formatting_upload_preview_canceled",),
    ),
    AuditActionDefinition(
        "formatting_issue_record",
        "Record Formatting Issue",
        "formatting",
        ("formatting_issue_recorded_from_pdf_preview",),
    ),
    # PDF processing.
    AuditActionDefinition(
        "pdf_process",
        "Process Publication PDFs",
        "processing",
        ("process_pdfs",),
    ),
    AuditActionDefinition(
        "publication_pdf_debug_sync",
        "Sync Publication PDF Debug Copies",
        "processing",
        ("sync_publication_pdf_debug",),
    ),
    # CrossCheck and plagiarism.
    AuditActionDefinition(
        "crosscheck_pdf_export",
        "Export CrossCheck PDFs",
        "plagiarism",
        ("crosscheck_export",),
    ),
    AuditActionDefinition(
        "crosscheck_result_import",
        "Import CrossCheck Results",
        "plagiarism",
    ),
    AuditActionDefinition(
        "crosscheck_report_upload",
        "Upload CrossCheck Reports",
        "plagiarism",
    ),
    AuditActionDefinition(
        "external_results_import",
        "Import External Results",
        "plagiarism",
    ),
    # Exceptions.
    AuditActionDefinition("exception_allow", "Allow Exception", "exceptions"),
    AuditActionDefinition("exception_remove", "Remove Exception", "exceptions"),
    # Reports and exports.
    AuditActionDefinition(
        "report_active_versions_export",
        "Export Active Versions",
        "exports",
        ("export_active_versions",),
    ),
    AuditActionDefinition(
        "report_author_count_export",
        "Export Author Count",
        "exports",
        ("export_author_count",),
    ),
    AuditActionDefinition(
        "report_editorial_workbook_export",
        "Export Editorial Publication Workbook",
        "exports",
        ("export_editorial_review_workbook",),
    ),
    AuditActionDefinition(
        "report_error_export",
        "Export Readiness Issues",
        "exports",
        ("export_error_report",),
    ),
    AuditActionDefinition(
        "report_old_versions_export",
        "Export Old Versions",
        "exports",
        ("export_old_versions",),
    ),
    AuditActionDefinition(
        "publication_package_export",
        "Export Publication Package",
        "exports",
    ),
    # Settings, storage, and destructive maintenance.
    AuditActionDefinition(
        "settings_update",
        "Update Settings",
        "administration",
        ("settings_save",),
    ),
    AuditActionDefinition(
        "settings_folder_reset",
        "Reset Settings Folders",
        "administration",
        ("settings_reset_folders",),
    ),
    AuditActionDefinition(
        "settings_active_version_rule_preview",
        "Preview Active Version Rule Change",
        "administration",
    ),
    AuditActionDefinition(
        "settings_active_version_rule_apply",
        "Apply Active Version Rule Change",
        "administration",
    ),
    AuditActionDefinition(
        "settings_active_version_rule_cancel",
        "Cancel Active Version Rule Change",
        "administration",
    ),
    AuditActionDefinition(
        "storage_cleanup_preview",
        "Preview Storage Cleanup",
        "administration",
    ),
    AuditActionDefinition(
        "storage_cleanup_apply",
        "Apply Storage Cleanup",
        "administration",
    ),
    AuditActionDefinition(
        "database_clear_request",
        "Request Clear Database",
        "administration",
        ("clear_database_requested",),
    ),
    AuditActionDefinition(
        "database_clear_apply",
        "Apply Clear Database",
        "administration",
        ("clear_database_apply",),
    ),
    AuditActionDefinition(
        "database_clear_complete",
        "Complete Clear Database",
        "administration",
        ("clear_database_applied",),
    ),
    AuditActionDefinition(
        "audit_log_archive_clear",
        "Archive and Clear Audit Log",
        "administration",
        ("audit_log_archived_and_cleared",),
    ),
    # System State backup and restore.
    AuditActionDefinition(
        "system_state_export_request",
        "Request System State Export",
        "system_state",
        ("system_state_export_requested",),
    ),
    AuditActionDefinition(
        "system_state_export",
        "Export System State",
        "system_state",
    ),
    AuditActionDefinition(
        "system_state_restore_preview",
        "Preview System State Restore",
        "system_state",
    ),
    AuditActionDefinition(
        "system_state_restore_apply",
        "Apply System State Restore",
        "system_state",
    ),
    AuditActionDefinition(
        "system_state_restore_cleanup",
        "Clean Up System State Restore",
        "system_state",
        ("system_state_restore_cleanup_warning",),
    ),
)


_ACTION_CODES = [definition.code for definition in _ACTION_DEFINITIONS]
if len(_ACTION_CODES) != len(set(_ACTION_CODES)):
    raise RuntimeError("Audit action registry contains duplicate canonical codes.")

AUDIT_ACTIONS = {definition.code: definition for definition in _ACTION_DEFINITIONS}
AUDIT_ACTION_ALIASES = {
    alias: definition.code
    for definition in _ACTION_DEFINITIONS
    for alias in definition.aliases
}
_ALIAS_COUNT = sum(len(definition.aliases) for definition in _ACTION_DEFINITIONS)
if len(AUDIT_ACTION_ALIASES) != _ALIAS_COUNT:
    raise RuntimeError("Audit action registry contains duplicate legacy aliases.")
if set(AUDIT_ACTIONS) & set(AUDIT_ACTION_ALIASES):
    raise RuntimeError("Audit action aliases must not reuse canonical codes.")


def canonical_audit_action(action):
    action = str(action or "").strip()
    return AUDIT_ACTION_ALIASES.get(action, action)


def audit_action_metadata(action):
    raw_action = str(action or "").strip()
    canonical_action = canonical_audit_action(raw_action)
    definition = AUDIT_ACTIONS.get(canonical_action)
    if definition:
        return {
            "action": definition.code,
            "action_label": definition.label,
            "category": definition.category,
            "category_label": AUDIT_CATEGORY_LABELS[definition.category],
            "raw_action": raw_action,
            "registered": True,
        }
    label = canonical_action.replace("_", " ").strip().title() or "Unknown Action"
    return {
        "action": canonical_action,
        "action_label": label,
        "category": "other",
        "category_label": AUDIT_CATEGORY_LABELS["other"],
        "raw_action": raw_action,
        "registered": False,
    }


def audit_category_options():
    return [
        {"value": value, "label": label}
        for value, label in AUDIT_CATEGORY_LABELS.items()
    ]


def audit_action_groups(category=""):
    definitions = sorted(
        (
            definition
            for definition in _ACTION_DEFINITIONS
            if not category or definition.category == category
        ),
        key=lambda definition: (
            AUDIT_CATEGORY_LABELS[definition.category],
            definition.label,
        ),
    )
    groups = []
    for category_code, category_label in AUDIT_CATEGORY_LABELS.items():
        rows = [
            {"value": definition.code, "label": definition.label}
            for definition in definitions
            if definition.category == category_code
        ]
        if rows:
            groups.append(
                {
                    "value": category_code,
                    "label": category_label,
                    "actions": rows,
                }
            )
    return groups
