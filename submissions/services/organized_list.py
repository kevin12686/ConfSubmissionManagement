from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.checks import (
    author_number_count,
    duplicate_authors_in_paper,
    has_unresolved_duplicate_authors,
    paper_id_effectively_verified,
    paper_title_matches_master,
    publication_duplicate_map,
    split_authors,
)
from submissions.services.editor_uploads import editor_conflict_paper_ids
from submissions.services.exceptions import (
    author_number_exception_status,
    exception_status_label,
    exception_rows_for_submission,
    page_exception_status,
    plagiarism_percent_exception_status,
    single_percent_exception_status,
)
from submissions.services.file_manager import (
    active_pdf_needs_processing,
    publication_debug_pdf_info,
    publication_pdf_info,
    publication_source_info,
)
from submissions.services.verification import normalize_title, text_diff_html, titles_identical


ORGANIZED_LIST_FILTER_OPTIONS = [
    {"value": "all", "label": "All"},
    {"value": "needs_attention", "label": "Needs attention"},
    {"value": "missing_final", "label": "Missing final"},
    {"value": "paper_id_review", "label": "Paper ID needs review"},
    {"value": "version_conflicts", "label": "Start2/Editor conflicts"},
    {"value": "title_issues", "label": "Title issues"},
    {"value": "no_authors", "label": "No authors"},
    {"value": "author_over_limit", "label": "Author over limit"},
    {"value": "page_issues", "label": "Page issues"},
    {"value": "pdf_issues", "label": "PDF issues"},
    {"value": "source_issues", "label": "Source issues"},
    {"value": "extraction_issues", "label": "Extraction issues"},
    {"value": "missing_plagiarism", "label": "Missing plagiarism"},
    {"value": "plagiarism_issues", "label": "Plagiarism issues"},
    {"value": "format_not_ok", "label": "Format not OK"},
]

ORGANIZED_LIST_SORT_OPTIONS = [
    {"value": "needs_attention", "label": "Needs attention first"},
    {"value": "paper_id_asc", "label": "Paper ID ascending"},
    {"value": "paper_id_desc", "label": "Paper ID descending"},
    {"value": "page_count_asc", "label": "Page count ascending"},
    {"value": "page_count_desc", "label": "Page count descending"},
    {"value": "author_count_desc", "label": "Author count descending"},
    {"value": "similarity_desc", "label": "Plagiarism % descending"},
    {"value": "format_status", "label": "Format status"},
]


def _page_status(submission, settings_obj):
    if not submission:
        return "No final", "danger"
    if not publication_pdf_info(submission)["exists"]:
        return "No PDF", "danger"
    if submission.processing_status == "error":
        return "PDF error", "danger"
    if submission.page_count is None:
        return "Not processed", "warning"
    if submission.page_count < settings_obj.page_minimum:
        status = page_exception_status(submission, settings_obj)
        if status == "allowed":
            return "Allowed exception", "info"
        if status == "stale":
            return "Stale allowed exception", "warning"
        return f"Below min {settings_obj.page_minimum}", "warning"
    if submission.page_count > settings_obj.page_limit:
        status = page_exception_status(submission, settings_obj)
        if status == "allowed":
            return "Allowed exception", "info"
        if status == "stale":
            return "Stale allowed exception", "warning"
        return f"Over limit {settings_obj.page_limit}", "danger"
    return "Page OK", "success"


def _verification_status(submission, paper=None):
    if not submission:
        return "No final", "secondary"
    if submission.excluded_from_publication:
        return "Excluded from publication", "secondary"
    if submission.paper_id_verified and not paper_title_matches_master(submission, paper):
        return "Verified, title differs", "warning"
    if paper_id_effectively_verified(submission, paper):
        if submission.paper_id_verified:
            return "Verified", "success"
        return "Auto-verified by title", "success"
    if submission.verification_status == "title_mismatch":
        return "Paper ID title mismatch", "warning"
    if submission.verification_status == "invalid_paper_id":
        return "Not in Master - needs decision", "danger"
    return "Paper ID needs review", "danger"


