import hashlib
import re
import string
from collections import defaultdict
from pathlib import Path

from django.db import transaction
from django.db.models import Count, Q

from submissions.models import (
    AppSetting,
    AuthorLimitWaiver,
    FinalSubmission,
    InitialPaper,
    PaperAuthor,
)
from submissions.services.file_manager import (
    active_pdf_needs_processing,
    corrected_pdf_needs_processing,
    pdf_available_for_processing,
    publication_pdf_info,
    publication_source_info,
)


ERROR_GROUPS = {
    "Paper ID / Mapping": {
        "level": "danger",
        "categories": {
            "Invalid Paper ID",
            "Unclassified Final Not In Master",
            "Unverified Paper ID",
            "Final Title / Paper Master Title Mismatch",
            "Missing Final Submission",
            "Replaced Final Submission",
            "Multiple Active Final Submissions",
            "Start2/Editor Version Conflict",
        },
    },
    "Files / PDF Processing": {
        "level": "warning",
        "categories": {
            "Missing PDF",
            "Corrected PDF Not Processed",
            "Missing Source File",
            "PDF Not Processed",
            "Page Limit Exceeded",
            "Below Page Minimum",
            "PDF Processing Error",
            "Allowed Page Exception",
        },
    },
    "Title / Author": {
        "level": "warning",
        "categories": {
            "Missing Extracted Title",
            "Missing Extracted Authors",
            "Title/Author Red Flag",
            "Unverified Title/Author Extraction",
            "Manual Title/Author Override",
            "Duplicate Author In Paper",
            "Author Over Limit",
            "Allowed Author Number Exception",
            "Allowed Author Paper Count Exception",
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
            "Stale Plagiarism Report",
            "Plagiarism % Over Threshold",
            "Single % Over Threshold",
            "Stale Plagiarism % Exception",
            "Stale Single % Exception",
            "Allowed Plagiarism % Exception",
            "Allowed Single % Exception",
        },
    },
    "Publication Duplicates": {
        "level": "danger",
        "categories": {
            "Duplicate Publication Title",
            "Duplicate Publication PDF",
            "Duplicate Publication Source",
        },
    },
    "Version Tracking": {
        "level": "info",
        "categories": {
            "Discarded Final Submission",
            "Not Publishing Final Submission",
        },
    },
}

ERROR_GROUP_ORDER = [
    "Paper ID / Mapping",
    "Files / PDF Processing",
    "Title / Author",
    "Formatting",
    "Plagiarism",
    "Publication Duplicates",
    "Version Tracking",
    "Other",
]

ERROR_SEVERITY_CONFIG = {
    "critical": {"label": "Critical", "level": "danger", "order": 0},
    "medium": {"label": "Medium", "level": "warning", "order": 1},
    "info": {"label": "Info", "level": "info", "order": 2},
}

ERROR_CATEGORY_SEVERITY = {
    "Invalid Paper ID": "critical",
    "Unclassified Final Not In Master": "critical",
    "Unverified Paper ID": "critical",
    "Missing Final Submission": "critical",
    "Multiple Active Final Submissions": "critical",
    "Start2/Editor Version Conflict": "critical",
    "Missing PDF": "critical",
    "Corrected PDF Not Processed": "critical",
    "Missing Source File": "critical",
    "PDF Not Processed": "critical",
    "Page Limit Exceeded": "critical",
    "Below Page Minimum": "critical",
    "PDF Processing Error": "critical",
    "Author Over Limit": "critical",
    "Duplicate Author In Paper": "critical",
    "Plagiarism % Over Threshold": "critical",
    "Single % Over Threshold": "critical",
    "Stale Plagiarism % Exception": "critical",
    "Stale Single % Exception": "critical",
    "Duplicate Publication Title": "critical",
    "Duplicate Publication PDF": "critical",
    "Duplicate Publication Source": "critical",
    "Final Title / Paper Master Title Mismatch": "medium",
    "Missing Extracted Title": "medium",
    "Missing Extracted Authors": "medium",
    "Title/Author Red Flag": "medium",
    "Unverified Title/Author Extraction": "medium",
    "Manual Title/Author Override": "info",
    "Formatting Not Review OK": "medium",
    "Missing Plagiarism Result": "medium",
    "Allowed Plagiarism % Exception": "info",
    "Allowed Single % Exception": "info",
    "Replaced Final Submission": "info",
    "Discarded Final Submission": "info",
    "Not Publishing Final Submission": "info",
    "Allowed Page Exception": "info",
    "Allowed Author Number Exception": "info",
    "Allowed Author Paper Count Exception": "info",
}

ERROR_REPORT_AREA_CATEGORIES = {
    "mapping": {
        "Invalid Paper ID",
        "Unclassified Final Not In Master",
        "Unverified Paper ID",
        "Final Title / Paper Master Title Mismatch",
        "Missing Final Submission",
        "Multiple Active Final Submissions",
        "Start2/Editor Version Conflict",
    },
    "files": {
        "Missing PDF",
        "Corrected PDF Not Processed",
        "Missing Source File",
        "PDF Not Processed",
        "Page Limit Exceeded",
        "Below Page Minimum",
        "PDF Processing Error",
        "Allowed Page Exception",
    },
    "authors": {
        "Duplicate Author In Paper",
        "Author Over Limit",
        "Allowed Author Number Exception",
        "Allowed Author Paper Count Exception",
    },
}

