from django.db import transaction

from submissions.services.checks import rebuild_paper_authors
from submissions.services.final_submission_state import (
    sync_all_submission_state_records,
)
from submissions.services.import_export import _mark_duplicate_submissions
from submissions.services.pdf_processor import determine_active_versions


def recompute_active_and_duplicate_state(*, refresh_author_cache=True):
    with transaction.atomic():
        determine_active_versions(
            sync_state_records=False,
            rebuild_authors=False,
        )
        duplicate_count = _mark_duplicate_submissions(
            sync_state_records=False,
        )
        sync_all_submission_state_records(domain_keys={"identity"})
        if refresh_author_cache:
            rebuild_paper_authors()
    return duplicate_count