def _plagiarism_status(submission, settings_obj):
    if not submission:
        return "", "secondary"
    if submission.similarity_score is None or submission.single_similarity_score is None:
        return "No result", "danger"
    return "", "secondary"


def _is_plagiarism_over_threshold(submission, settings_obj):
    if not submission:
        return False
    plagiarism_status = plagiarism_percent_exception_status(submission, settings_obj)
    single_status = single_percent_exception_status(submission, settings_obj)
    return bool(
        plagiarism_status in {"not_allowed", "stale"}
        or single_status in {"not_allowed", "stale"}
    )


def _score_level(value, threshold):
    if value is None:
        return "secondary"
    return "danger" if value > threshold else "light"


def _plagiarism_exception_summary(submission, settings_obj):
    plagiarism_status = plagiarism_percent_exception_status(submission, settings_obj)
    single_status = single_percent_exception_status(submission, settings_obj)
    return {
        "plagiarism_percent_exception_status": plagiarism_status,
        "plagiarism_percent_exception_label": exception_status_label(plagiarism_status),
        "single_percent_exception_status": single_status,
        "single_percent_exception_label": exception_status_label(single_status),
    }


def _exception_panel_sections(submission, settings_obj):
    if not submission:
        return []
    sections = []
    for exception_row in exception_rows_for_submission(submission, settings_obj):
        sections.append(
            {
                "kind": "exception",
                "title": f"{exception_row['type_label']} exception",
                "status": exception_row["status"],
                "status_label": exception_row["status_label"],
                "status_level": exception_row["status_level"],
                "exception": exception_row,
                "is_plagiarism": exception_row["type"] in {"plagiarism_percent", "single_percent"},
            }
        )
    duplicate_authors = duplicate_authors_in_paper(submission.extracted_authors)
    if duplicate_authors:
        unresolved = has_unresolved_duplicate_authors(submission)
        sections.append(
            {
                "kind": "duplicate_author",
                "title": "Duplicate author review",
                "status": "not_allowed" if unresolved else "allowed",
                "status_label": "Not reviewed" if unresolved else "Reviewed",
                "status_level": "danger" if unresolved else "secondary",
                "duplicate_authors": duplicate_authors,
                "reason": submission.duplicate_author_review_notes,
            }
        )
    return sections


def _legacy_plagiarism_status(submission):
    if not submission.plagiarism_status:
        return "No result", "danger"
    if submission.plagiarism_status == "clear":
        return "Clear", "success"
    if submission.plagiarism_status == "review":
        return "Review", "warning"
    if submission.plagiarism_status == "flagged":
        return "Flagged", "warning"
    return submission.get_plagiarism_status_display(), "secondary"


def _source_status(submission):
    if not submission:
        return "", "secondary"
    source_info = publication_source_info(submission)
    if source_info["source"] == "corrected":
        return "Corrected", "success" if submission.format_status == "review_ok" else "warning"
    if source_info["source"] == "original":
        return "Original", "success"
    return "No source", "danger"


def _extraction_status(submission):
    if not submission:
        return "", "secondary"
    if submission.title_author_review_status == "red_flag":
        return "Red Flag", "danger"
    missing = []
    if not submission.extracted_title:
        missing.append("title")
    if not submission.extracted_authors:
        missing.append("authors")
    if not missing:
        if submission.title_author_review_status == "review_ok":
            return "Review OK", "success"
        if submission.title_author_review_status == "red_flag":
            return "Red Flag", "danger"
        return "Pending", "warning"
    if len(missing) == 2:
        return "No extracted title/authors", "danger"
    return f"No extracted {missing[0]}", "danger"