ERROR_REPORT_AREA_LABELS = {
    "mapping": "Paper mapping and version decisions",
    "files": "PDF, source, and page checks",
    "authors": "Author limits and duplicates",
}


def _error_group_for_category(category):
    for group_name, config in ERROR_GROUPS.items():
        if category in config["categories"]:
            return group_name
    return "Other"


def _whole_percent_label(value):
    if value is None:
        return ""
    return str(int(round(float(value))))


def _normalize_title_for_verification(value):
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _titles_match_for_mapping(left, right):
    left_normalized = _normalize_title_for_verification(left)
    right_normalized = _normalize_title_for_verification(right)
    return bool(left_normalized and right_normalized and left_normalized == right_normalized)


def paper_id_effectively_verified(submission, master_paper=None):
    if not submission or submission.excluded_from_publication:
        return False
    if submission.paper_id_verified:
        return True
    if submission.auto_verify_blocked:
        return False
    master_paper = master_paper or InitialPaper.objects.filter(
        paper_id=submission.paper_id_filled
    ).first()
    if not master_paper:
        return False
    final_title = _normalize_title_for_verification(submission.final_submission_title)
    master_title = _normalize_title_for_verification(master_paper.title)
    return bool(final_title and master_title and final_title == master_title)


def paper_title_matches_master(submission, master_paper=None):
    if not submission:
        return False
    master_paper = master_paper or InitialPaper.objects.filter(
        paper_id=submission.paper_id_filled
    ).first()
    if not master_paper:
        return False
    final_title = _normalize_title_for_verification(submission.final_submission_title)
    master_title = _normalize_title_for_verification(master_paper.title)
    return bool(final_title and master_title and final_title == master_title)


def page_count_out_of_range(submission, setting=None):
    if not submission or submission.page_count is None:
        return False
    setting = setting or AppSetting.load()
    return submission.page_count < setting.page_minimum or submission.page_count > setting.page_limit


def author_number_count(submission):
    if not submission or not submission.extracted_authors:
        return 0
    return len(split_authors(submission.extracted_authors))


def author_number_over_limit(submission, setting=None):
    setting = setting or AppSetting.load()
    return author_number_count(submission) > setting.max_authors_per_paper


def has_valid_author_number_exception(submission):
    count = author_number_count(submission)
    return bool(
        submission
        and submission.author_number_exception_approved
        and submission.author_number_exception_reason.strip()
        and submission.author_number_exception_author_count == count
    )


def reset_page_limit_exception(submission):
    submission.page_limit_exception_approved = False
    submission.page_limit_exception_reason = ""
    submission.page_limit_exception_page_count = None
    submission.page_limit_exception_approved_at = None


def reset_author_number_exception(submission):
    submission.author_number_exception_approved = False
    submission.author_number_exception_reason = ""
    submission.author_number_exception_author_count = None
    submission.author_number_exception_approved_at = None


def plagiarism_percent_over_threshold(submission, setting=None):
    if not submission or submission.similarity_score is None:
        return False
    setting = setting or AppSetting.load()
    return submission.similarity_score > setting.plagiarism_percent_threshold


def single_percent_over_threshold(submission, setting=None):
    if not submission or submission.single_similarity_score is None:
        return False
    setting = setting or AppSetting.load()
    return submission.single_similarity_score > setting.single_similarity_threshold


def has_valid_plagiarism_percent_exception(submission, setting=None):
    return bool(
        submission
        and plagiarism_percent_over_threshold(submission, setting)
        and submission.plagiarism_percent_exception_approved
        and submission.plagiarism_percent_exception_reason.strip()
        and submission.plagiarism_percent_exception_approved_score == submission.similarity_score
    )


def has_valid_single_percent_exception(submission, setting=None):
    return bool(
        submission
        and single_percent_over_threshold(submission, setting)
        and submission.single_percent_exception_approved
        and submission.single_percent_exception_reason.strip()
        and submission.single_percent_exception_approved_score == submission.single_similarity_score
    )


def reset_plagiarism_percent_exception(submission):
    submission.plagiarism_percent_exception_approved = False
    submission.plagiarism_percent_exception_reason = ""
    submission.plagiarism_percent_exception_approved_score = None
    submission.plagiarism_percent_exception_approved_at = None


def reset_single_percent_exception(submission):
    submission.single_percent_exception_approved = False
    submission.single_percent_exception_reason = ""
    submission.single_percent_exception_approved_score = None
    submission.single_percent_exception_approved_at = None


def reset_plagiarism_exceptions(submission):
    reset_plagiarism_percent_exception(submission)
    reset_single_percent_exception(submission)


def _error_severity_for_category(category):
    return ERROR_CATEGORY_SEVERITY.get(category, "medium")


def _annotate_error_rows(rows):
    for row in rows:
        group = _error_group_for_category(row["category"])
        severity = _error_severity_for_category(row["category"])
        row["group"] = group
        row["level"] = ERROR_GROUPS.get(group, {"level": "secondary"})["level"]
        row["severity"] = severity
        row["severity_label"] = ERROR_SEVERITY_CONFIG[severity]["label"]
        row["severity_level"] = ERROR_SEVERITY_CONFIG[severity]["level"]
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


