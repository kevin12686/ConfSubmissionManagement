from django import forms

from .models import AppSetting, FinalSubmission, InitialPaper


class BootstrapMixin:
    def _apply_bootstrap(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class InitialPaperForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = InitialPaper
        fields = [
            "paper_id",
            "acceptance_status",
            "title",
            "authors",
        ]
        widgets = {"authors": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["acceptance_status"].label = "Accept Status"
        self._apply_bootstrap()


class FinalSubmissionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = FinalSubmission
        fields = [
            "final_submission_id",
            "start2_paper_id_raw",
            "paper_id_filled",
            "final_submission_title",
            "final_submission_authors",
            "upload_date",
            "pdf_file",
            "source_file",
            "extracted_title",
            "extracted_authors",
            "title_author_source",
            "title_author_extraction_message",
            "title_author_verified",
            "extracted_title_match_message",
            "extracted_title_verified",
            "similarity_score",
            "single_similarity_score",
            "plagiarism_report_path",
            "processing_message",
        ]
        widgets = {
            "upload_date": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "final_submission_authors": forms.Textarea(attrs={"rows": 3}),
            "extracted_authors": forms.Textarea(attrs={"rows": 3}),
            "title_author_extraction_message": forms.Textarea(attrs={"rows": 2}),
            "extracted_title_match_message": forms.Textarea(attrs={"rows": 2}),
            "processing_message": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paper_id_filled"].label = "Official Paper ID"
        self.fields["start2_paper_id_raw"].label = "Author-entered ID"
        self.fields["final_submission_title"].label = "Final Title"
        self.fields["final_submission_authors"].label = "Final Authors"
        self.fields["title_author_extraction_message"].label = "Title/Author Extraction Message"
        self.fields["title_author_verified"].label = "Title/Author Reviewed"
        self.fields["extracted_title_match_message"].label = "Extracted Title Match Message"
        self.fields["extracted_title_verified"].label = "Extracted Title Matched"
        self.fields["similarity_score"].label = "Plagiarism %"
        self.fields["single_similarity_score"].label = "Single %"
        self._apply_bootstrap()


class ImportFileForm(BootstrapMixin, forms.Form):
    file = forms.FileField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]
        return [single_clean(data, initial)] if data else []


class CrossCheckExportForm(BootstrapMixin, forms.Form):
    token = forms.CharField(
        label="Batch token",
        max_length=80,
        initial="MAY082026",
        help_text="Used in filenames like PaperID_token.pdf. Use letters, numbers, underscore, or hyphen.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class CrossCheckReportUploadForm(BootstrapMixin, forms.Form):
    report_files = MultipleFileField(required=False, label="CrossCheck report PDFs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class FinalSubmissionImportForm(BootstrapMixin, forms.Form):
    file = forms.FileField(label="Metadata CSV/XLSX")
    submission_files = MultipleFileField(required=False, label="PDF and Source files")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["submission_files"].help_text = (
            "Optional. Upload files named like 34_file_Submit_PDF.pdf and 34_file_Submit_Source.docx/zip/tex. "
            "The system uses file extension to correct swapped PDF/source uploads."
        )
        self._apply_bootstrap()


class FormattingUploadForm(BootstrapMixin, forms.Form):
    corrected_pdf = forms.FileField(required=False, label="Corrected PDF")
    corrected_source = forms.FileField(required=False, label="Corrected Source")
    format_status = forms.ChoiceField(
        choices=[
            ("pending", "Pending"),
            ("needs_edit", "Needs edit"),
            ("review_ok", "Review OK"),
        ],
        widget=forms.RadioSelect,
        required=True,
    )
    format_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, submission=None, **kwargs):
        super().__init__(*args, **kwargs)
        if submission:
            self.fields["format_status"].initial = submission.format_status
            self.fields["format_notes"].initial = submission.format_notes
        self._apply_bootstrap()


class AppSettingForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = AppSetting
        fields = [
            "page_minimum",
            "page_limit",
            "author_paper_limit",
            "max_authors_per_paper",
            "title_words_for_filename",
            "active_version_rule",
            "time_zone",
            "incoming_folder",
            "active_final_folder",
            "old_versions_folder",
            "reports_folder",
            "extraction_results_folder",
            "plagiarism_reports_folder",
            "plagiarism_percent_threshold",
            "single_similarity_threshold",
            "title_author_script_path",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["page_minimum"].label = "Page minimum"
        self.fields["page_limit"].label = "Page limit"
        self.fields["max_authors_per_paper"].label = "Max authors per paper"
        self.fields["active_version_rule"].label = "Active final version rule"
        self.fields["active_version_rule"].help_text = (
            "Choose whether the current final version is selected by largest Final ID "
            "or by latest upload date."
        )
        self.fields["time_zone"].label = "Time zone"
        self.fields["time_zone"].help_text = "Default is Dallas / Central Time."
        self.fields["plagiarism_percent_threshold"].label = "Plagiarism % threshold"
        self.fields["single_similarity_threshold"].label = "Single % threshold"
        self.fields["title_author_script_path"].label = "Title/author extraction script path"
        self._apply_bootstrap()
