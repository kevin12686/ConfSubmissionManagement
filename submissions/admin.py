from django.contrib import admin

from .models import AppSetting, FinalSubmission, InitialPaper, PaperAuthor


@admin.register(InitialPaper)
class InitialPaperAdmin(admin.ModelAdmin):
    list_display = ("paper_id", "acceptance_status", "title")
    search_fields = ("paper_id", "acceptance_status", "title", "authors")


@admin.register(FinalSubmission)
class FinalSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "final_submission_id",
        "paper_id_filled",
        "start2_paper_id_raw",
        "upload_date",
        "active_version",
        "duplicate_submission",
        "verification_status",
        "title_author_extraction_status",
        "title_author_verified",
        "extracted_title_match_status",
        "extracted_title_verified",
        "format_status",
        "similarity_score",
        "single_similarity_score",
        "page_count",
        "processing_status",
    )
    list_filter = (
        "active_version",
        "duplicate_submission",
        "paper_id_verified",
        "verification_status",
        "processing_status",
        "title_author_extraction_status",
        "title_author_verified",
        "extracted_title_match_status",
        "extracted_title_verified",
        "format_status",
        "plagiarism_status",
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


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    fieldsets = (
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
                    "title_author_script_path",
                )
            },
        ),
    )