def error_report_severity_sections(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("severity") or _error_severity_for_category(row["category"])].append(row)

    sections = []
    for severity, config in sorted(
        ERROR_SEVERITY_CONFIG.items(), key=lambda item: item[1]["order"]
    ):
        severity_rows = grouped.get(severity, [])
        grouped_by_workflow = defaultdict(list)
        for row in severity_rows:
            grouped_by_workflow[row.get("group") or _error_group_for_category(row["category"])].append(row)
        workflow_groups = []
        for group in ERROR_GROUP_ORDER:
            group_rows = grouped_by_workflow.get(group, [])
            if group_rows:
                workflow_groups.append(
                    {
                        "group": group,
                        "level": ERROR_GROUPS.get(group, {"level": "secondary"})["level"],
                        "rows": group_rows,
                        "count": len(group_rows),
                    }
                )
        sections.append(
            {
                "severity": severity,
                "label": config["label"],
                "level": config["level"],
                "rows": severity_rows,
                "groups": workflow_groups,
                "count": len(severity_rows),
            }
        )
    return sections


def filter_error_report_rows(rows, area=""):
    categories = ERROR_REPORT_AREA_CATEGORIES.get(area)
    if not categories:
        return rows, "", ""
    return (
        [row for row in rows if row["category"] in categories],
        area,
        ERROR_REPORT_AREA_LABELS[area],
    )


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


def _normalize_publication_title(value):
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _submission_label(submission):
    return f"{submission.paper_id_filled or 'No Paper ID'} / Final {submission.final_submission_id}"


def _has_any_pdf_file(submission):
    return bool(
        (submission.formatted_pdf_file and Path(submission.formatted_pdf_file.path).exists())
        or (submission.pdf_file and Path(submission.pdf_file.path).exists())
    )


def publication_duplicate_groups():
    title_groups = defaultdict(list)
    pdf_groups = defaultdict(list)
    source_groups = defaultdict(list)

    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))
    for submission in FinalSubmission.objects.filter(
        active_version=True,
        excluded_from_publication=False,
        discarded=False,
        paper_id_filled__in=valid_ids,
    ):
        title_key = _normalize_publication_title(submission.extracted_title)
        if title_key:
            title_groups[title_key].append(submission)

        pdf_info = publication_pdf_info(submission)
        if pdf_info["exists"]:
            pdf_groups[_file_hash(pdf_info["path"])].append(submission)

        source_info = publication_source_info(submission)
        if source_info["exists"]:
            source_groups[_file_hash(source_info["path"])].append(submission)

    groups = []
    for kind, category, label, source in [
        ("title", "Duplicate Publication Title", "Duplicate title", title_groups),
        ("pdf", "Duplicate Publication PDF", "Duplicate PDF", pdf_groups),
        ("source", "Duplicate Publication Source", "Duplicate source", source_groups),
    ]:
        for key, submissions in source.items():
            if len(submissions) > 1:
                groups.append(
                    {
                        "kind": kind,
                        "category": category,
                        "label": label,
                        "key": key,
                        "key_summary": key[:12],
                        "submissions": submissions,
                    }
                )
    return groups


def publication_duplicate_rows():
    rows = []
    for group in publication_duplicate_groups():
        labels = [_submission_label(submission) for submission in group["submissions"]]
        for index, submission in enumerate(group["submissions"]):
            other_labels = labels[:index] + labels[index + 1 :]
            rows.append(
                {
                    "category": group["category"],
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": (
                        f"{group['label']} with {', '.join(other_labels)}. "
                        f"Key: {group['key_summary']}."
                    ),
                }
            )
    return rows


def publication_duplicate_map():
    duplicates = defaultdict(list)
    for group in publication_duplicate_groups():
        for submission in group["submissions"]:
            duplicates[submission.pk].append(group["label"])
    return duplicates


def active_master_submission_map():
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))
    submissions = FinalSubmission.objects.filter(
        active_version=True,
        excluded_from_publication=False,
        discarded=False,
        paper_id_filled__in=valid_ids,
    )
    return {submission.paper_id_filled: submission for submission in submissions}