def _author_count_status(submission, settings_obj):
    if not submission or not submission.extracted_authors:
        return {
            "author_count": None,
            "author_count_label": "No authors",
            "author_count_level": "danger",
            "author_display_items": [],
            "over_author_limit": False,
            "duplicate_authors": [],
            "has_duplicate_authors": False,
            "unresolved_duplicate_authors": False,
        }
    author_names = split_authors(submission.extracted_authors)
    author_count = author_number_count(submission)
    over_limit = author_count > settings_obj.max_authors_per_paper
    exception_status = author_number_exception_status(submission, settings_obj)
    waiver_valid = exception_status == "allowed"
    duplicate_authors = duplicate_authors_in_paper(submission.extracted_authors)
    unresolved_duplicate_authors = has_unresolved_duplicate_authors(submission)
    return {
        "author_count": author_count,
        "author_count_label": f"{author_count} author{'s' if author_count != 1 else ''}",
        "author_display_items": [
            {"order": index, "name": author_name}
            for index, author_name in enumerate(author_names, start=1)
        ],
        "author_count_level": "info"
        if over_limit and waiver_valid
        else ("warning" if exception_status == "stale" else ("danger" if over_limit or unresolved_duplicate_authors else "success")),
        "over_author_limit": over_limit,
        "author_number_exception_valid": waiver_valid,
        "author_number_exception_status": exception_status,
        "author_number_exception_label": exception_status_label(exception_status),
        "duplicate_authors": duplicate_authors,
        "has_duplicate_authors": bool(duplicate_authors),
        "unresolved_duplicate_authors": unresolved_duplicate_authors,
    }


def _title_check(paper, submission):
    master_title = paper.title if paper else ""
    final_title = submission.final_submission_title if submission else ""
    extracted_title = submission.extracted_title if submission else ""
    title_values = [master_title, final_title, extracted_title]
    trimmed_titles = [title.strip() for title in title_values]
    normalized_titles = [normalize_title(title) for title in title_values]
    has_all_titles = all(trimmed_titles)
    all_exact_identical = bool(has_all_titles and len(set(trimmed_titles)) == 1)
    all_normalized_identical = bool(
        has_all_titles and all(normalized_titles) and len(set(normalized_titles)) == 1
    )
    badges = []
    if not master_title:
        badges.append({"label": "Missing master title", "level": "danger"})
    if not final_title:
        badges.append({"label": "Missing final title", "level": "danger"})
    if not extracted_title:
        badges.append({"label": "Missing extracted title", "level": "danger"})

    comparisons = []
    for label, reference_title in [("Master", master_title), ("Final", final_title)]:
        comparison = _title_comparison(label, reference_title, extracted_title)
        comparisons.append(comparison)
        if comparison["status"] == "hard":
            badges.append({"label": f"{label} title text differs", "level": "warning"})
        elif comparison["status"] == "soft":
            badges.append({"label": f"{label} punctuation/spacing only", "level": "info"})

    if all_exact_identical:
        badges.append({"label": "All titles match", "level": "success"})
    elif all_normalized_identical:
        badges.append({"label": "Only punctuation/spacing differs", "level": "info"})
    return {
        "master_title": master_title,
        "final_title": final_title,
        "extracted_title": extracted_title,
        "all_exact_identical": all_exact_identical,
        "all_normalized_identical": all_normalized_identical,
        "has_soft_diff": bool(has_all_titles and not all_exact_identical and all_normalized_identical),
        "has_hard_diff": any(comparison["status"] == "hard" for comparison in comparisons),
        "comparisons": comparisons,
        "badges": badges,
    }


def _title_comparison(label, reference_title, extracted_title):
    if not reference_title or not extracted_title:
        status = "missing"
    elif reference_title.strip() == extracted_title.strip():
        status = "identical"
    elif titles_identical(reference_title, extracted_title):
        status = "soft"
    else:
        status = "hard"
    return {
        "label": label,
        "reference_title": reference_title,
        "status": status,
        "diff_html": text_diff_html(reference_title, extracted_title)
        if reference_title and extracted_title
        else "",
    }


