from django.db import models
from django.utils import timezone


TITLE_AUTHOR_SOURCE_CHOICES = [
    ("unknown", "Unknown"),
    ("manual", "Manual"),
    ("external_import", "External import"),
    ("external_script", "External script"),
]

TITLE_AUTHOR_EXTRACTION_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("extracted", "Extracted"),
    ("error", "Error"),
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
    corresponding_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["paper_id"]

    def __str__(self):
        return self.paper_id


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
    title_author_verified = models.BooleanField(default=False, db_index=True)
    title_author_verified_at = models.DateTimeField(blank=True, null=True)
    extracted_title_match_status = models.CharField(
        max_length=30, choices=EXTRACTED_TITLE_MATCH_STATUS_CHOICES, default="pending"
    )
    extracted_title_match_score = models.FloatField(blank=True, null=True)
    extracted_title_match_message = models.TextField(blank=True)
    extracted_title_verified = models.BooleanField(default=False, db_index=True)
    extracted_title_auto_verify_blocked = models.BooleanField(default=False, db_index=True)
    extracted_title_verified_at = models.DateTimeField(blank=True, null=True)
    page_count = models.PositiveIntegerField(blank=True, null=True)
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

    def __str__(self):
        return f"{self.final_submission_id} - {self.paper_id_filled}"

    @property
    def has_corrected_files(self):
        return bool(self.formatted_pdf_file or self.formatted_source_file)

    def save(self, *args, **kwargs):
        if self.pdf_file and not self.original_file_name:
            self.original_file_name = self.pdf_file.name.split("/")[-1]
        if self.source_file and not self.source_original_file_name:
            self.source_original_file_name = self.source_file.name.split("/")[-1]
        super().save(*args, **kwargs)


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


class AppSetting(models.Model):
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
    reports_folder = models.CharField(max_length=500, default="data/reports")
    extraction_results_folder = models.CharField(
        max_length=500, default="data/extraction_results"
    )
    plagiarism_reports_folder = models.CharField(
        max_length=500, default="data/plagiarism_reports"
    )
    plagiarism_percent_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, default=35
    )
    single_similarity_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, default=10
    )
    title_author_script_path = models.CharField(
        max_length=700,
        default="/Users/kevin/Codes/UTDConferenceTools/PDF Title/ExportTitleAuthor.py",
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