def publication_readiness_rows(include_allowed=False):
    setting = AppSetting.load()
    rows = []
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))
    active_valid_submissions = FinalSubmission.objects.filter(
        active_version=True,
        excluded_from_publication=False,
        discarded=False,
        paper_id_filled__in=valid_ids,
    )
    duplicate_active_groups = (
        active_valid_submissions.values("paper_id_filled")
        .annotate(active_count=Count("id"))
        .filter(active_count__gt=1)
    )
    for group in duplicate_active_groups:
        finals = ", ".join(
            active_valid_submissions.filter(paper_id_filled=group["paper_id_filled"])
            .order_by("final_submission_id")
            .values_list("final_submission_id", flat=True)
        )
        rows.append(
            {
                "category": "Multiple Active Final Submissions",
                "paper_id": group["paper_id_filled"],
                "final_submission_id": finals,
                "message": (
                    "More than one active final submission exists for this Paper ID. "
                    "Recalculate active versions before publication."
                ),
            }
        )
    active_by_paper = active_master_submission_map()
    excluded_paper_ids = set(
        FinalSubmission.objects.filter(
            active_version=True,
            excluded_from_publication=True,
            discarded=False,
            paper_id_filled__in=valid_ids,
        ).values_list("paper_id_filled", flat=True)
    )

    from submissions.services.editor_uploads import editor_conflict_details

    for conflict in editor_conflict_details():
        rows.append(
            {
                "category": "Start2/Editor Version Conflict",
                "paper_id": conflict["paper_id"],
                "final_submission_id": conflict["editor_ids"],
                "message": (
                    "Both Start2 and Editor-uploaded versions are undiscarded. "
                    "Editor upload is currently prioritized; discard one side before final export. "
                    f"Start2: {conflict['start2_ids']}; Editor: {conflict['editor_ids']}."
                ),
            }
        )

    for submission in (
        FinalSubmission.objects.filter(
            active_version=True,
            excluded_from_publication=False,
            discarded=False,
        )
        .exclude(paper_id_filled__in=valid_ids)
    ):
        rows.append(
            {
                "category": "Unclassified Final Not In Master",
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "message": "Final submission is not in the Paper Master List. Correct the Paper ID or mark it as Not Publishing.",
            }
        )

    for paper in InitialPaper.objects.all():
        if paper.paper_id in excluded_paper_ids:
            continue
        submission = active_by_paper.get(paper.paper_id)
        if not submission:
            rows.append(
                {
                    "category": "Missing Final Submission",
                    "paper_id": paper.paper_id,
                    "final_submission_id": "",
                    "message": "This Paper Master record has no active final submission.",
                }
            )
            continue

        label = f"{submission.final_submission_id} / {submission.paper_id_filled or 'No Paper ID'}"
        publication_pdf = publication_pdf_info(submission)
        publication_source = publication_source_info(submission)

        paper_id_verified = paper_id_effectively_verified(submission, paper)
        if not paper_id_verified:
            rows.append(
                {
                    "category": "Unverified Paper ID",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": submission.verification_message
                    or "Paper ID has not been manually verified.",
                }
            )
        if not paper_title_matches_master(submission, paper) and not submission.paper_id_verified:
            rows.append(
                {
                    "category": "Final Title / Paper Master Title Mismatch",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": (
                        submission.verification_message
                        or "Final Submission title does not match the Paper Master title."
                    ),
                }
            )
        if not _has_any_pdf_file(submission):
            rows.append(
                {
                    "category": "Missing PDF",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"No PDF file is attached for {label}.",
                }
            )
        elif active_pdf_needs_processing(submission):
            rows.append(
                {
                    "category": "PDF Not Processed",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "PDF page count/hash are not ready. Run Process PDFs before publishing.",
                }
            )
        if (
            submission.formatted_pdf_file
            and Path(submission.formatted_pdf_file.path).exists()
            and corrected_pdf_needs_processing(submission)
        ):
            rows.append(
                {
                    "category": "Corrected PDF Not Processed",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "A corrected PDF exists but page count/hash need to be refreshed by running Process PDFs.",
                }
            )
        if not publication_source["exists"]:
            rows.append(
                {
                    "category": "Missing Source File",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"No source file is attached for {label}.",
                }
            )
        if submission.page_count and submission.page_count > setting.page_limit:
            if submission.has_valid_page_limit_exception:
                if include_allowed:
                    rows.append(
                        {
                            "category": "Allowed Page Exception",
                            "paper_id": submission.paper_id_filled,
                            "final_submission_id": submission.final_submission_id,
                            "message": (
                                f"{submission.page_count} pages exceeds the limit of "
                                f"{setting.page_limit}. Allowed: {submission.page_limit_exception_reason}"
                            ),
                        }
                    )
            else:
                rows.append(
                    {
                        "category": "Page Limit Exceeded",
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "message": f"{submission.page_count} pages exceeds the limit of {setting.page_limit}.",
                    }
                )
        if submission.page_count and submission.page_count < setting.page_minimum:
            if submission.has_valid_page_limit_exception:
                if include_allowed:
                    rows.append(
                        {
                            "category": "Allowed Page Exception",
                            "paper_id": submission.paper_id_filled,
                            "final_submission_id": submission.final_submission_id,
                            "message": (
                                f"{submission.page_count} pages is below the minimum of "
                                f"{setting.page_minimum}. Allowed: {submission.page_limit_exception_reason}"
                            ),
                        }
                    )
            else:
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
        if not submission.extracted_title:
            rows.append(
                {
                    "category": "Missing Extracted Title",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "No extracted title has been imported or entered.",
                }
            )
        if not submission.extracted_authors:
            rows.append(
                {
                    "category": "Missing Extracted Authors",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "No extracted authors have been imported or entered.",
                }
            )
        author_count = author_number_count(submission)
        if author_count > setting.max_authors_per_paper:
            if has_valid_author_number_exception(submission):
                if include_allowed:
                    rows.append(
                        {
                            "category": "Allowed Author Number Exception",
                            "paper_id": submission.paper_id_filled,
                            "final_submission_id": submission.final_submission_id,
                            "message": (
                                f"{author_count} authors exceeds the per-paper limit of "
                                f"{setting.max_authors_per_paper}. Allowed: "
                                f"{submission.author_number_exception_reason}"
                            ),
                        }
                    )
            else:
                rows.append(
                    {
                        "category": "Author Over Limit",
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "message": (
                            f"{author_count} authors exceeds the per-paper limit of "
                            f"{setting.max_authors_per_paper}."
                        ),
                    }
                )
        duplicate_authors = duplicate_authors_in_paper(submission.extracted_authors)
        if duplicate_authors and submission.duplicate_author_review_status != "review_ok":
            labels = [
                f"{', '.join(item['display_names'])} ({item['count']} times)"
                for item in duplicate_authors
            ]
            rows.append(
                {
                    "category": "Duplicate Author In Paper",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Duplicate author names need review: " + "; ".join(labels),
                }
            )
        if submission.title_author_review_status == "red_flag":
            rows.append(
                {
                    "category": "Title/Author Red Flag",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Title/author extraction is marked Red Flag; fix formatting or re-extract before publication.",
                }
            )
        elif (
            submission.extracted_title
            and submission.extracted_authors
            and submission.title_author_review_status != "review_ok"
        ):
            rows.append(
                {
                    "category": "Unverified Title/Author Extraction",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Extracted title/authors have not been manually verified.",
                }
            )
        if submission.format_status != "review_ok":
            rows.append(
                {
                    "category": "Formatting Not Review OK",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": f"Formatting status is {submission.get_format_status_display()}.",
                }
            )
        if submission.similarity_score is None or submission.single_similarity_score is None:
            rows.append(
                {
                    "category": "Missing Plagiarism Result",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Plagiarism % or Single % has not been imported.",
                }
            )
        if submission.plagiarism_report_stale:
            rows.append(
                {
                    "category": "Stale Plagiarism Report",
                    "paper_id": submission.paper_id_filled,
                    "final_submission_id": submission.final_submission_id,
                    "message": "Plagiarism scores changed after the report was attached. Upload the matching report before publication.",
                }
            )
        if (
            plagiarism_percent_over_threshold(submission, setting)
        ):
            if has_valid_plagiarism_percent_exception(submission, setting):
                if include_allowed:
                    rows.append(
                        {
                            "category": "Allowed Plagiarism % Exception",
                            "paper_id": submission.paper_id_filled,
                            "final_submission_id": submission.final_submission_id,
                            "message": (
                                f"{_whole_percent_label(submission.similarity_score)}% is over "
                                f"the threshold of {_whole_percent_label(setting.plagiarism_percent_threshold)}%. "
                                f"Allowed: {submission.plagiarism_percent_exception_reason}"
                            ),
                        }
                    )
            elif submission.plagiarism_percent_exception_approved:
                rows.append(
                    {
                        "category": "Stale Plagiarism % Exception",
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "message": (
                            f"Current Plagiarism % is {_whole_percent_label(submission.similarity_score)}%, "
                            f"but the approved exception was for "
                            f"{_whole_percent_label(submission.plagiarism_percent_exception_approved_score)}%."
                        ),
                    }
                )
            else:
                rows.append(
                    {
                        "category": "Plagiarism % Over Threshold",
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "message": (
                            f"{_whole_percent_label(submission.similarity_score)}% is over "
                            f"the threshold of {_whole_percent_label(setting.plagiarism_percent_threshold)}%."
                        ),
                    }
                )
        if (
            single_percent_over_threshold(submission, setting)
        ):
            if has_valid_single_percent_exception(submission, setting):
                if include_allowed:
                    rows.append(
                        {
                            "category": "Allowed Single % Exception",
                            "paper_id": submission.paper_id_filled,
                            "final_submission_id": submission.final_submission_id,
                            "message": (
                                f"{_whole_percent_label(submission.single_similarity_score)}% is over "
                                f"the threshold of {_whole_percent_label(setting.single_similarity_threshold)}%. "
                                f"Allowed: {submission.single_percent_exception_reason}"
                            ),
                        }
                    )
            elif submission.single_percent_exception_approved:
                rows.append(
                    {
                        "category": "Stale Single % Exception",
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "message": (
                            f"Current Single % is {_whole_percent_label(submission.single_similarity_score)}%, "
                            f"but the approved exception was for "
                            f"{_whole_percent_label(submission.single_percent_exception_approved_score)}%."
                        ),
                    }
                )
            else:
                rows.append(
                    {
                        "category": "Single % Over Threshold",
                        "paper_id": submission.paper_id_filled,
                        "final_submission_id": submission.final_submission_id,
                        "message": (
                            f"{_whole_percent_label(submission.single_similarity_score)}% is over "
                            f"the threshold of {_whole_percent_label(setting.single_similarity_threshold)}%."
                        ),
                    }
                )

    rows.extend(publication_duplicate_rows())
    for row in author_count_rows():
        if row["over_limit"]:
            if row["waiver_valid"]:
                if include_allowed:
                    rows.append(
                        {
                            "category": "Allowed Author Paper Count Exception",
                            "paper_id": row["paper_ids"],
                            "final_submission_id": "",
                            "message": (
                                f"{row['display_author_name']} appears in "
                                f"{row['publication_paper_count']} publication papers. "
                                f"Allowed: {row['waiver_reason']}"
                            ),
                        }
                    )
            else:
                rows.append(
                    {
                        "category": "Author Over Limit",
                        "paper_id": row["paper_ids"],
                        "final_submission_id": "",
                        "message": (
                            f"{row['display_author_name']} appears in "
                            f"{row['publication_paper_count']} publication papers."
                        ),
                    }
                )
    return rows