def _row_matches(row, query):
    if not query:
        return True
    haystack = " ".join(
        str(value or "")
        for value in [
            row["paper"].paper_id if row["paper"] else "",
            row["paper"].acceptance_status if row["paper"] else "",
            row["paper"].title if row["paper"] else "",
            row["paper"].authors if row["paper"] else "",
            row["paper"].notes if row["paper"] else "",
            row["submission"].final_submission_id if row["submission"] else "",
            row["submission"].start2_paper_id_raw if row["submission"] else "",
            row["submission"].paper_id_filled if row["submission"] else "",
            row["submission"].final_submission_title if row["submission"] else "",
            row["submission"].final_submission_authors if row["submission"] else "",
            row["submission"].extracted_title if row["submission"] else "",
            row["submission"].extracted_authors if row["submission"] else "",
            row["submission"].plagiarism_status if row["submission"] else "",
            row["submission"].similarity_score if row["submission"] else "",
            row["submission"].single_similarity_score if row["submission"] else "",
        ]
    ).lower()
    return query.lower() in haystack


def _row_paper_id(row):
    paper = row["paper"]
    submission = row["submission"]
    if paper:
        return paper.paper_id or ""
    return submission.paper_id_filled if submission else ""


def _has_page_issue(row):
    if not row["submission"]:
        return False
    return row["page_level"] in {"danger", "warning"} or row["page_label"] == "Not processed"


def _has_pdf_issue(row):
    return bool(
        row["submission"]
        and (
            not row["publication_pdf"]["exists"]
            or row["needs_processing_after_formatting"]
            or row["page_label"] == "PDF error"
            or row["page_label"] == "Not processed"
        )
    )


def _has_source_issue(row):
    return bool(
        row["submission"]
        and (
            row["source_level"] == "danger"
            or (
                row["publication_source"]["source"] == "corrected"
                and row["submission"].format_status != "review_ok"
            )
        )
    )


def _has_extraction_issue(row):
    return bool(row["submission"] and row["extraction_level"] in {"danger", "warning"})


def _has_plagiarism_issue(row):
    submission = row["submission"]
    return bool(
        submission
        and (
            submission.similarity_score is None
            or submission.single_similarity_score is None
            or row.get("plagiarism_over_threshold")
            or submission.plagiarism_report_stale
        )
    )


def _has_missing_plagiarism(row):
    submission = row["submission"]
    return bool(
        submission
        and (
            submission.similarity_score is None
            or submission.single_similarity_score is None
        )
    )


def _has_non_missing_plagiarism_issue(row):
    submission = row["submission"]
    return bool(
        submission
        and (
            row.get("plagiarism_over_threshold")
            or submission.plagiarism_report_stale
        )
    )


def _has_format_issue(row):
    return bool(row["submission"] and row["submission"].format_status != "review_ok")


def _is_verified_title_diff(row):
    submission = row["submission"]
    return bool(
        submission
        and submission.paper_id_verified
        and row["verify_level"] == "warning"
        and row["verify_label"] == "Verified, title differs"
    )


def _has_missing_title_issue(row):
    if not row["submission"]:
        return False
    title_check = row["title_check"]
    return bool(
        not title_check["master_title"]
        or not title_check["final_title"]
        or not title_check["extracted_title"]
    )


def _has_title_match_unverified_issue(row):
    submission = row["submission"]
    title_check = row["title_check"]
    return bool(
        submission
        and title_check["final_title"]
        and title_check["extracted_title"]
        and not submission.title_match_review_complete
    )


def _has_hard_title_diff(row):
    return any(
        comparison["status"] == "hard"
        for comparison in row["title_check"]["comparisons"]
    )


def _has_verified_hard_title_diff(row):
    submission = row["submission"]
    return bool(
        submission
        and (
            _is_verified_title_diff(row)
            or (
                submission.title_match_review_complete
                and _has_hard_title_diff(row)
            )
        )
    )


def _has_soft_title_issue(row):
    return bool(
        row["title_check"]["has_soft_diff"]
        or any(
            comparison["status"] == "soft"
            for comparison in row["title_check"]["comparisons"]
        )
    )


