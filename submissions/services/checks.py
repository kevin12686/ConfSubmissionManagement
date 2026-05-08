import re
import string
from collections import defaultdict

from django.db.models import Count, Q

from submissions.models import AppSetting, FinalSubmission, InitialPaper, PaperAuthor
from submissions.services.file_manager import (
    corrected_pdf_needs_processing,
    publication_pdf_info,
    publication_source_info,
)


ERROR_GROUPS = {
    "Paper ID / Mapping": {
        "level": "danger",
        "categories": {
            "Invalid Paper ID",
            "Unverified Paper ID",
            "Final Title / Paper Master Title Mismatch",
            "Missing Final Submission",
            "Replaced Final Submission",
        },
    },
    "Files / PDF Processing": {
        "level": "warning",
        "categories": {
            "Missing PDF",
            "Corrected PDF Not Processed",
            "Missing Source File",
            "Page Limit Exceeded",
            "Below Page Minimum",
            "PDF Processing Error",
        },
    },
    "Title / Author": {
        "level": "warning",
        "categories": {
            "Missing Extracted Title",
            "Missing Extracted Authors",
            "Unverified Title/Author Extraction",
            "Unverified Extracted Title Match",
            "Author Over Limit",
        },
    },
    "Formatting": {
        "level": "warning",
        "categories": {"Formatting Not Review OK"},
    },
    "Plagiarism": {
        "level": "warning",
        "categories": {
            "Missing Plagiarism Result",
            "Plagiarism % Over Threshold",
            "Single % Over Threshold",
        },
    },
}

ERROR_GROUP_ORDER = [
    "Paper ID / Mapping",
    "Files / PDF Processing",
    "Title / Author",
    "Formatting",
    "Plagiarism",
    "Other",
]


def _error_group_for_category(category):
    for group_name, config in ERROR_GROUPS.items():
        if category in config["categories"]:
            return group_name
    return "Other"


def _annotate_error_rows(rows):
    for row in rows:
        group = _error_group_for_category(row["category"])
        row["group"] = group
        row["level"] = ERROR_GROUPS.get(group, {"level": "secondary"})["level"]
    return rows


def error_report_sections(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("group") or _error_group_for_category(row["category"])].append(row)
    sections = []
    for group in ERROR_GROUP_ORDER:
        group_rows = grouped.get(group, [])
        if not group_rows:
            continue
        sections.append(
            {
                "group": group,
                "level": ERROR_GROUPS.get(group, {"level": "secondary"})["level"],
                "rows": group_rows,
                "count": len(group_rows),
            }
        )
    return sections


def build_paper_id(initial_submission_id, track):
    if not track:
        return str(initial_submission_id or "").strip()
    track_part = re.sub(r"[^A-Za-z0-9]+", "", str(track or "").upper()) or "TRACK"
    submission_part = re.sub(r"[^A-Za-z0-9]+", "", str(initial_submission_id or ""))
    return f"{track_part}-{submission_part}"