def canonical_paper_id_key(value):
    raw = clean_identifier(value).upper()
    raw = re.sub(r"[\s_-]+", "", raw)
    match = re.match(r"^([A-Z]+)(\d+)$", raw)
    if match:
        return f"{match.group(1)}:{int(match.group(2))}"
    return raw


def resolve_official_paper_id(raw_paper_id, final_title=""):
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
    if raw.isdigit():
        numeric_matches = []
        raw_number = int(raw)
        for paper in InitialPaper.objects.all():
            match = re.match(r"^[A-Za-z]+0*(\d+)$", clean_identifier(paper.paper_id))
            if match and int(match.group(1)) == raw_number:
                numeric_matches.append(paper)
        if numeric_matches:
            title_matches = [
                paper.paper_id
                for paper in numeric_matches
                if _titles_match_for_mapping(final_title, paper.title)
            ]
            if len(title_matches) == 1:
                return title_matches[0]
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
    text = re.sub(r"[\r\n]+", ";", raw_authors)
    text = re.sub(r"\s*,\s*(?:and|&)\s+", ";", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:and|&)\s+", ";", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[;,]\s*", ";", text)
    return [part.strip(" \t,;") for part in text.split(";") if part.strip(" \t,;")]


def duplicate_authors_in_paper(raw_authors):
    grouped = defaultdict(list)
    for index, author_name in enumerate(split_authors(raw_authors), start=1):
        normalized = normalize_author_name(author_name)
        if normalized:
            grouped[normalized].append({"name": author_name, "order": index})
    duplicates = []
    for normalized, entries in grouped.items():
        if len(entries) > 1:
            display_names = []
            for entry in entries:
                if entry["name"] not in display_names:
                    display_names.append(entry["name"])
            duplicates.append(
                {
                    "normalized_author_name": normalized,
                    "display_names": display_names,
                    "count": len(entries),
                    "orders": [entry["order"] for entry in entries],
                }
            )
    return duplicates