def _has_author_issue(row):
    return bool(
        row["submission"]
        and (
            row["author_count"] is None
            or (row["over_author_limit"] and not row.get("author_number_exception_valid"))
            or row.get("unresolved_duplicate_authors")
        )
    )


def _has_title_issue(row):
    return bool(
        _has_missing_title_issue(row)
        or _has_title_match_unverified_issue(row)
        or _has_verified_hard_title_diff(row)
        or _has_soft_title_issue(row)
    )


def _needs_attention(row):
    return any(
        [
            not row["submission"],
            row["row_type"] == "unmatched",
            row.get("duplicate_badges"),
            row.get("version_conflict"),
            row["verify_level"] in {"danger", "warning"},
            _has_pdf_issue(row),
            _has_page_issue(row),
            _has_title_issue(row),
            _has_author_issue(row),
            _has_source_issue(row),
            _has_extraction_issue(row),
            _has_plagiarism_issue(row),
            _has_format_issue(row),
        ]
    )


def _attention_priority(row):
    if not row["submission"]:
        return 0
    if row["row_type"] == "unmatched" or row["verify_level"] == "danger":
        return 1
    if row.get("version_conflict"):
        return 2
    if not row["publication_pdf"]["exists"]:
        return 3
    if row.get("duplicate_badges"):
        return 4
    if _has_pdf_issue(row):
        return 5
    if _has_page_issue(row):
        return 6
    if _has_source_issue(row):
        return 7
    if _has_missing_title_issue(row):
        return 8
    if _has_title_match_unverified_issue(row):
        return 9
    if _has_extraction_issue(row):
        return 10
    if _has_author_issue(row):
        return 11
    if _has_plagiarism_issue(row):
        return 12
    if _has_format_issue(row):
        return 13
    if _has_verified_hard_title_diff(row):
        return 14
    if _has_soft_title_issue(row):
        return 15
    return 99


def _format_status_rank(row):
    submission = row["submission"]
    if not submission:
        return 0
    return {"pending": 0, "needs_edit": 1, "review_ok": 2}.get(submission.format_status, 3)


def _filter_rows(rows, current_filter):
    predicates = {
        "all": lambda row: True,
        "needs_attention": _needs_attention,
        "missing_final": lambda row: not row["submission"],
        "paper_id_review": lambda row: bool(
            row["submission"] and row["verify_level"] in {"danger", "warning"}
        ),
        "version_conflicts": lambda row: bool(row.get("version_conflict")),
        "title_issues": _has_title_issue,
        "no_authors": lambda row: row["author_count"] is None,
        "author_over_limit": lambda row: row["over_author_limit"] and not row.get("author_number_exception_valid"),
        "page_issues": _has_page_issue,
        "pdf_issues": _has_pdf_issue,
        "source_issues": _has_source_issue,
        "extraction_issues": _has_extraction_issue,
        "missing_plagiarism": _has_missing_plagiarism,
        "plagiarism_issues": _has_plagiarism_issue,
        "format_not_ok": _has_format_issue,
    }
    predicate = predicates.get(current_filter, predicates["needs_attention"])
    return [row for row in rows if predicate(row)]


def _sort_rows(rows, current_sort):
    if current_sort == "paper_id_desc":
        return sorted(rows, key=_row_paper_id, reverse=True)
    if current_sort == "page_count_asc":
        return sorted(
            rows,
            key=lambda row: (
                row["submission"].page_count is None if row["submission"] else True,
                row["submission"].page_count if row["submission"] and row["submission"].page_count is not None else 0,
                _row_paper_id(row),
            ),
        )
    if current_sort == "page_count_desc":
        return sorted(
            rows,
            key=lambda row: (
                row["submission"].page_count or -1 if row["submission"] else -1,
                _row_paper_id(row),
            ),
            reverse=True,
        )
    if current_sort == "author_count_desc":
        return sorted(
            rows,
            key=lambda row: (row["author_count"] or -1, _row_paper_id(row)),
            reverse=True,
        )
    if current_sort == "similarity_desc":
        return sorted(
            rows,
            key=lambda row: (
                row["submission"].similarity_score if row["submission"] and row["submission"].similarity_score is not None else -1,
                _row_paper_id(row),
            ),
            reverse=True,
        )
    if current_sort == "format_status":
        return sorted(rows, key=lambda row: (_format_status_rank(row), _row_paper_id(row)))
    if current_sort == "paper_id_asc":
        return sorted(rows, key=_row_paper_id)
    return sorted(rows, key=lambda row: (_attention_priority(row), _row_paper_id(row)))


