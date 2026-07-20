from .controllers.dashboard import dashboard, dashboard_summary
from .controllers.papers import (
    import_initial_papers_view,
    initial_paper_delete,
    initial_paper_form,
    initial_paper_list,
)
from .controllers.final_submissions import (
    editor_upload_form,
    editor_upload_preview_pdf,
    final_submission_delete,
    final_submission_form,
    final_submission_list,
    import_final_submissions_view,
    final_submission_display_pdf,
    final_submission_display_source,
    plagiarism_report,
    publication_debug_pdf,
    publication_pdf,
    publication_source,
)
from .controllers.reviews import (
    exceptions_center,
    formatting,
    formatting_preview,
    not_publishing_list,
    organized_list,
    title_author_extraction,
    title_author_manual_override_form,
    verify_paper_ids,
)
from .controllers.processing import process_pdfs_view
from .controllers.exports import (
    active_versions,
    author_count,
    download_template,
    error_report,
    export_reports,
    old_versions,
)
from .controllers.audit import audit_log, download_audit_log
from .controllers.integrations import (
    download_crosscheck_zip,
    download_crosscheck_zip_scoped,
    download_system_state,
    integration,
    system_state,
)
from .controllers.settings import (
    app_settings,
    clear_database,
    grobid_health_check,
    storage_inventory_panel,
)
from .controllers.ui import publication_duplicate_details, workflow_alerts
