SUBMISSION_EXCEPTION_TYPES = {
    "page",
    "author_number",
    "plagiarism_percent",
    "single_percent",
}


def submission_exception_key(exception_type, submission_or_pk):
    if exception_type not in SUBMISSION_EXCEPTION_TYPES:
        raise ValueError(f"Unsupported submission exception type: {exception_type}")
    submission_pk = getattr(submission_or_pk, "pk", submission_or_pk)
    return f"{exception_type}:{submission_pk}"


def author_limit_exception_key(normalized_author_name):
    return f"author_limit:{normalized_author_name}"
