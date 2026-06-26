from django.db import models
from django.utils import timezone

from submissions.services.text_utils import clean_note_text


TITLE_AUTHOR_SOURCE_CHOICES = [
    ("unknown", "Unknown"),
    ("built_in_extractor", "Built-in extractor"),
    ("grobid", "GROBID"),
    ("manual_override", "Manual override"),
    ("manual", "Manual"),
    ("external_import", "External import"),
    ("external_script", "External script"),
]

TITLE_AUTHOR_EXTRACTION_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("extracted", "Extracted"),
    ("error", "Error"),
]

TITLE_AUTHOR_REVIEW_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("red_flag", "Red Flag"),
    ("review_ok", "Review OK"),
]

DUPLICATE_AUTHOR_REVIEW_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("review_ok", "Review OK"),
]

PROCESSING_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("processed", "Processed"),
    ("error", "Error"),
]

PLAGIARISM_STATUS_CHOICES = [
    ("", "Missing"),
    ("pending", "Pending"),
    ("clear", "Clear"),
    ("review", "Needs review"),
    ("flagged", "Flagged"),
]

VERIFICATION_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("verified", "Verified"),
    ("title_mismatch", "Title mismatch"),
    ("invalid_paper_id", "Invalid Paper ID"),
]

EXTRACTED_TITLE_MATCH_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("verified", "Verified"),
    ("title_mismatch", "Title mismatch"),
    ("missing", "Missing title"),
]

FORMAT_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("needs_edit", "Needs edit"),
    ("review_ok", "Review OK"),
]

SUBMISSION_ORIGIN_CHOICES = [
    ("start2", "Start2"),
    ("editor_upload", "Editor Upload"),
]

PUBLICATION_EXCLUSION_REASON_CHOICES = [
    ("", "Not excluded"),
    ("unpaid", "Unpaid"),
    ("withdrawn", "Withdrawn"),
    ("not_in_master", "Not in Master List"),
    ("other", "Other"),
]

ACTIVE_VERSION_RULE_CHOICES = [
    ("final_id", "Largest Final ID"),
    ("upload_date", "Latest upload date"),
]

TIME_ZONE_CHOICES = [
    ("America/Chicago", "Dallas / Central Time"),
    ("America/New_York", "Eastern Time"),
    ("America/Denver", "Mountain Time"),
    ("America/Los_Angeles", "Pacific Time"),
    ("UTC", "UTC"),
]


class InitialPaper(models.Model):
    paper_id = models.CharField(max_length=150, unique=True)
    acceptance_status = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=500, blank=True)
    authors = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    corresponding_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["paper_id"]

    def __str__(self):
        return self.paper_id

    def save(self, *args, **kwargs):
        self.notes = clean_note_text(self.notes)
        super().save(*args, **kwargs)