def has_unresolved_duplicate_authors(submission):
    if not submission or not submission.extracted_authors:
        return False
    return bool(
        duplicate_authors_in_paper(submission.extracted_authors)
        and submission.duplicate_author_review_status != "review_ok"
    )


def rebuild_paper_authors():
    with transaction.atomic():
        PaperAuthor.objects.all().delete()
        rows = []
        for submission in FinalSubmission.objects.filter(
            active_version=True,
            excluded_from_publication=False,
            discarded=False,
        ).exclude(
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
        PaperAuthor.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def author_count_rows():
    setting = AppSetting.load()
    authors = list(
        PaperAuthor.objects.select_related("final_submission").order_by(
            "normalized_author_name", "id"
        )
    )
    grouped = defaultdict(list)
    for author in authors:
        grouped[author.normalized_author_name].append(author)
    waivers = {
        waiver.normalized_author_name: waiver
        for waiver in AuthorLimitWaiver.objects.filter(
            normalized_author_name__in=grouped.keys()
        )
    }
    all_paper_ids = {author.paper_id for author in authors if author.paper_id}
    active_submission_by_paper_id = {}
    for submission in FinalSubmission.objects.filter(
        active_version=True,
        discarded=False,
        excluded_from_publication=False,
        paper_id_filled__in=all_paper_ids,
    ).order_by("paper_id_filled", "-updated_at", "-pk"):
        active_submission_by_paper_id.setdefault(submission.paper_id_filled, submission)
    rows = []
    for normalized_name, author_group in grouped.items():
        display_names = []
        for author in author_group:
            if author.author_name and author.author_name not in display_names:
                display_names.append(author.author_name)
        display_name = "; ".join(display_names)
        paper_ids = sorted({author.paper_id for author in author_group if author.paper_id})
        paper_links = []
        for paper_id in paper_ids:
            submission = active_submission_by_paper_id.get(paper_id)
            pdf_info = publication_pdf_info(submission) if submission else {
                "url": "",
                "label": "No PDF",
                "source": "missing",
                "exists": False,
            }
            paper_links.append(
                {
                    "paper_id": paper_id,
                    "url": pdf_info["url"] if pdf_info["exists"] else "",
                    "label": pdf_info["label"],
                    "source": pdf_info["source"],
                    "exists": pdf_info["exists"],
                }
            )
        paper_count = len(paper_ids)
        over_limit = paper_count > setting.author_paper_limit
        waiver = waivers.get(normalized_name)
        waiver_valid = bool(waiver and waiver.is_valid_for_count(paper_count))
        duplicate_papers = []
        for author in author_group:
            if any(
                duplicate["normalized_author_name"] == normalized_name
                for duplicate in duplicate_authors_in_paper(author.final_submission.extracted_authors)
            ):
                duplicate_papers.append(author.paper_id)
        rows.append(
            {
                "normalized_author_name": normalized_name,
                "display_author_name": display_name,
                "display_author_names": display_names,
                "publication_paper_count": paper_count,
                "paper_count": paper_count,
                "paper_ids": ", ".join(paper_ids),
                "paper_links": paper_links,
                "duplicate_author_papers": ", ".join(sorted(set(duplicate_papers))),
                "status": "Allowed" if over_limit and waiver_valid else ("Over limit" if over_limit else "OK"),
                "over_limit": over_limit,
                "waiver": waiver,
                "waiver_valid": waiver_valid,
                "exception_key": f"author_limit:{normalized_name}",
                "waiver_reason": waiver.reason if waiver else "",
                "waiver_approved_count": waiver.approved_publication_paper_count if waiver else None,
            }
        )
    return sorted(rows, key=lambda row: (-row["paper_count"], row["normalized_author_name"]))


def invalid_paper_id_submissions():
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))
    return [
        submission
        for submission in FinalSubmission.objects.filter(
            active_version=True,
            excluded_from_publication=False,
            discarded=False,
        )
        if not submission.paper_id_filled or submission.paper_id_filled not in valid_ids
    ]


