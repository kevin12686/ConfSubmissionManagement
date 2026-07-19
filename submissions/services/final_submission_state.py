from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.apps import apps
from django.db import transaction
from django.utils import timezone


def _file_name(value):
    return value.name if value else ""


@dataclass(frozen=True)
class StateField:
    target: str
    source: str
    transform: object = None

    def value_from(self, submission):
        value = getattr(submission, self.source)
        return self.transform(value) if self.transform else value


@dataclass(frozen=True)
class StateDomain:
    key: str
    model_name: str
    fields: tuple

    @property
    def source_fields(self):
        return frozenset(item.source for item in self.fields)

    @property
    def update_fields(self):
        return [item.target for item in self.fields] + ["updated_at"]


STATE_DOMAINS = (
    StateDomain(
        "identity",
        "FinalSubmissionIdentityState",
        (
            StateField("submission_identifier", "final_submission_id"),
            StateField("start2_paper_id_raw", "start2_paper_id_raw"),
            StateField("paper_id_filled", "paper_id_filled"),
            StateField("final_submission_title", "final_submission_title"),
            StateField("final_submission_authors", "final_submission_authors"),
            StateField("upload_date", "upload_date"),
            StateField("active_version", "active_version"),
            StateField("duplicate_submission", "duplicate_submission"),
            StateField("submission_origin", "submission_origin"),
            StateField("editor_upload_notes", "editor_upload_notes"),
            StateField("editor_uploaded_at", "editor_uploaded_at"),
            StateField("mapping_source", "mapping_source"),
            StateField("mapping_order", "mapping_order"),
        ),
    ),
    StateDomain(
        "file",
        "FinalSubmissionFileState",
        (
            StateField("original_file_name", "original_file_name"),
            StateField("pdf_file_name", "pdf_file", _file_name),
            StateField("source_original_file_name", "source_original_file_name"),
            StateField("source_file_name", "source_file", _file_name),
            StateField("current_file_path", "current_file_path"),
            StateField("source_current_file_path", "source_current_file_path"),
            StateField("formatted_pdf_file_name", "formatted_pdf_file", _file_name),
            StateField("formatted_source_file_name", "formatted_source_file", _file_name),
            StateField("formatted_pdf_uploaded_at", "formatted_pdf_uploaded_at"),
            StateField("formatted_source_uploaded_at", "formatted_source_uploaded_at"),
            StateField("page_count", "page_count"),
            StateField("pdf_hash", "pdf_hash"),
            StateField("thumbnail_folder", "thumbnail_folder"),
            StateField("thumbnail_status", "thumbnail_status"),
            StateField("thumbnail_message", "thumbnail_message"),
            StateField("processing_status", "processing_status"),
            StateField("processing_message", "processing_message"),
        ),
    ),
    StateDomain(
        "review",
        "FinalSubmissionReviewState",
        (
            StateField("paper_id_verified", "paper_id_verified"),
            StateField("auto_verify_blocked", "auto_verify_blocked"),
            StateField("verification_status", "verification_status"),
            StateField("title_match_score", "title_match_score"),
            StateField("verification_message", "verification_message"),
            StateField("verified_at", "verified_at"),
            StateField("extracted_title", "extracted_title"),
            StateField("extracted_authors", "extracted_authors"),
            StateField("title_author_source", "title_author_source"),
            StateField("title_author_imported_at", "title_author_imported_at"),
            StateField("title_author_extraction_status", "title_author_extraction_status"),
            StateField("title_author_extraction_message", "title_author_extraction_message"),
            StateField("title_author_verification_image", "title_author_verification_image"),
            StateField("title_author_manual_override_reason", "title_author_manual_override_reason"),
            StateField("title_author_manual_override_at", "title_author_manual_override_at"),
            StateField("title_author_verified", "title_author_verified"),
            StateField("title_author_verified_at", "title_author_verified_at"),
            StateField("title_author_review_status", "title_author_review_status"),
            StateField("duplicate_author_review_status", "duplicate_author_review_status"),
            StateField("duplicate_author_review_notes", "duplicate_author_review_notes"),
            StateField("duplicate_author_reviewed_at", "duplicate_author_reviewed_at"),
            StateField("extracted_title_match_status", "extracted_title_match_status"),
            StateField("extracted_title_match_score", "extracted_title_match_score"),
            StateField("extracted_title_match_message", "extracted_title_match_message"),
            StateField("extracted_title_verified", "extracted_title_verified"),
            StateField(
                "extracted_title_auto_verify_blocked",
                "extracted_title_auto_verify_blocked",
            ),
            StateField("extracted_title_verified_at", "extracted_title_verified_at"),
            StateField("format_status", "format_status"),
            StateField("format_notes", "format_notes"),
        ),
    ),
    StateDomain(
        "publication",
        "FinalSubmissionPublicationState",
        (
            StateField("excluded_from_publication", "excluded_from_publication"),
            StateField("publication_exclusion_reason", "publication_exclusion_reason"),
            StateField("publication_exclusion_notes", "publication_exclusion_notes"),
            StateField("publication_excluded_at", "publication_excluded_at"),
            StateField("discarded", "discarded"),
            StateField("discard_notes", "discard_notes"),
            StateField("discarded_at", "discarded_at"),
            StateField("page_limit_exception_approved", "page_limit_exception_approved"),
            StateField("page_limit_exception_reason", "page_limit_exception_reason"),
            StateField(
                "page_limit_exception_page_count",
                "page_limit_exception_page_count",
            ),
            StateField("page_limit_exception_approved_at", "page_limit_exception_approved_at"),
            StateField("author_number_exception_approved", "author_number_exception_approved"),
            StateField("author_number_exception_reason", "author_number_exception_reason"),
            StateField(
                "author_number_exception_author_count",
                "author_number_exception_author_count",
            ),
            StateField(
                "author_number_exception_approved_at",
                "author_number_exception_approved_at",
            ),
        ),
    ),
    StateDomain(
        "plagiarism",
        "FinalSubmissionPlagiarismState",
        (
            StateField("plagiarism_status", "plagiarism_status"),
            StateField("similarity_score", "similarity_score"),
            StateField("single_similarity_score", "single_similarity_score"),
            StateField(
                "plagiarism_percent_exception_approved",
                "plagiarism_percent_exception_approved",
            ),
            StateField(
                "plagiarism_percent_exception_reason",
                "plagiarism_percent_exception_reason",
            ),
            StateField(
                "plagiarism_percent_exception_approved_score",
                "plagiarism_percent_exception_approved_score",
            ),
            StateField(
                "plagiarism_percent_exception_approved_at",
                "plagiarism_percent_exception_approved_at",
            ),
            StateField(
                "single_percent_exception_approved",
                "single_percent_exception_approved",
            ),
            StateField(
                "single_percent_exception_reason",
                "single_percent_exception_reason",
            ),
            StateField(
                "single_percent_exception_approved_score",
                "single_percent_exception_approved_score",
            ),
            StateField(
                "single_percent_exception_approved_at",
                "single_percent_exception_approved_at",
            ),
            StateField("plagiarism_report_path", "plagiarism_report_path"),
            StateField("plagiarism_report_stale", "plagiarism_report_stale"),
            StateField("plagiarism_imported_at", "plagiarism_imported_at"),
        ),
    ),
)