def organized_list_rows(query="", current_filter="all", current_sort="needs_attention"):
    settings_obj = AppSetting.load()
    papers = list(InitialPaper.objects.all())
    valid_paper_ids = {paper.paper_id for paper in papers}
    duplicate_map = publication_duplicate_map()
    conflict_paper_ids = set(editor_conflict_paper_ids())
    active_submissions = (
        FinalSubmission.objects.filter(
            active_version=True,
            discarded=False,
            excluded_from_publication=False,
        )
        .order_by("paper_id_filled", "-created_at", "-final_submission_id")
    )
    active_by_paper_id = {}
    for submission in active_submissions:
        active_by_paper_id.setdefault(submission.paper_id_filled, submission)

    rows = []
    for paper in papers:
        submission = active_by_paper_id.get(paper.paper_id)
        page_label, page_level = _page_status(submission, settings_obj)
        verify_label, verify_level = _verification_status(submission, paper)
        plagiarism_label, plagiarism_level = _plagiarism_status(submission, settings_obj)
        source_label, source_level = _source_status(submission)
        extraction_label, extraction_level = _extraction_status(submission)
        author_status = _author_count_status(submission, settings_obj)
        publication_pdf = publication_pdf_info(submission) if submission else None
        publication_source = publication_source_info(submission) if submission else None
        debug_pdf = publication_debug_pdf_info(submission, paper) if submission else None
        rows.append(
            {
                "row_type": "master",
                "paper": paper,
                "submission": submission,
                "publication_pdf": publication_pdf,
                "publication_source": publication_source,
                "debug_pdf": debug_pdf,
                "duplicate_badges": duplicate_map.get(submission.pk, []) if submission else [],
                "version_conflict": bool(submission and paper.paper_id in conflict_paper_ids),
                "needs_processing_after_formatting": active_pdf_needs_processing(submission)
                if submission
                else False,
                "title_check": _title_check(paper, submission),
                "page_label": page_label,
                "page_level": page_level,
                "verify_label": verify_label,
                "verify_level": verify_level,
                "plagiarism_label": plagiarism_label,
                "plagiarism_level": plagiarism_level,
                "plagiarism_percent_level": _score_level(
                    submission.similarity_score if submission else None,
                    settings_obj.plagiarism_percent_threshold,
                ),
                "single_percent_level": _score_level(
                    submission.single_similarity_score if submission else None,
                    settings_obj.single_similarity_threshold,
                ),
                "plagiarism_over_threshold": _is_plagiarism_over_threshold(
                    submission, settings_obj
                ),
                **_plagiarism_exception_summary(submission, settings_obj),
                "exception_panel_sections": _exception_panel_sections(
                    submission, settings_obj
                ),
                "source_label": source_label,
                "source_level": source_level,
                "extraction_label": extraction_label,
                "extraction_level": extraction_level,
                **author_status,
            }
        )

    for submission in active_submissions:
        if submission.excluded_from_publication:
            continue
        if submission.paper_id_filled in valid_paper_ids:
            continue
        page_label, page_level = _page_status(submission, settings_obj)
        verify_label, verify_level = _verification_status(submission)
        plagiarism_label, plagiarism_level = _plagiarism_status(submission, settings_obj)
        source_label, source_level = _source_status(submission)
        extraction_label, extraction_level = _extraction_status(submission)
        author_status = _author_count_status(submission, settings_obj)
        publication_pdf = publication_pdf_info(submission)
        publication_source = publication_source_info(submission)
        debug_pdf = publication_debug_pdf_info(submission)
        rows.append(
            {
                "row_type": "unmatched",
                "paper": None,
                "submission": submission,
                "publication_pdf": publication_pdf,
                "publication_source": publication_source,
                "debug_pdf": debug_pdf,
                "duplicate_badges": duplicate_map.get(submission.pk, []),
                "version_conflict": submission.paper_id_filled in conflict_paper_ids,
                "needs_processing_after_formatting": active_pdf_needs_processing(submission),
                "title_check": _title_check(None, submission),
                "page_label": page_label,
                "page_level": page_level,
                "verify_label": verify_label,
                "verify_level": verify_level,
                "plagiarism_label": plagiarism_label,
                "plagiarism_level": plagiarism_level,
                "plagiarism_percent_level": _score_level(
                    submission.similarity_score,
                    settings_obj.plagiarism_percent_threshold,
                ),
                "single_percent_level": _score_level(
                    submission.single_similarity_score,
                    settings_obj.single_similarity_threshold,
                ),
                "plagiarism_over_threshold": _is_plagiarism_over_threshold(
                    submission, settings_obj
                ),
                **_plagiarism_exception_summary(submission, settings_obj),
                "exception_panel_sections": _exception_panel_sections(
                    submission, settings_obj
                ),
                "source_label": source_label,
                "source_level": source_level,
                "extraction_label": extraction_label,
                "extraction_level": extraction_level,
                **author_status,
            }
        )

    valid_filter_values = {option["value"] for option in ORGANIZED_LIST_FILTER_OPTIONS}
    valid_sort_values = {option["value"] for option in ORGANIZED_LIST_SORT_OPTIONS}
    if current_filter not in valid_filter_values:
        current_filter = "all"
    if current_sort not in valid_sort_values:
        current_sort = "needs_attention"

    searched_rows = [row for row in rows if _row_matches(row, query.strip())]
    filtered_rows = _sort_rows(_filter_rows(searched_rows, current_filter), current_sort)
    needs_process_rows = [
        row
        for row in searched_rows
        if row["submission"] and row["needs_processing_after_formatting"]
    ]
    summary = {
        "total_rows": len(filtered_rows),
        "missing_final": sum(1 for row in filtered_rows if not row["submission"]),
        "unverified": sum(
            1
            for row in filtered_rows
            if row["submission"] and row["verify_level"] in {"danger", "warning"}
        ),
        "page_errors": sum(
            1
            for row in filtered_rows
            if _has_page_issue(row)
        ),
        "missing_plagiarism": sum(
            1
            for row in filtered_rows
            if row["submission"]
            and (
                row["submission"].similarity_score is None
                or row["submission"].single_similarity_score is None
            )
        ),
        "plagiarism_issues": sum(
            1
            for row in filtered_rows
            if _has_non_missing_plagiarism_issue(row)
        ),
        "publication_duplicates": sum(
            1 for row in filtered_rows if row["submission"] and row.get("duplicate_badges")
        ),
        "duplicate_author_issues": sum(
            1 for row in filtered_rows if row["submission"] and row.get("unresolved_duplicate_authors")
        ),
        "version_conflicts": sum(1 for row in filtered_rows if row.get("version_conflict")),
        "excluded_from_publication": FinalSubmission.objects.filter(
            active_version=True, excluded_from_publication=True, discarded=False
        ).count(),
        "needs_process_pdfs": len(needs_process_rows),
        "needs_process_pdf_labels": [
            f"{_row_paper_id(row) or 'No Paper ID'} / Final {row['submission'].final_submission_id}"
            for row in needs_process_rows[:12]
        ],
        "needs_process_pdf_more": max(len(needs_process_rows) - 12, 0),
    }
    return filtered_rows, summary, settings_obj, current_filter, current_sort