def dashboard_counts():
    setting = AppSetting.load()
    active = FinalSubmission.objects.filter(
        active_version=True, excluded_from_publication=False, discarded=False
    )
    author_rows = author_count_rows()
    active_paper_ids = set(
        FinalSubmission.objects.filter(
            active_version=True,
            excluded_from_publication=False,
            discarded=False,
        )
        .exclude(paper_id_filled="")
        .values_list("paper_id_filled", flat=True)
    )
    excluded_paper_ids = set(
        FinalSubmission.objects.filter(
            active_version=True,
            excluded_from_publication=True,
            discarded=False,
        )
        .exclude(paper_id_filled="")
        .values_list("paper_id_filled", flat=True)
    )
    valid_ids = set(InitialPaper.objects.values_list("paper_id", flat=True))
    unclassified_not_in_master = active.exclude(paper_id_filled__in=valid_ids).count()
    plagiarism_over_threshold = sum(
        1
        for submission in active
        if plagiarism_percent_over_threshold(submission, setting)
        and not has_valid_plagiarism_percent_exception(submission, setting)
    )
    single_over_threshold = sum(
        1
        for submission in active
        if single_percent_over_threshold(submission, setting)
        and not has_valid_single_percent_exception(submission, setting)
    )
    plagiarism_threshold_issue_papers = sum(
        1
        for submission in active
        if (
            plagiarism_percent_over_threshold(submission, setting)
            and not has_valid_plagiarism_percent_exception(submission, setting)
        )
        or (
            single_percent_over_threshold(submission, setting)
            and not has_valid_single_percent_exception(submission, setting)
        )
    )
    allowed_plagiarism_exceptions = sum(
        1
        for submission in active
        if has_valid_plagiarism_percent_exception(submission, setting)
        or has_valid_single_percent_exception(submission, setting)
    )
    stale_plagiarism_exceptions = sum(
        1
        for submission in active
        if (
            plagiarism_percent_over_threshold(submission, setting)
            and submission.plagiarism_percent_exception_approved
            and not has_valid_plagiarism_percent_exception(submission, setting)
        )
        or (
            single_percent_over_threshold(submission, setting)
            and submission.single_percent_exception_approved
            and not has_valid_single_percent_exception(submission, setting)
        )
    )
    format_pending = active.filter(format_status="pending").count()
    format_needs_edit = active.filter(format_status="needs_edit").count()
    corrected_pdf_processing_needed = sum(
        1 for submission in active if active_pdf_needs_processing(submission)
    )
    master_by_id = {paper.paper_id: paper for paper in InitialPaper.objects.all()}
    unverified_paper_ids = sum(
        1
        for submission in active
        if not paper_id_effectively_verified(
            submission,
            master_by_id.get(submission.paper_id_filled),
        )
    )
    title_mismatches = sum(
        1
        for submission in active
        if submission.paper_id_filled in valid_ids
        and not paper_title_matches_master(
            submission,
            master_by_id.get(submission.paper_id_filled),
        )
        and not paper_id_effectively_verified(
            submission,
            master_by_id.get(submission.paper_id_filled),
        )
    )
    verified_title_differences = sum(
        1
        for submission in active
        if submission.paper_id_filled in valid_ids
        and not paper_title_matches_master(
            submission,
            master_by_id.get(submission.paper_id_filled),
        )
        and paper_id_effectively_verified(
            submission,
            master_by_id.get(submission.paper_id_filled),
        )
    )
    reviewed_extracted_title_differences = sum(
        1
        for submission in active
        if submission.title_author_review_status == "review_ok"
        and submission.extracted_title
        and submission.final_submission_title
        and not _titles_match_for_mapping(
            submission.extracted_title,
            submission.final_submission_title,
        )
    )
    title_author_attention_papers = sum(
        1
        for submission in active
        if (
            not submission.extracted_title
            or not submission.extracted_authors
            or submission.title_author_review_status != "review_ok"
        )
    )
    papers_over_author_number_limit = sum(
        1
        for submission in active
        if author_number_over_limit(submission, setting)
        and not has_valid_author_number_exception(submission)
    )
    duplicate_author_papers = sum(
        1 for submission in active if has_unresolved_duplicate_authors(submission)
    )
    from submissions.services.editor_uploads import editor_conflict_count

    return {
        "total_papers": InitialPaper.objects.count(),
        "total_final_submissions": FinalSubmission.objects.count(),
        "active_final_versions": active.count(),
        "publication_candidates": active.filter(paper_id_filled__in=valid_ids).count(),
        "unverified_paper_ids": unverified_paper_ids,
        "title_mismatches": title_mismatches,
        "verified_title_differences": verified_title_differences,
        "reviewed_extracted_title_differences": reviewed_extracted_title_differences,
        "duplicate_final_submissions": FinalSubmission.objects.filter(
            duplicate_submission=True,
            discarded=False,
        ).count(),
        "excluded_from_publication": FinalSubmission.objects.filter(
            active_version=True, excluded_from_publication=True, discarded=False
        ).count(),
        "unclassified_not_in_master": unclassified_not_in_master,
        "invalid_paper_ids": len(invalid_paper_id_submissions()),
        "missing_final_submissions": InitialPaper.objects.exclude(
            paper_id__in=active_paper_ids | excluded_paper_ids
        ).count(),
        "page_limit_errors": sum(
            1
            for submission in active
            if submission.page_count
            and submission.page_count > setting.page_limit
            and not submission.has_valid_page_limit_exception
        ),
        "page_minimum_errors": sum(
            1
            for submission in active
            if submission.page_count
            and submission.page_count < setting.page_minimum
            and not submission.has_valid_page_limit_exception
        ),
        "missing_pdfs": sum(
            1 for submission in active if not pdf_available_for_processing(submission)
        ),
        "authors_over_limit": sum(
            1 for row in author_rows if row["over_limit"] and not row["waiver_valid"]
        ),
        "missing_title_author_extraction": active.filter(
            Q(extracted_title="") | Q(extracted_authors="")
        ).count(),
        "title_author_pending": active.filter(title_author_review_status="pending").count(),
        "title_author_red_flag": active.filter(title_author_review_status="red_flag").count(),
        "title_author_review_ok": active.filter(title_author_review_status="review_ok").count(),
        "unverified_title_author_extraction": active.exclude(extracted_title="")
        .exclude(extracted_authors="")
        .exclude(title_author_review_status="review_ok")
        .count(),
        "unverified_extracted_title_match": 0,
        "title_author_attention_papers": title_author_attention_papers,
        "papers_over_author_number_limit": papers_over_author_number_limit,
        "duplicate_author_papers": duplicate_author_papers,
        "missing_plagiarism_result": active.filter(
            Q(similarity_score__isnull=True) | Q(single_similarity_score__isnull=True)
        ).count(),
        "plagiarism_over_threshold": plagiarism_over_threshold,
        "single_over_threshold": single_over_threshold,
        "plagiarism_threshold_issue_papers": plagiarism_threshold_issue_papers,
        "allowed_plagiarism_exceptions": allowed_plagiarism_exceptions,
        "stale_plagiarism_exceptions": stale_plagiarism_exceptions,
        "format_pending": format_pending,
        "format_needs_edit": format_needs_edit,
        "format_not_ok": format_pending + format_needs_edit,
        "active_pdfs_need_processing": corrected_pdf_processing_needed,
        "corrected_pdf_needs_processing": corrected_pdf_processing_needed,
        "start2_editor_conflicts": editor_conflict_count(),
    }


