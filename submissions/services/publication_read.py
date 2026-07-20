import hashlib
import json
from dataclasses import dataclass
from functools import cached_property

from django.db.models import Count, Q

from submissions.models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    InitialPaper,
)
from submissions.services.file_inspection import FileInspectionContext


class PublicationStateChangedDuringExport(RuntimeError):
    pass


def _model_snapshot(model):
    fields = [field.attname for field in model._meta.concrete_fields]
    return list(model.objects.order_by("pk").values_list(*fields))


def publication_database_signature():
    payload = [
        (model._meta.label_lower, _model_snapshot(model))
        for model in (
            AppSetting,
            InitialPaper,
            FinalSubmission,
            AuthorLimitWaiver,
        )
    ]
    encoded = json.dumps(
        payload,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PublicationReadContext:
    settings: AppSetting
    papers: tuple
    active_submissions: tuple
    file_inspection: FileInspectionContext
    database_signature: str | None = None

    @classmethod
    def load(cls, *, require_stable_database=False):
        AppSetting.load()
        attempts = 2 if require_stable_database else 1
        for _attempt in range(attempts):
            before = publication_database_signature() if require_stable_database else None
            context = cls(
                settings=AppSetting.objects.get(pk=1),
                papers=tuple(InitialPaper.objects.all()),
                active_submissions=tuple(
                    FinalSubmission.objects.filter(
                        active_version=True,
                        discarded=False,
                    )
                ),
                file_inspection=FileInspectionContext(),
                database_signature=before,
            )
            if not require_stable_database:
                return context
            after = publication_database_signature()
            if before == after:
                return context
        raise PublicationStateChangedDuringExport(
            "Publication workflow state changed while export data was being loaded."
        )

    def assert_database_unchanged(self):
        if (
            self.database_signature is not None
            and publication_database_signature() != self.database_signature
        ):
            raise PublicationStateChangedDuringExport(
                "Publication workflow state changed during export. "
                "No final package was retained; review readiness and export again."
            )

    @cached_property
    def valid_paper_ids(self):
        return {paper.paper_id for paper in self.papers}

    @cached_property
    def paper_by_id(self):
        return {paper.paper_id: paper for paper in self.papers}

    @cached_property
    def publishable_submissions(self):
        return tuple(
            submission
            for submission in self.active_submissions
            if not submission.excluded_from_publication
        )

    @cached_property
    def master_submissions(self):
        valid_ids = self.valid_paper_ids
        return tuple(
            submission
            for submission in self.publishable_submissions
            if submission.paper_id_filled in valid_ids
        )

    @cached_property
    def unmatched_submissions(self):
        valid_ids = self.valid_paper_ids
        return tuple(
            submission
            for submission in self.publishable_submissions
            if submission.paper_id_filled not in valid_ids
        )

    @cached_property
    def excluded_paper_ids(self):
        valid_ids = self.valid_paper_ids
        active_by_paper = {}
        for submission in self.active_submissions:
            if submission.paper_id_filled in valid_ids:
                active_by_paper.setdefault(submission.paper_id_filled, []).append(
                    submission
                )
        return {
            paper_id
            for paper_id, submissions in active_by_paper.items()
            if submissions
            and all(
                submission.excluded_from_publication
                for submission in submissions
            )
        }

    @cached_property
    def mixed_publication_decision_groups(self):
        candidates = FinalSubmission.objects.filter(discarded=False).exclude(
            paper_id_filled=""
        )
        mixed_paper_ids = list(
            candidates.values("paper_id_filled")
            .annotate(
                excluded_count=Count(
                    "pk",
                    filter=Q(excluded_from_publication=True),
                ),
                included_count=Count(
                    "pk",
                    filter=Q(excluded_from_publication=False),
                ),
            )
            .filter(excluded_count__gt=0, included_count__gt=0)
            .values_list("paper_id_filled", flat=True)
        )
        if not mixed_paper_ids:
            return {}

        grouped = {}
        rows = (
            candidates.filter(paper_id_filled__in=mixed_paper_ids)
            .order_by("paper_id_filled", "final_submission_id")
            .values_list(
                "paper_id_filled",
                "final_submission_id",
                "excluded_from_publication",
            )
        )
        for paper_id, final_id, excluded in rows:
            group = grouped.setdefault(
                paper_id,
                {"excluded": [], "included": []},
            )
            group["excluded" if excluded else "included"].append(final_id)
        return grouped