STATE_DOMAIN_BY_KEY = {domain.key: domain for domain in STATE_DOMAINS}
ALL_STATE_DOMAIN_KEYS = frozenset(STATE_DOMAIN_BY_KEY)
MIRRORED_SOURCE_FIELDS = frozenset(
    source_field
    for domain in STATE_DOMAINS
    for source_field in domain.source_fields
)


@dataclass
class _DeferredStateSync:
    submission_ids: set = field(default_factory=set)
    domain_keys: set = field(default_factory=set)


_DEFERRED_STATE_SYNC = ContextVar("final_submission_deferred_state_sync", default=None)


def state_domain_keys_for_fields(update_fields):
    if update_fields is None:
        return set(ALL_STATE_DOMAIN_KEYS)
    changed_fields = set(update_fields) - {"created_at", "updated_at"}
    return {
        domain.key
        for domain in STATE_DOMAINS
        if changed_fields & domain.source_fields
    }


def _state_model(domain):
    return apps.get_model("submissions", domain.model_name)


def _state_object(domain, submission):
    model = _state_model(domain)
    values = {
        item.target: item.value_from(submission)
        for item in domain.fields
    }
    return model(final_submission_id=submission.pk, **values)


def _deduplicate_submissions(submissions):
    by_id = {}
    for submission in submissions:
        if submission.pk is None:
            raise ValueError("State records require a saved FinalSubmission.")
        by_id[submission.pk] = submission
    return list(by_id.values())