class FinalSubmission(models.Model):
    final_submission_id = models.CharField(max_length=100, unique=True)
    start2_paper_id_raw = models.CharField(max_length=150, blank=True)
    paper_id_filled = models.CharField(max_length=150, blank=True, db_index=True)
    final_submission_title = models.CharField(max_length=500, blank=True)
    final_submission_authors = models.TextField(blank=True)
    upload_date = models.DateTimeField(default=timezone.now)
    original_file_name = models.CharField(max_length=255, blank=True)
    pdf_file = models.FileField(upload_to="final_submissions/", blank=True, null=True)
    source_original_file_name = models.CharField(max_length=255, blank=True)
    source_file = models.FileField(upload_to="source_submissions/", blank=True, null=True)
    source_current_file_path = models.TextField(blank=True)
    current_file_path = models.TextField(blank=True)
    submission_origin = models.CharField(
        max_length=30,
        choices=SUBMISSION_ORIGIN_CHOICES,
        default="start2",
        db_index=True,
    )
    editor_upload_notes = models.TextField(blank=True)
    editor_uploaded_at = models.DateTimeField(blank=True, null=True)
    extracted_title = models.CharField(max_length=500, blank=True)
    extracted_authors = models.TextField(blank=True)
    title_author_source = models.CharField(
        max_length=30, choices=TITLE_AUTHOR_SOURCE_CHOICES, default="unknown"
    )
    title_author_imported_at = models.DateTimeField(blank=True, null=True)
    title_author_extraction_status = models.CharField(
        max_length=30, choices=TITLE_AUTHOR_EXTRACTION_STATUS_CHOICES, default="pending"
    )
    title_author_extraction_message = models.TextField(blank=True)
    title_author_verification_image = models.TextField(blank=True)
    title_author_manual_override_reason = models.TextField(blank=True)
    title_author_manual_override_at = models.DateTimeField(blank=True, null=True)
    title_author_verified = models.BooleanField(default=False, db_index=True)
    title_author_verified_at = models.DateTimeField(blank=True, null=True)
    title_author_review_status = models.CharField(
        max_length=30,
        choices=TITLE_AUTHOR_REVIEW_STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    duplicate_author_review_status = models.CharField(
        max_length=30,
        choices=DUPLICATE_AUTHOR_REVIEW_STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    duplicate_author_review_notes = models.TextField(blank=True)
    duplicate_author_reviewed_at = models.DateTimeField(blank=True, null=True)
    author_number_exception_approved = models.BooleanField(default=False, db_index=True)
    author_number_exception_reason = models.TextField(blank=True)
    author_number_exception_author_count = models.PositiveIntegerField(blank=True, null=True)
    author_number_exception_approved_at = models.DateTimeField(blank=True, null=True)
    extracted_title_match_status = models.CharField(
        max_length=30, choices=EXTRACTED_TITLE_MATCH_STATUS_CHOICES, default="pending"
    )
    extracted_title_match_score = models.FloatField(blank=True, null=True)
    extracted_title_match_message = models.TextField(blank=True)
    extracted_title_verified = models.BooleanField(default=False, db_index=True)
    extracted_title_auto_verify_blocked = models.BooleanField(default=False, db_index=True)
    extracted_title_verified_at = models.DateTimeField(blank=True, null=True)
    page_count = models.PositiveIntegerField(blank=True, null=True)
    page_limit_exception_approved = models.BooleanField(default=False, db_index=True)
    page_limit_exception_reason = models.TextField(blank=True)
    page_limit_exception_page_count = models.PositiveIntegerField(blank=True, null=True)
    page_limit_exception_approved_at = models.DateTimeField(blank=True, null=True)
    pdf_hash = models.CharField(max_length=64, blank=True)
    thumbnail_folder = models.TextField(blank=True)
    thumbnail_status = models.CharField(max_length=30, blank=True)
    thumbnail_message = models.TextField(blank=True)
    active_version = models.BooleanField(default=True, db_index=True)
    plagiarism_status = models.CharField(
        max_length=30, choices=PLAGIARISM_STATUS_CHOICES, blank=True, default=""
    )
    similarity_score = models.DecimalField(
        max_digits=5, decimal_places=2, blank=True, null=True
    )
    single_similarity_score = models.DecimalField(
        max_digits=5, decimal_places=2, blank=True, null=True
    )
    plagiarism_report_path = models.TextField(blank=True)
    plagiarism_report_stale = models.BooleanField(default=False, db_index=True)
    plagiarism_imported_at = models.DateTimeField(blank=True, null=True)
    processing_status = models.CharField(
        max_length=30, choices=PROCESSING_STATUS_CHOICES, default="pending"
    )
    processing_message = models.TextField(blank=True)
    formatted_pdf_file = models.FileField(upload_to="formatted_pdfs/", blank=True, null=True)
    formatted_source_file = models.FileField(upload_to="formatted_sources/", blank=True, null=True)
    formatted_pdf_uploaded_at = models.DateTimeField(blank=True, null=True)
    formatted_source_uploaded_at = models.DateTimeField(blank=True, null=True)
    format_status = models.CharField(
        max_length=30, choices=FORMAT_STATUS_CHOICES, default="pending", db_index=True
    )
    format_notes = models.TextField(blank=True)
    mapping_source = models.CharField(max_length=100, blank=True)
    mapping_order = models.PositiveIntegerField(blank=True, null=True, db_index=True)
    duplicate_submission = models.BooleanField(default=False, db_index=True)
    discarded = models.BooleanField(default=False, db_index=True)
    discard_notes = models.TextField(blank=True)
    discarded_at = models.DateTimeField(blank=True, null=True)
    excluded_from_publication = models.BooleanField(default=False, db_index=True)
    publication_exclusion_reason = models.CharField(
        max_length=30,
        choices=PUBLICATION_EXCLUSION_REASON_CHOICES,
        blank=True,
        default="",
    )
    publication_exclusion_notes = models.TextField(blank=True)
    publication_excluded_at = models.DateTimeField(blank=True, null=True)
    paper_id_verified = models.BooleanField(default=False, db_index=True)
    auto_verify_blocked = models.BooleanField(default=False, db_index=True)
    verification_status = models.CharField(
        max_length=30, choices=VERIFICATION_STATUS_CHOICES, default="pending"
    )
    title_match_score = models.FloatField(blank=True, null=True)
    verification_message = models.TextField(blank=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["paper_id_filled", "-upload_date", "-created_at"]
        indexes = [
            models.Index(
                fields=["active_version", "excluded_from_publication", "paper_id_filled"],
                name="sub_final_active_pub_idx",
            ),
            models.Index(
                fields=[
                    "active_version",
                    "title_author_review_status",
                    "extracted_title_verified",
                ],
                name="sub_final_title_review_idx",
            ),
            models.Index(
                fields=["active_version", "format_status"],
                name="sub_final_format_idx",
            ),
        ]

    def __str__(self):
        return f"{self.final_submission_id} - {self.paper_id_filled}"

    @property
    def has_corrected_files(self):
        return bool(self.formatted_pdf_file or self.formatted_source_file)

    @property
    def has_valid_page_limit_exception(self):
        return bool(
            self.page_limit_exception_approved
            and self.page_limit_exception_reason.strip()
            and self.page_count is not None
            and self.page_limit_exception_page_count == self.page_count
        )

    def save(self, *args, **kwargs):
        if self.pdf_file and not self.original_file_name:
            self.original_file_name = self.pdf_file.name.split("/")[-1]
        if self.source_file and not self.source_original_file_name:
            self.source_original_file_name = self.source_file.name.split("/")[-1]
        if self.title_author_review_status == "review_ok":
            self.title_author_verified = True
            self.title_author_verified_at = self.title_author_verified_at or timezone.now()
        else:
            self.title_author_verified = False
            self.title_author_verified_at = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            update_fields.update({"title_author_verified", "title_author_verified_at"})
            kwargs["update_fields"] = list(update_fields)
        super().save(*args, **kwargs)
        self.sync_state_records()

    def sync_state_records(self):
        FinalSubmissionIdentityState.objects.update_or_create(
            final_submission=self,
            defaults={
                "submission_identifier": self.final_submission_id,
                "start2_paper_id_raw": self.start2_paper_id_raw,
                "paper_id_filled": self.paper_id_filled,
                "final_submission_title": self.final_submission_title,
                "final_submission_authors": self.final_submission_authors,
                "upload_date": self.upload_date,
                "active_version": self.active_version,
                "duplicate_submission": self.duplicate_submission,
                "submission_origin": self.submission_origin,
                "editor_upload_notes": self.editor_upload_notes,
                "editor_uploaded_at": self.editor_uploaded_at,
                "mapping_source": self.mapping_source,
                "mapping_order": self.mapping_order,
            },
        )
        FinalSubmissionFileState.objects.update_or_create(
            final_submission=self,
            defaults={
                "original_file_name": self.original_file_name,
                "pdf_file_name": self.pdf_file.name if self.pdf_file else "",
                "source_original_file_name": self.source_original_file_name,
                "source_file_name": self.source_file.name if self.source_file else "",
                "current_file_path": self.current_file_path,
                "source_current_file_path": self.source_current_file_path,
                "formatted_pdf_file_name": (
                    self.formatted_pdf_file.name if self.formatted_pdf_file else ""
                ),
                "formatted_source_file_name": (
                    self.formatted_source_file.name if self.formatted_source_file else ""
                ),
                "formatted_pdf_uploaded_at": self.formatted_pdf_uploaded_at,
                "formatted_source_uploaded_at": self.formatted_source_uploaded_at,
                "page_count": self.page_count,
                "pdf_hash": self.pdf_hash,
                "thumbnail_folder": self.thumbnail_folder,
                "thumbnail_status": self.thumbnail_status,
                "thumbnail_message": self.thumbnail_message,
                "processing_status": self.processing_status,
                "processing_message": self.processing_message,
            },
        )
        FinalSubmissionReviewState.objects.update_or_create(
            final_submission=self,
            defaults={
                "paper_id_verified": self.paper_id_verified,
                "auto_verify_blocked": self.auto_verify_blocked,
                "verification_status": self.verification_status,
                "title_match_score": self.title_match_score,
                "verification_message": self.verification_message,
                "verified_at": self.verified_at,
                "extracted_title": self.extracted_title,
                "extracted_authors": self.extracted_authors,
                "title_author_source": self.title_author_source,
                "title_author_imported_at": self.title_author_imported_at,
                "title_author_extraction_status": self.title_author_extraction_status,
                "title_author_extraction_message": self.title_author_extraction_message,
                "title_author_verification_image": self.title_author_verification_image,
                "title_author_manual_override_reason": self.title_author_manual_override_reason,
                "title_author_manual_override_at": self.title_author_manual_override_at,
                "title_author_verified": self.title_author_verified,
                "title_author_verified_at": self.title_author_verified_at,
                "title_author_review_status": self.title_author_review_status,
                "duplicate_author_review_status": self.duplicate_author_review_status,
                "duplicate_author_review_notes": self.duplicate_author_review_notes,
                "duplicate_author_reviewed_at": self.duplicate_author_reviewed_at,
                "extracted_title_match_status": self.extracted_title_match_status,
                "extracted_title_match_score": self.extracted_title_match_score,
                "extracted_title_match_message": self.extracted_title_match_message,
                "extracted_title_verified": self.extracted_title_verified,
                "extracted_title_auto_verify_blocked": self.extracted_title_auto_verify_blocked,
                "extracted_title_verified_at": self.extracted_title_verified_at,
                "format_status": self.format_status,
                "format_notes": self.format_notes,
            },
        )
        FinalSubmissionPublicationState.objects.update_or_create(
            final_submission=self,
            defaults={
                "excluded_from_publication": self.excluded_from_publication,
                "publication_exclusion_reason": self.publication_exclusion_reason,
                "publication_exclusion_notes": self.publication_exclusion_notes,
                "publication_excluded_at": self.publication_excluded_at,
                "discarded": self.discarded,
                "discard_notes": self.discard_notes,
                "discarded_at": self.discarded_at,
                "page_limit_exception_approved": self.page_limit_exception_approved,
                "page_limit_exception_reason": self.page_limit_exception_reason,
                "page_limit_exception_page_count": self.page_limit_exception_page_count,
                "page_limit_exception_approved_at": self.page_limit_exception_approved_at,
                "author_number_exception_approved": self.author_number_exception_approved,
                "author_number_exception_reason": self.author_number_exception_reason,
                "author_number_exception_author_count": (
                    self.author_number_exception_author_count
                ),
                "author_number_exception_approved_at": (
                    self.author_number_exception_approved_at
                ),
            },
        )
        FinalSubmissionPlagiarismState.objects.update_or_create(
            final_submission=self,
            defaults={
                "plagiarism_status": self.plagiarism_status,
                "similarity_score": self.similarity_score,
                "single_similarity_score": self.single_similarity_score,
                "plagiarism_report_path": self.plagiarism_report_path,
                "plagiarism_report_stale": self.plagiarism_report_stale,
                "plagiarism_imported_at": self.plagiarism_imported_at,
            },
        )


class FinalSubmissionIdentityState(models.Model):
    final_submission = models.OneToOneField(
        FinalSubmission, on_delete=models.CASCADE, related_name="identity_state"
    )
    submission_identifier = models.CharField(max_length=100, db_index=True)
    start2_paper_id_raw = models.CharField(max_length=150, blank=True)
    paper_id_filled = models.CharField(max_length=150, blank=True, db_index=True)
    final_submission_title = models.CharField(max_length=500, blank=True)
    final_submission_authors = models.TextField(blank=True)
    upload_date = models.DateTimeField()
    active_version = models.BooleanField(default=True, db_index=True)
    duplicate_submission = models.BooleanField(default=False, db_index=True)
    submission_origin = models.CharField(
        max_length=30,
        choices=SUBMISSION_ORIGIN_CHOICES,
        default="start2",
        db_index=True,
    )
    editor_upload_notes = models.TextField(blank=True)
    editor_uploaded_at = models.DateTimeField(blank=True, null=True)
    mapping_source = models.CharField(max_length=100, blank=True)
    mapping_order = models.PositiveIntegerField(blank=True, null=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["paper_id_filled", "-upload_date", "submission_identifier"]


class FinalSubmissionFileState(models.Model):
    final_submission = models.OneToOneField(
        FinalSubmission, on_delete=models.CASCADE, related_name="file_state"
    )
    original_file_name = models.CharField(max_length=255, blank=True)
    pdf_file_name = models.TextField(blank=True)
    source_original_file_name = models.CharField(max_length=255, blank=True)
    source_file_name = models.TextField(blank=True)
    current_file_path = models.TextField(blank=True)
    source_current_file_path = models.TextField(blank=True)
    formatted_pdf_file_name = models.TextField(blank=True)
    formatted_source_file_name = models.TextField(blank=True)
    formatted_pdf_uploaded_at = models.DateTimeField(blank=True, null=True)
    formatted_source_uploaded_at = models.DateTimeField(blank=True, null=True)
    page_count = models.PositiveIntegerField(blank=True, null=True)
    pdf_hash = models.CharField(max_length=64, blank=True)
    thumbnail_folder = models.TextField(blank=True)
    thumbnail_status = models.CharField(max_length=30, blank=True)
    thumbnail_message = models.TextField(blank=True)
    processing_status = models.CharField(
        max_length=30, choices=PROCESSING_STATUS_CHOICES, default="pending"
    )
    processing_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class FinalSubmissionReviewState(models.Model):
    final_submission = models.OneToOneField(
        FinalSubmission, on_delete=models.CASCADE, related_name="review_state"
    )
    paper_id_verified = models.BooleanField(default=False, db_index=True)
    auto_verify_blocked = models.BooleanField(default=False, db_index=True)
    verification_status = models.CharField(
        max_length=30, choices=VERIFICATION_STATUS_CHOICES, default="pending"
    )
    title_match_score = models.FloatField(blank=True, null=True)
    verification_message = models.TextField(blank=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    extracted_title = models.CharField(max_length=500, blank=True)
    extracted_authors = models.TextField(blank=True)
    title_author_source = models.CharField(
        max_length=30, choices=TITLE_AUTHOR_SOURCE_CHOICES, default="unknown"
    )
    title_author_imported_at = models.DateTimeField(blank=True, null=True)
    title_author_extraction_status = models.CharField(
        max_length=30, choices=TITLE_AUTHOR_EXTRACTION_STATUS_CHOICES, default="pending"
    )
    title_author_extraction_message = models.TextField(blank=True)
    title_author_verification_image = models.TextField(blank=True)
    title_author_manual_override_reason = models.TextField(blank=True)
    title_author_manual_override_at = models.DateTimeField(blank=True, null=True)
    title_author_verified = models.BooleanField(default=False, db_index=True)
    title_author_verified_at = models.DateTimeField(blank=True, null=True)
    title_author_review_status = models.CharField(
        max_length=30,
        choices=TITLE_AUTHOR_REVIEW_STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    duplicate_author_review_status = models.CharField(
        max_length=30,
        choices=DUPLICATE_AUTHOR_REVIEW_STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    duplicate_author_review_notes = models.TextField(blank=True)
    duplicate_author_reviewed_at = models.DateTimeField(blank=True, null=True)
    extracted_title_match_status = models.CharField(
        max_length=30, choices=EXTRACTED_TITLE_MATCH_STATUS_CHOICES, default="pending"
    )
    extracted_title_match_score = models.FloatField(blank=True, null=True)
    extracted_title_match_message = models.TextField(blank=True)
    extracted_title_verified = models.BooleanField(default=False, db_index=True)
    extracted_title_auto_verify_blocked = models.BooleanField(default=False, db_index=True)
    extracted_title_verified_at = models.DateTimeField(blank=True, null=True)
    format_status = models.CharField(
        max_length=30, choices=FORMAT_STATUS_CHOICES, default="pending", db_index=True
    )
    format_notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class FinalSubmissionPublicationState(models.Model):
    final_submission = models.OneToOneField(
        FinalSubmission, on_delete=models.CASCADE, related_name="publication_state"
    )
    excluded_from_publication = models.BooleanField(default=False, db_index=True)
    publication_exclusion_reason = models.CharField(
        max_length=30,
        choices=PUBLICATION_EXCLUSION_REASON_CHOICES,
        blank=True,
        default="",
    )
    publication_exclusion_notes = models.TextField(blank=True)
    publication_excluded_at = models.DateTimeField(blank=True, null=True)
    discarded = models.BooleanField(default=False, db_index=True)
    discard_notes = models.TextField(blank=True)
    discarded_at = models.DateTimeField(blank=True, null=True)
    page_limit_exception_approved = models.BooleanField(default=False, db_index=True)
    page_limit_exception_reason = models.TextField(blank=True)
    page_limit_exception_page_count = models.PositiveIntegerField(blank=True, null=True)
    page_limit_exception_approved_at = models.DateTimeField(blank=True, null=True)
    author_number_exception_approved = models.BooleanField(default=False, db_index=True)
    author_number_exception_reason = models.TextField(blank=True)
    author_number_exception_author_count = models.PositiveIntegerField(blank=True, null=True)
    author_number_exception_approved_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)


class FinalSubmissionPlagiarismState(models.Model):
    final_submission = models.OneToOneField(
        FinalSubmission, on_delete=models.CASCADE, related_name="plagiarism_state"
    )
    plagiarism_status = models.CharField(
        max_length=30, choices=PLAGIARISM_STATUS_CHOICES, blank=True, default=""
    )
    similarity_score = models.DecimalField(
        max_digits=5, decimal_places=2, blank=True, null=True
    )
    single_similarity_score = models.DecimalField(
        max_digits=5, decimal_places=2, blank=True, null=True
    )
    plagiarism_report_path = models.TextField(blank=True)
    plagiarism_report_stale = models.BooleanField(default=False, db_index=True)
    plagiarism_imported_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)


def sync_final_submission_state_records(queryset=None):
    submissions = queryset or FinalSubmission.objects.all()
    for submission in submissions.iterator():
        submission.sync_state_records()


class PaperAuthor(models.Model):
    final_submission = models.ForeignKey(
        FinalSubmission, on_delete=models.CASCADE, related_name="paper_authors"
    )
    paper_id = models.CharField(max_length=150, db_index=True)
    author_name = models.CharField(max_length=255)
    normalized_author_name = models.CharField(max_length=255, db_index=True)
    author_order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["normalized_author_name", "paper_id", "author_order"]
        unique_together = [("final_submission", "normalized_author_name", "author_order")]

    def __str__(self):
        return f"{self.author_name} ({self.paper_id})"


class AuthorLimitWaiver(models.Model):
    normalized_author_name = models.CharField(max_length=255, unique=True)
    display_author_name = models.CharField(max_length=255, blank=True)
    approved = models.BooleanField(default=False, db_index=True)
    reason = models.TextField(blank=True)
    approved_publication_paper_count = models.PositiveIntegerField(blank=True, null=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_author_name"]

    def __str__(self):
        return self.display_author_name or self.normalized_author_name

    def is_valid_for_count(self, paper_count):
        return bool(
            self.approved
            and self.reason.strip()
            and self.approved_publication_paper_count == paper_count
        )


class AppSetting(models.Model):
    conference_name = models.CharField(max_length=255, blank=True, default="")
    page_minimum = models.PositiveIntegerField(default=6)
    page_limit = models.PositiveIntegerField(default=12)
    author_paper_limit = models.PositiveIntegerField(default=3)
    max_authors_per_paper = models.PositiveIntegerField(default=5)
    title_words_for_filename = models.PositiveIntegerField(default=5)
    active_version_rule = models.CharField(
        max_length=30, choices=ACTIVE_VERSION_RULE_CHOICES, default="final_id"
    )
    time_zone = models.CharField(
        max_length=80, choices=TIME_ZONE_CHOICES, default="America/Chicago"
    )
    incoming_folder = models.CharField(max_length=500, default="data/incoming")
    active_final_folder = models.CharField(max_length=500, default="data/active_final")
    old_versions_folder = models.CharField(max_length=500, default="data/old_versions")
    publication_pdf_debug_folder = models.CharField(
        max_length=500, default="data/publication_pdf_debug"
    )
    reports_folder = models.CharField(max_length=500, default="data/reports")
    extraction_results_folder = models.CharField(
        max_length=500, default="data/extraction_results"
    )
    plagiarism_reports_folder = models.CharField(
        max_length=500, default="data/plagiarism_reports"
    )
    grobid_enabled = models.BooleanField(default=False)
    grobid_api_url = models.CharField(max_length=500, default="http://localhost:8070")
    grobid_timeout_seconds = models.PositiveIntegerField(default=20)
    plagiarism_percent_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, default=35
    )
    single_similarity_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, default=10
    )
    class Meta:
        verbose_name = "Application setting"
        verbose_name_plural = "Application settings"

    def __str__(self):
        return "Conference final manager settings"

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