def error_report_rows():
    rows = publication_readiness_rows(include_allowed=True)
    for submission in FinalSubmission.objects.filter(
        title_author_source="manual_override",
        discarded=False,
    ):
        rows.append(
            {
                "category": "Manual Title/Author Override",
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "message": (
                    "Extracted title/authors were manually overridden. Reason: "
                    f"{submission.title_author_manual_override_reason or 'No reason recorded.'}"
                ),
            }
        )
    for submission in FinalSubmission.objects.filter(duplicate_submission=True, discarded=False):
        rows.append(
            {
                "category": "Replaced Final Submission",
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "message": "A newer mapped final submission exists for this Paper ID.",
            }
        )
    for submission in FinalSubmission.objects.filter(discarded=True):
        note = submission.discard_notes.strip() if submission.discard_notes else ""
        rows.append(
            {
                "category": "Discarded Final Submission",
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "message": f"This final submission version was discarded. {note}".strip(),
            }
        )
    for submission in FinalSubmission.objects.filter(excluded_from_publication=True):
        reason = submission.get_publication_exclusion_reason_display()
        note = (
            submission.publication_exclusion_notes.strip()
            if submission.publication_exclusion_notes
            else ""
        )
        rows.append(
            {
                "category": "Not Publishing Final Submission",
                "paper_id": submission.paper_id_filled,
                "final_submission_id": submission.final_submission_id,
                "message": f"Marked not publishing ({reason}). {note}".strip(),
            }
        )
    return _annotate_error_rows(rows)
