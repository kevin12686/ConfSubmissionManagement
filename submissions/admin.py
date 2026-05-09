from django.contrib import admin

from .models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    FinalSubmissionFileState,
    FinalSubmissionIdentityState,
    FinalSubmissionPlagiarismState,
    FinalSubmissionPublicationState,
    FinalSubmissionReviewState,
    InitialPaper,
    PaperAuthor,
)


class FinalSubmissionIdentityStateInline(admin.StackedInline):
    model = FinalSubmissionIdentityState
    can_delete = False
    extra = 0


class FinalSubmissionFileStateInline(admin.StackedInline):
    model = FinalSubmissionFileState
    can_delete = False
    extra = 0


class FinalSubmissionReviewStateInline(admin.StackedInline):
    model = FinalSubmissionReviewState
    can_delete = False
    extra = 0


class FinalSubmissionPublicationStateInline(admin.StackedInline):
    model = FinalSubmissionPublicationState
    can_delete = False
    extra = 0


class FinalSubmissionPlagiarismStateInline(admin.StackedInline):
    model = FinalSubmissionPlagiarismState
    can_delete = False
    extra = 0


@admin.register(InitialPaper)
class InitialPaperAdmin(admin.ModelAdmin):
    list_display = ("paper_id", "acceptance_status", "title", "has_notes")
    search_fields = ("paper_id", "acceptance_status", "title", "authors", "notes")

    @admin.display(boolean=True, description="Notes")
    def has_notes(self, obj):
        return bool(obj.notes)


@admin.register(FinalSubmission)
class FinalSubmissionAdmin(admin.ModelAdmin):
    inlines = (
        FinalSubmissionIdentityStateInline,
        FinalSubmissionFileStateInline,
        FinalSubmissionReviewStateInline,
        FinalSubmissionPublicationStateInline,
        FinalSubmissionPlagiarismStateInline,
    )
    list_display = (
        "final_submission_id",
        "paper_id_filled",
        "start2_paper_id_raw",
        "submission_origin",
        "upload_date",
        "active_version",
        "duplicate_submission",
        "discarded",
        "excluded_from_publication",
        "verification_status",
        "title_author_extraction_status",
        "title_author_review_status",
        "title_author_verified",
        "duplicate_author_review_status",
        "extracted_title_match_status",
        "extracted_title_verified",
        "format_status",
        "similarity_score",
        "single_similarity_score",
        "plagiarism_report_stale",
        "page_count",
        "page_limit_exception_approved",
        "author_number_exception_approved",
        "processing_status",
    )
    list_filter = (
        "active_version",
        "submission_origin",
        "duplicate_submission",
        "discarded",
        "excluded_from_publication",
        "publication_exclusion_reason",
        "paper_id_verified",
        "verification_status",
        "processing_status",
        "title_author_extraction_status",
        "title_author_review_status",
        "title_author_verified",
        "duplicate_author_review_status",
        "extracted_title_match_status",
        "extracted_title_verified",
        "format_status",
        "plagiarism_status",
        "plagiarism_report_stale",
        "page_limit_exception_approved",
        "author_number_exception_approved",
    )
    search_fields = (
        "final_submission_id",
        "paper_id_filled",
        "start2_paper_id_raw",
        "final_submission_title",
        "extracted_title",
    )


@admin.register(PaperAuthor)
class PaperAuthorAdmin(admin.ModelAdmin):
    list_display = ("author_name", "normalized_author_name", "paper_id", "final_submission")
    search_fields = ("author_name", "normalized_author_name", "paper_id")


@admin.register(AuthorLimitWaiver)
class AuthorLimitWaiverAdmin(admin.ModelAdmin):
    list_display = (
        "display_author_name",
        "normalized_author_name",
        "approved",
        "approved_publication_paper_count",
        "approved_at",
    )
    list_filter = ("approved",)
    search_fields = ("display_author_name", "normalized_author_name", "reason")


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    fieldsets = (
        (
            "Conference",
            {
                "fields": (
                    "conference_name",
                )
            },
        ),
        (
            "Limits",
            {
                "fields": (
                    "page_minimum",
                    "page_limit",
                    "author_paper_limit",
                    "max_authors_per_paper",
                    "title_words_for_filename",
                    "active_version_rule",
                    "time_zone",
                    "plagiarism_percent_threshold",
                    "single_similarity_threshold",
                )
            },
        ),
        (
            "Folders",
            {
                "fields": (
                    "incoming_folder",
                    "active_final_folder",
                    "old_versions_folder",
                    "reports_folder",
                    "extraction_results_folder",
                    "plagiarism_reports_folder",
                )
            },
        ),
    )
