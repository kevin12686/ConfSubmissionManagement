from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from submissions.models import FinalSubmission
from submissions.services.formatting_history import capture_existing_formatting


class Command(BaseCommand):
    help = "Capture current corrected files as formatting baselines without changing review state."

    def handle(self, *args, **options):
        ids = FinalSubmission.objects.filter(
            (Q(formatted_pdf_file__isnull=False) & ~Q(formatted_pdf_file=""))
            | (Q(formatted_source_file__isnull=False) & ~Q(formatted_source_file=""))
        ).values_list("pk", flat=True).iterator()
        captured = 0
        errors = []
        for submission_id in ids:
            try:
                if capture_existing_formatting(submission_id):
                    captured += 1
            except Exception as exc:
                errors.append(str(submission_id))
                self.stderr.write(f"Submission {submission_id}: {exc}")
        self.stdout.write(f"Captured {captured} formatting baseline(s).")
        if errors:
            raise CommandError("Baseline capture failed for submissions: " + ", ".join(errors))