def bulk_sync_submission_state_records(
    submissions,
    *,
    domain_keys=None,
    batch_size=500,
):
    submissions = _deduplicate_submissions(submissions)
    if not submissions:
        return 0
    selected_keys = (
        set(ALL_STATE_DOMAIN_KEYS)
        if domain_keys is None
        else set(domain_keys)
    )
    unknown_keys = selected_keys - ALL_STATE_DOMAIN_KEYS
    if unknown_keys:
        raise ValueError(f"Unknown FinalSubmission state domains: {sorted(unknown_keys)}")
    if not selected_keys:
        return 0

    with transaction.atomic():
        for domain in STATE_DOMAINS:
            if domain.key not in selected_keys:
                continue
            model = _state_model(domain)
            model.objects.bulk_create(
                [_state_object(domain, submission) for submission in submissions],
                batch_size=batch_size,
                update_conflicts=True,
                update_fields=domain.update_fields,
                unique_fields=["final_submission"],
            )
    return len(submissions)


def sync_submission_state_records(submission, *, update_fields=None):
    domain_keys = state_domain_keys_for_fields(update_fields)
    return bulk_sync_submission_state_records(
        [submission],
        domain_keys=domain_keys,
    )


def schedule_submission_state_sync(submission, *, update_fields=None):
    domain_keys = state_domain_keys_for_fields(update_fields)
    if not domain_keys:
        return 0
    deferred = _DEFERRED_STATE_SYNC.get()
    if deferred is None:
        return bulk_sync_submission_state_records(
            [submission],
            domain_keys=domain_keys,
        )
    deferred.submission_ids.add(submission.pk)
    deferred.domain_keys.update(domain_keys)
    return 0


@contextmanager
def defer_submission_state_sync():
    existing = _DEFERRED_STATE_SYNC.get()
    if existing is not None:
        yield
        return

    deferred = _DeferredStateSync()
    token = _DEFERRED_STATE_SYNC.set(deferred)
    try:
        yield
        if deferred.submission_ids and deferred.domain_keys:
            final_submission_model = apps.get_model("submissions", "FinalSubmission")
            bulk_sync_submission_state_records(
                final_submission_model.objects.filter(
                    pk__in=deferred.submission_ids
                ),
                domain_keys=deferred.domain_keys,
            )
    finally:
        _DEFERRED_STATE_SYNC.reset(token)


def sync_all_submission_state_records(queryset=None, *, domain_keys=None):
    final_submission_model = apps.get_model("submissions", "FinalSubmission")
    submissions = (
        final_submission_model.objects.all()
        if queryset is None
        else queryset
    )
    return bulk_sync_submission_state_records(
        submissions,
        domain_keys=domain_keys,
    )


def bulk_update_submissions(
    submissions,
    update_fields,
    *,
    batch_size=500,
    sync_state=True,
):
    submissions = _deduplicate_submissions(submissions)
    if not submissions:
        return 0
    fields = set(update_fields)
    for submission in submissions:
        fields = submission.prepare_derived_fields_for_save(fields)
    now = timezone.now()
    for submission in submissions:
        submission.updated_at = now
    fields.add("updated_at")

    with transaction.atomic():
        final_submission_model = apps.get_model("submissions", "FinalSubmission")
        final_submission_model.objects.bulk_update(
            submissions,
            sorted(fields),
            batch_size=batch_size,
        )
        if sync_state:
            bulk_sync_submission_state_records(
                submissions,
                domain_keys=state_domain_keys_for_fields(fields),
                batch_size=batch_size,
            )
    return len(submissions)
