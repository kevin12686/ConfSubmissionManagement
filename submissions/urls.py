from django.urls import path

from . import views


app_name = "submissions"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("initial-papers/", views.initial_paper_list, name="initial_paper_list"),
    path("initial-papers/add/", views.initial_paper_form, name="initial_paper_add"),
    path("initial-papers/<int:pk>/edit/", views.initial_paper_form, name="initial_paper_edit"),
    path("initial-papers/<int:pk>/delete/", views.initial_paper_delete, name="initial_paper_delete"),
    path("initial-papers/import/", views.import_initial_papers_view, name="import_initial_papers"),
    path("organized-list/", views.organized_list, name="organized_list"),
    path("final-submissions/<int:pk>/publication-pdf/", views.publication_pdf, name="publication_pdf"),
    path("final-submissions/<int:pk>/plagiarism-report/", views.plagiarism_report, name="plagiarism_report"),
    path("final-submissions/", views.final_submission_list, name="final_submission_list"),
    path("final-submissions/add/", views.final_submission_form, name="final_submission_add"),
    path("final-submissions/<int:pk>/edit/", views.final_submission_form, name="final_submission_edit"),
    path("final-submissions/<int:pk>/delete/", views.final_submission_delete, name="final_submission_delete"),
    path("final-submissions/import/", views.import_final_submissions_view, name="import_final_submissions"),
    path("process/", views.process_pdfs_view, name="process"),
    path("title-author-extraction/", views.title_author_extraction, name="title_author_extraction"),
    path("formatting/", views.formatting, name="formatting"),
    path("verify/", views.verify_paper_ids, name="verify_paper_ids"),
    path("active-versions/", views.active_versions, name="active_versions"),
    path("old-versions/", views.old_versions, name="old_versions"),
    path("errors/", views.error_report, name="error_report"),
    path("author-count/", views.author_count, name="author_count"),
    path("integration/", views.integration, name="integration"),
    path("integration/crosscheck-zip/<str:token>/", views.download_crosscheck_zip, name="download_crosscheck_zip"),
    path("settings/", views.app_settings, name="settings"),
    path("settings/clear-database/", views.clear_database, name="clear_database"),
    path("exports/", views.export_reports, name="export_reports"),
    path("templates/<str:template_type>/", views.download_template, name="download_template"),
]
