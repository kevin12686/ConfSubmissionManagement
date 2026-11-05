"""Version-7 history adapter shared by schema migration and archive restore.

This is metadata-only: no files are read/moved and no submission is updated.
Keep the v7 interpretation stable for older archives and migrations.
"""


def capture_v7_crosscheck_history(apps):
    Job = apps.get_model("submissions", "CrossCheckServiceJob")
    Attempt = apps.get_model("submissions", "CrossCheckServiceAttempt")
    Result = apps.get_model("submissions", "CrossCheckServiceResult")
    attempts = results = 0
    fields = ("status", "message", "document_id", "plagiarism_percent", "single_percent",
              "report_member", "report_sha256", "import_evidence")
    for job in Job.objects.all().iterator():
        if Attempt.objects.filter(job_id=job.pk).exists():
            continue
        items = list(job.items.all())
        status = job.status
        if job.archive_sha256:
            status = "received"
        elif status not in {"preview", "submitting", "uncertain", "running", "failed", "cancelled"}:
            status = "failed"
        attempt = Attempt.objects.create(job_id=job.pk, number=1, kind="upload", remote_id=job.remote_id,
            status=status, eligible_paper_ids=[item.paper_id for item in items], remote_file_count=len(items),
            wait_minutes=job.wait_minutes, progress=job.progress, archive_sha256=job.archive_sha256,
            message=job.message, created_at=job.created_at)
        Attempt.objects.filter(pk=attempt.pk).update(updated_at=job.updated_at)
        attempts += 1
        if job.archive_sha256:
            for item in items:
                values = {name: getattr(item, name) for name in fields}
                Result.objects.create(attempt_id=attempt.pk, item_id=item.pk,
                    upload_confirmed=bool(item.report_sha256),
                    applied_at=job.imported_at if item.status == "applied" else None, **values)
                results += 1
    return {"crosscheck_service_attempts": attempts, "crosscheck_service_results": results}
