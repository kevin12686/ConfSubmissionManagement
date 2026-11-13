from submissions.services.editor_uploads import editor_conflict_count
from submissions.services.file_manager import active_pdfs_needing_processing
from submissions.services.publication_read import PublicationReadContext


def workflow_alert_counts():
    """Read advisory counts afresh; a failed read must not mean zero work."""
    context = PublicationReadContext.load()
    return {
        "active_pdfs_need_processing": len(
            active_pdfs_needing_processing(
                context.file_inspection,
                submissions=context.master_submissions,
            )
        ),
        "start2_editor_conflicts": editor_conflict_count(),
    }
