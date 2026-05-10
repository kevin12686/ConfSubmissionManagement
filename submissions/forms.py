from django import forms
from django.utils import timezone

from .models import AppSetting, FinalSubmission, InitialPaper
from .services.import_export import round_percent
from .services.text_utils import clean_note_text


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
            "notes",
        ]
        widgets = {
            "authors": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["acceptance_status"].label = "Accept Status"
        self._apply_bootstrap()

    def clean_notes(self):
        return clean_note_text(self.cleaned_data.get("notes"))


class FinalSubmissionForm(BootstrapMixin, forms.ModelForm):
    plagiarism_report_file = forms.FileField(
        required=False,
        label="Upload / replace plagiarism report",
        help_text="PDF only. Leave blank to keep the existing report.",
    )

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
            "title_author_review_status",
            "duplicate_author_review_status",
            "duplicate_author_review_notes",
            "extracted_title_match_message",
            "extracted_title_verified",
            "similarity_score",
            "single_similarity_score",
            "processing_message",
            "excluded_from_publication",
            "publication_exclusion_reason",
            "publication_exclusion_notes",
        ]
        widgets = {
            "upload_date": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "final_submission_authors": forms.Textarea(attrs={"rows": 3}),
            "extracted_authors": forms.Textarea(attrs={"rows": 3}),
            "title_author_extraction_message": forms.Textarea(attrs={"rows": 2}),
            "duplicate_author_review_notes": forms.Textarea(attrs={"rows": 2}),
            "extracted_title_match_message": forms.Textarea(attrs={"rows": 2}),
            "processing_message": forms.Textarea(attrs={"rows": 2}),
            "publication_exclusion_notes": forms.Textarea(attrs={"rows": 2}),
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paper_id_filled"].label = "Official Paper ID"
        self.fields["start2_paper_id_raw"].label = "Author-entered ID"
        self.fields["final_submission_title"].label = "Final Title"
        self.fields["final_submission_authors"].label = "Final Authors"
        self.fields["title_author_extraction_message"].label = "Title/Author Extraction Message"
        self.fields["title_author_review_status"].label = "Title/Author Review Status"
        self.fields["duplicate_author_review_status"].label = "Duplicate Author Review"
        self.fields["duplicate_author_review_notes"].label = "Duplicate Author Notes"
        self.fields["extracted_title_match_message"].label = "Extracted Title Match Message"
        self.fields["extracted_title_verified"].label = "Extracted Title Matched"
        self.fields["similarity_score"].label = "Plagiarism %"
        self.fields["single_similarity_score"].label = "Single %"
        self.fields["similarity_score"].widget.attrs.update({"step": "1", "min": "0"})
        self.fields["single_similarity_score"].widget.attrs.update({"step": "1", "min": "0"})
        if self.instance and self.instance.pk:
            if self.instance.similarity_score is not None:
                self.initial["similarity_score"] = int(self.instance.similarity_score)
            if self.instance.single_similarity_score is not None:
                self.initial["single_similarity_score"] = int(self.instance.single_similarity_score)
        self.fields["excluded_from_publication"].label = "Not Publishing"
        self.fields["publication_exclusion_reason"].label = "Not Publishing Reason"
        self.fields["publication_exclusion_notes"].label = "Not Publishing Notes"
        self._apply_bootstrap()

    def clean_plagiarism_report_file(self):
        report_file = self.cleaned_data.get("plagiarism_report_file")
        if not report_file:
            return report_file
        if not report_file.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Plagiarism report must be a PDF file.")
        content_type = getattr(report_file, "content_type", "")
        if content_type and content_type not in {"application/pdf", "application/octet-stream"}:
            raise forms.ValidationError("Plagiarism report must be a PDF file.")
        return report_file

    def clean_similarity_score(self):
        return round_percent(self.cleaned_data.get("similarity_score"))

    def clean_single_similarity_score(self):
        return round_percent(self.cleaned_data.get("single_similarity_score"))


class EditorUploadForm(BootstrapMixin, forms.Form):
    paper = forms.ModelChoiceField(
        queryset=InitialPaper.objects.all(),
        label="Paper ID",
        help_text="Editor upload must be linked to a Paper Master List record.",
    )
    pdf_file = forms.FileField(label="Editor PDF")
    source_file = forms.FileField(required=False, label="Editor source file")
    final_submission_title = forms.CharField(required=False, label="Final Title")
    final_submission_authors = forms.CharField(
        required=False,
        label="Final Authors",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    notes = forms.CharField(
        required=True,
        label="Editor upload note",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Required. Record why this editor-uploaded version should be used.",
    )

    def __init__(self, *args, initial_paper_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paper"].queryset = InitialPaper.objects.all().order_by("paper_id")
        if initial_paper_id:
            self.fields["paper"].initial = InitialPaper.objects.filter(
                paper_id=initial_paper_id
            ).first()
        self._apply_bootstrap()


class ImportFileForm(BootstrapMixin, forms.Form):
    file = forms.FileField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class SystemStateRestoreForm(BootstrapMixin, forms.Form):
    snapshot = forms.FileField(
        label="System state ZIP",
        help_text="Upload a ZIP created by Download System State ZIP.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_snapshot(self):
        snapshot = self.cleaned_data.get("snapshot")
        if snapshot and not snapshot.name.lower().endswith(".zip"):
            raise forms.ValidationError("System state snapshot must be a ZIP file.")
        return snapshot


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


def default_crosscheck_token(date_value=None):
    date_value = date_value or timezone.localdate()
    return f"{date_value.strftime('%b').upper()}{date_value.day:02d}{date_value.year}_1"


class CrossCheckExportForm(BootstrapMixin, forms.Form):
    token = forms.CharField(
        label="Batch token",
        max_length=80,
        initial=default_crosscheck_token,
        help_text="Default format is MONDDYYYY_1, such as MAY102026_1. Used in filenames like PaperID_token.pdf.",
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
            "conference_name",
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
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conference_name"].label = "Conference name"
        self.fields["conference_name"].help_text = (
            "Shown in the top-right navbar and included in system state backups."
        )
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
        folder_help = "Use relative paths like data/reports unless you intentionally need an absolute folder."
        for field_name in [
            "incoming_folder",
            "active_final_folder",
            "old_versions_folder",
            "reports_folder",
            "extraction_results_folder",
            "plagiarism_reports_folder",
        ]:
            self.fields[field_name].help_text = folder_help
        self.fields["plagiarism_percent_threshold"].label = "Plagiarism % threshold"
        self.fields["single_similarity_threshold"].label = "Single % threshold"
        self._apply_bootstrap()
