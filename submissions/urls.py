from django.urls import path

from . import views


app_name = "submissions"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("papers/", views.initial_paper_list, name="initial_paper_list"),
    path("papers/add/", views.initial_paper_form, name="initial_paper_add"),
    path("papers/<int:pk>/edit/", views.initial_paper_form, name="initial_paper_edit"),
    path("papers/<int:pk>/delete/", views.initial_paper_delete, name="initial_paper_delete"),
    path("papers/import/", views.import_initial_papers_view, name="import_initial_papers"),
    path("submissions/organized/", views.organized_list, name="organized_list"),
    path("submissions/<int:pk>/publication-pdf/", views.publication_pdf, name="publication_pdf"),
    path("submissions/<int:pk>/plagiarism-report/", views.plagiarism_report, name="plagiarism_report"),
    path("submissions/", views.final_submission_list, name="final_submission_list"),
    path("submissions/editor-upload/", views.editor_upload_form, name="editor_upload"),
    path("submissions/add/", views.final_submission_form, name="final_submission_add"),
    path("submissions/<int:pk>/edit/", views.final_submission_form, name="final_submission_edit"),
    path("submissions/<int:pk>/delete/", views.final_submission_delete, name="final_submission_delete"),
    path("submissions/import/", views.import_final_submissions_view, name="import_final_submissions"),
    path("processing/pdfs/", views.process_pdfs_view, name="process"),
    path("reviews/title-authors/", views.title_author_extraction, name="title_author_extraction"),
    path("reviews/formatting/", views.formatting, name="formatting"),
    path("reviews/paper-ids/", views.verify_paper_ids, name="verify_paper_ids"),
    path("reviews/not-publishing/", views.not_publishing_list, name="not_publishing_list"),
    path("reports/active-versions/", views.active_versions, name="active_versions"),
    path("reports/old-versions/", views.old_versions, name="old_versions"),
    path("reports/errors/", views.error_report, name="error_report"),
    path("reviews/exceptions/", views.exceptions_center, name="exceptions_center"),
    path("reports/author-count/", views.author_count, name="author_count"),
    path("integrations/crosscheck/", views.integration, name="integration"),
    path("integrations/system-state/", views.integration, name="system_state"),
    path("integrations/crosscheck/zip/<str:token>/", views.download_crosscheck_zip, name="download_crosscheck_zip"),
    path("integrations/system-state/download/", views.download_system_state, name="download_system_state"),
    path("settings/", views.app_settings, name="settings"),
    path("settings/clear-database/", views.clear_database, name="clear_database"),
    path("reports/", views.export_reports, name="export_reports"),
    path("templates/<str:template_type>/", views.download_template, name="download_template"),
]