def clean_identifier(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def canonical_paper_id_key(value):
    raw = clean_identifier(value).upper()
    raw = re.sub(r"[\s_-]+", "", raw)
    match = re.match(r"^([A-Z]+)(\d+)$", raw)
    if match:
        return f"{match.group(1)}:{int(match.group(2))}"
    return raw


def resolve_official_paper_id(raw_paper_id):
    raw = clean_identifier(raw_paper_id)
    if not raw:
        return ""

    exact = InitialPaper.objects.filter(paper_id__iexact=raw).first()
    if exact:
        return exact.paper_id

    key = canonical_paper_id_key(raw)
    matches = [
        paper.paper_id
        for paper in InitialPaper.objects.all()
        if canonical_paper_id_key(paper.paper_id) == key
    ]
    if len(matches) == 1:
        return matches[0]
    return raw


def validate_paper_id(final_submission):
    if not final_submission.paper_id_filled:
        return False
    return InitialPaper.objects.filter(paper_id=final_submission.paper_id_filled).exists()


def normalize_author_name(name):
    translator = str.maketrans("", "", string.punctuation)
    normalized = (name or "").lower().translate(translator)
    return re.sub(r"\s+", " ", normalized).strip()


def split_authors(raw_authors):
    if not raw_authors:
        return []
    text = re.sub(r"\s+\band\b\s+", ";", raw_authors, flags=re.IGNORECASE)
    if ";" in text:
        parts = text.split(";")
    else:
        parts = text.split(",")
    return [part.strip() for part in parts if part.strip()]


def rebuild_paper_authors():
    PaperAuthor.objects.all().delete()
    rows = []
    for submission in FinalSubmission.objects.filter(active_version=True).exclude(
        extracted_authors=""
    ):
        for index, author_name in enumerate(split_authors(submission.extracted_authors), start=1):
            normalized = normalize_author_name(author_name)
            if normalized:
                rows.append(
                    PaperAuthor(
                        final_submission=submission,
                        paper_id=submission.paper_id_filled,
                        author_name=author_name,
                        normalized_author_name=normalized,
                        author_order=index,
                    )
                )
    PaperAuthor.objects.bulk_create(rows)
    return len(rows)


def author_count_rows():
    rebuild_paper_authors()
    setting = AppSetting.load()
    grouped = (
        PaperAuthor.objects.values("normalized_author_name")
        .annotate(paper_count=Count("paper_id", distinct=True))
        .order_by("-paper_count", "normalized_author_name")
    )
    rows = []
    for item in grouped:
        authors = PaperAuthor.objects.filter(
            normalized_author_name=item["normalized_author_name"]
        ).order_by("author_order", "author_name")
        display_name = authors.first().author_name if authors.exists() else ""
        paper_ids = sorted({author.paper_id for author in authors if author.paper_id})
        over_limit = item["paper_count"] > setting.author_paper_limit
        rows.append(
            {
                "normalized_author_name": item["normalized_author_name"],
                "display_author_name": display_name,
                "paper_count": item["paper_count"],
                "paper_ids": ", ".join(paper_ids),
                "status": "Over limit" if over_limit else "OK",
                "over_limit": over_limit,
            }
        )
    return rows


def invalid_paper_id_submissions():
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))
    return [
        submission
        for submission in FinalSubmission.objects.all()
        if not submission.paper_id_filled or submission.paper_id_filled not in valid_ids
    ]


def dashboard_counts():
    setting = AppSetting.load()
    active = FinalSubmission.objects.filter(active_version=True)
    author_rows = author_count_rows()
    submitted_paper_ids = set(
        FinalSubmission.objects.exclude(paper_id_filled="").values_list(
            "paper_id_filled", flat=True
        )
    )
    return {
        "total_papers": InitialPaper.objects.count(),
        "total_final_submissions": FinalSubmission.objects.count(),
        "active_final_versions": active.count(),
        "unverified_paper_ids": active.filter(paper_id_verified=False).count(),
        "title_mismatches": active.filter(verification_status="title_mismatch").count(),
        "duplicate_final_submissions": FinalSubmission.objects.filter(
            duplicate_submission=True
        ).count(),
        "invalid_paper_ids": len(invalid_paper_id_submissions()),
        "missing_final_submissions": InitialPaper.objects.exclude(
            paper_id__in=submitted_paper_ids
        ).count(),
        "page_limit_errors": active.filter(page_count__gt=setting.page_limit).count(),
        "page_minimum_errors": active.filter(page_count__lt=setting.page_minimum).count(),
        "missing_pdfs": sum(
            1 for submission in active if not publication_pdf_info(submission)["exists"]
        ),
        "authors_over_limit": sum(1 for row in author_rows if row["over_limit"]),
        "missing_title_author_extraction": active.filter(extracted_title="").count()
        + active.filter(extracted_authors="").count(),
        "unverified_title_author_extraction": active.exclude(extracted_title="")
        .exclude(extracted_authors="")
        .filter(title_author_verified=False)
        .count(),
        "unverified_extracted_title_match": active.exclude(extracted_title="")
        .exclude(final_submission_title="")
        .filter(extracted_title_verified=False)
        .count(),
        "missing_plagiarism_result": active.filter(
            Q(similarity_score__isnull=True) | Q(single_similarity_score__isnull=True)
        ).count(),
    }


def error_report_rows():
    setting = AppSetting.load()
    rows = []
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))

    for submission in FinalSubmission.objects.all():
        label = f"{submission.final_submission_id} / {submission.paper_id_filled or 'No Paper ID'}"
        publication_pdf = publication_pdf_info(submission)
        publication_source = publication_source_info(submission)
        if not submission.paper_id_filled or submission.paper_id_filled not in valid_ids:
            rows.append(
                {
                    "category": "Invalid Paper ID",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Paper ID does not match the Paper Master List.",
                }
            )
        if submission.active_version and not submission.paper_id_verified:
            rows.append(
                {
                    "category": "Unverified Paper ID",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": submission.verification_message
                    or "Paper ID has not been manually verified.",
                }
            )
        if submission.active_version and submission.verification_status == "title_mismatch":
            rows.append(
                {
                    "category": "Final Title / Paper Master Title Mismatch",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": submission.verification_message,
                }
            )
        if submission.duplicate_submission:
            rows.append(
                {
                    "category": "Replaced Final Submission",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "A newer mapped final submission exists for this Paper ID.",
                }
            )
        if not publication_pdf["exists"]:
            rows.append(
                {
                    "category": "Missing PDF",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"No PDF file is attached for {label}.",
                }
            )
        if submission.active_version and corrected_pdf_needs_processing(submission):
            rows.append(
                {
                    "category": "Corrected PDF Not Processed",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "A corrected PDF exists but page count/hash/active-final copy need to be refreshed by running Process PDFs.",
                }
            )
        if submission.active_version and submission.format_status != "review_ok":
            rows.append(
                {
                    "category": "Formatting Not Review OK",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"Formatting status is {submission.get_format_status_display()}.",
                }
            )
        if (
            submission.active_version
            and not publication_source["exists"]
        ):
            rows.append(
                {
                    "category": "Missing Source File",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"No source file is attached for {label}.",
                }
            )
        if submission.page_count and submission.page_count > setting.page_limit:
            rows.append(
                {
                    "category": "Page Limit Exceeded",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"{submission.page_count} pages exceeds the limit of {setting.page_limit}.",
                }
            )
        if submission.page_count and submission.page_count < setting.page_minimum:
            rows.append(
                {
                    "category": "Below Page Minimum",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"{submission.page_count} pages is below the minimum of {setting.page_minimum}.",
                }
            )
        if submission.processing_status == "error":
            rows.append(
                {
                    "category": "PDF Processing Error",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": submission.processing_message,
                }
            )
        if submission.active_version and not submission.extracted_title:
            rows.append(
                {
                    "category": "Missing Extracted Title",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "No extracted title has been imported or entered.",
                }
            )
        if submission.active_version and not submission.extracted_authors:
            rows.append(
                {
                    "category": "Missing Extracted Authors",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "No extracted authors have been imported or entered.",
                }
            )
        if (
            submission.active_version
            and submission.extracted_title
            and submission.extracted_authors
            and not submission.title_author_verified
        ):
            rows.append(
                {
                    "category": "Unverified Title/Author Extraction",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Extracted title/authors have not been manually verified.",
                }
            )
        if (
            submission.active_version
            and submission.extracted_title
            and submission.final_submission_title
            and not submission.extracted_title_verified
        ):
            rows.append(
                {
                    "category": "Unverified Extracted Title Match",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": submission.extracted_title_match_message
                    or "Extracted title has not been verified against Final Submission Title.",
                }
            )
        if submission.active_version and (
            submission.similarity_score is None or submission.single_similarity_score is None
        ):
            rows.append(
                {
                    "category": "Missing Plagiarism Result",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Plagiarism % or Single % has not been imported.",
                }
            )
        if (
            submission.active_version
            and submission.similarity_score is not None
            and submission.similarity_score > setting.plagiarism_percent_threshold
        ):
            rows.append(
                {
                    "category": "Plagiarism % Over Threshold",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"{submission.similarity_score}% is over the threshold of {setting.plagiarism_percent_threshold}%.",
                }
            )
        if (
            submission.active_version
            and submission.single_similarity_score is not None
            and submission.single_similarity_score > setting.single_similarity_threshold
        ):
            rows.append(
                {
                    "category": "Single % Over Threshold",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"{submission.single_similarity_score}% is over the threshold of {setting.single_similarity_threshold}%.",
                }
            )

    for row in author_count_rows():
        if row["over_limit"]:
            rows.append(
                {
                    "category": "Author Over Limit",
                    "paper_id": row["paper_ids"],
                    "final_submission_id": "",
                    "message": f"{row['display_author_name']} has {row['paper_count']} active papers.",
                }
            )
    submitted_paper_ids = set(
        FinalSubmission.objects.exclude(paper_id_filled="").values_list(
            "paper_id_filled", flat=True
        )
    )
    for paper in InitialPaper.objects.exclude(paper_id__in=submitted_paper_ids):
        rows.append(
            {
                "category": "Missing Final Submission",
                "paper_id": paper.paper_id,
                "final_submission_id": "",
                "message": "This official Paper ID has no mapped final submission.",
            }
        )
    return rows
