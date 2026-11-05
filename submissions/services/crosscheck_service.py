"""Previewed, version-bound automatic CrossCheck jobs.

The manual export service owns PDF selection and durable provenance. Remote
results can only update the exact Final/PDF pair recorded by that service.
"""

import csv
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from submissions.models import (
    AppSetting, CrossCheckServiceAttempt, CrossCheckServiceJob, CrossCheckServiceJobItem,
)
from submissions.services.audit import audit_failure, audit_preview, audit_requested, audit_success
from submissions.services.crosscheck import (
    CROSSCHECK_EXPORT_SCOPES, _crosscheck_batch_submission,
    _sha256_path, _assert_crosscheck_scope_unambiguous, prepare_crosscheck_upload,
)
from submissions.services.crosscheck_client import CrossCheckClient, CrossCheckServiceError
from submissions.services.crosscheck_client import normalize_service_url
from submissions.services.final_submission_state import bulk_update_submissions
from submissions.services.publication_read import PublicationReadContext
from submissions.services.workflow_evidence import (
    require_evidence_token,
)
from submissions.services.crosscheck_service_history import (
    active_attempt, archive_path, attempt_archive,
    confirmed_upload_ids, current_result, initial_attempt, job_root,
    job_evidence, job_evidence_token, parse_service_archive,
    plagiarism_evidence as _plagiarism_evidence, record_attempt_results,
    remote_matches as _remote_matches, result_pdf_bytes, sync_batch_status,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9]+$")
SAFE_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def batch_prefix(conference_name, day):
    name = re.sub(r"[^A-Za-z0-9]+", "_", conference_name).strip("_")
    if not name:
        raise ValueError("Conference name must contain at least one English letter or number.")
    return f"{name}_CFM_{MONTHS[day.month - 1]}{day.day:02d}{day.year}_"


def _configuration(job=None):
    config = AppSetting.read()
    if not config.crosscheck_service_enabled:
        raise ValueError("Enable automatic CrossCheck in Settings first.")
    normalize_service_url(config.crosscheck_service_url)
    if not config.crosscheck_service_folder.strip():
        raise ValueError("Select the conference CrossCheck folder in Settings first.")
    if config.crosscheck_service_conference != config.conference_name:
        raise ValueError("Conference name changed. Confirm the CrossCheck folder again in Settings.")
    if job and (job.conference_name, job.service_url, job.folder, job.wait_minutes) != (
            config.conference_name, config.crosscheck_service_url, config.crosscheck_service_folder,
            config.crosscheck_service_wait_minutes):
        raise ValueError("CrossCheck settings changed. Prepare a new upload preview.")
    return config


def _job_event(action, job, **kwargs):
    audit_success(action, f"CrossCheck batch {job.token}: {job.status}.",
                  object_type="CrossCheckServiceJob", extra={"job_id": str(job.pk), "remote_id": job.remote_id, "token": job.token, **kwargs})


def preview_service_job(scope):
    config = _configuration()
    if scope not in CROSSCHECK_EXPORT_SCOPES:
        raise ValueError("Unknown CrossCheck scope.")
    day = timezone.now().astimezone(ZoneInfo(config.time_zone)).date()
    prefix = batch_prefix(config.conference_name, day)
    for attempt in range(5):
        try:
            with transaction.atomic():
                number = (CrossCheckServiceJob.objects.filter(batch_day=day).aggregate(n=Max("batch_number"))["n"] or 0) + 1
                job = CrossCheckServiceJob.objects.create(token=f"{prefix}{number}", batch_day=day,
                    batch_number=number, conference_name=config.conference_name, service_url=config.crosscheck_service_url,
                    folder=config.crosscheck_service_folder, scope=scope, wait_minutes=config.crosscheck_service_wait_minutes)
                execution = CrossCheckServiceAttempt.objects.create(job=job, number=1, wait_minutes=job.wait_minutes)
            break
        except IntegrityError:
            if attempt == 4:
                raise ValueError("Another editor is allocating a batch. Please try again.") from None
    try:
        exported = prepare_crosscheck_upload(job.token, scope=scope)
        with open(exported["manifest_path"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        root = job_root(job) / "inputs"
        root.mkdir(parents=True, exist_ok=False)
        items = []
        for row in rows:
            paper_id = row["paper_id"]
            if not SAFE_ID.fullmatch(paper_id):
                raise ValueError(f"Paper ID {paper_id} must contain letters and numbers for automatic upload.")
            if len(f"{paper_id}_{job.token}.pdf") > 240:
                raise ValueError("Conference name and Paper ID make the remote filename too long. Shorten the conference name.")
            source = Path(exported["target_dir"]) / row["exported_filename"]
            target = root / f"{paper_id}.pdf"
            shutil.copyfile(source, target)
            if _sha256_path(target) != row["publication_pdf_sha256"]:
                raise ValueError("Prepared PDF bytes changed. Prepare a new batch.")
            submission, problem, _stale = _crosscheck_batch_submission(paper_id, job.token)
            if problem:
                raise ValueError(problem)
            from submissions.services.file_manager import publication_pdf_info
            items.append(CrossCheckServiceJobItem(job=job, paper_id=paper_id,
                final_submission_id=row["final_submission_id"], pdf_sha256=row["publication_pdf_sha256"],
                source=publication_pdf_info(submission, include_url=False)["source"]))
        CrossCheckServiceJobItem.objects.bulk_create(items)
        execution.eligible_paper_ids = [item.paper_id for item in items]
        execution.remote_file_count = len(items)
        execution.save()
        job.skipped = exported["skipped"]
        job.save(update_fields=["skipped", "updated_at"])
        audit_preview("crosscheck_service_preview", "Automatic CrossCheck upload preview prepared.",
                      extra={"job_id": str(job.pk), "token": job.token}, result_counts={"papers": len(items), "skipped": len(job.skipped)})
        return job
    except Exception as exc:
        shutil.rmtree(job_root(job), ignore_errors=True)
        job.status = "failed"
        job.message = str(exc)
        job.save(update_fields=["status", "message", "updated_at"])
        execution.status = "failed"
        execution.message = str(exc)
        execution.save()
        audit_failure("crosscheck_service_preview", exc)
        raise



def start_service_job(job_id, evidence_token):
    job = CrossCheckServiceJob.objects.get(pk=job_id)
    _configuration(job)
    if timezone.now() - job.created_at > timedelta(hours=2):
        raise ValueError("Upload preview expired. Prepare a new batch.")
    client = CrossCheckClient(job.service_url)
    folders = client.folders()
    if job.folder not in folders["folders"]:
        raise ValueError("Selected folder is no longer available on the service.")
    if client.json("/api/jobs").get("active_id"):
        raise ValueError("CrossCheck service is busy. No files were sent.")
    with transaction.atomic():
        job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
        require_evidence_token(evidence_token, "crosscheck-service-job", job_evidence(job))
        if job.status != "preview":
            raise ValueError("This batch has already been submitted or cancelled.")
        inputs = []
        for item in job.items.all():
            _submission, problem, _stale = _crosscheck_batch_submission(item.paper_id, job.token, lock=True)
            path = job_root(job) / "inputs" / f"{item.paper_id}.pdf"
            if problem or not path.is_file() or _sha256_path(path) != item.pdf_sha256:
                raise ValueError(problem or "Prepared PDF is missing or changed. Prepare a new batch.")
            inputs.append((f"{item.paper_id}.pdf", path, item.pdf_sha256))
        if not inputs:
            raise ValueError("This batch has no PDFs.")
        audit_requested("crosscheck_service_submit", "Submitting one version-bound CrossCheck batch.",
                        extra={"job_id": str(job.pk), "token": job.token, "folder": job.folder}, result_counts={"papers": len(inputs)})
        job.status = "submitting"
        job.save(update_fields=["status", "updated_at"])
        execution = initial_attempt(job)
        execution.status = "submitting"
        execution.save()
    # Commit before the external side effect. An ambiguous response must never
    # turn this batch back into a submit-ready preview.
    try:
        def check_before_send():
            _configuration(job)
            _assert_crosscheck_scope_unambiguous(PublicationReadContext.load(require_stable_database=True))
            for item in job.items.all():
                _, problem, _ = _crosscheck_batch_submission(item.paper_id, job.token)
                if problem:
                    raise ValueError(problem)
        response = client.upload(job, inputs, before_send=check_before_send)
        remote_id = response.get("id", "")
        if not SAFE_REMOTE_ID.fullmatch(str(remote_id)):
            raise CrossCheckServiceError("Service accepted the request without a usable job ID.")
        _remote_matches(job, response)
        job.remote_id = remote_id
        job.status = "running"
        job.message = "Files accepted by service; waiting for CrossCheck confirmation."
    except Exception as exc:
        job.status = "preview" if isinstance(exc, CrossCheckServiceError) and exc.http_status == 409 else "uncertain"
        job.message = str(exc)
        job.save(update_fields=["status", "message", "updated_at"])
        execution.status = job.status
        execution.message = str(exc)
        execution.save()
        audit_failure("crosscheck_service_submit", exc, extra={"job_id": str(job.pk), "token": job.token})
        raise
    job.save(update_fields=["remote_id", "status", "message", "updated_at"])
    execution.remote_id = job.remote_id
    execution.status = "running"
    execution.message = job.message
    execution.save()
    _job_event("crosscheck_service_started", job)
    return job


def _result_archive(job):
    return attempt_archive(job, initial_attempt(job))


def _progress(response):
    progress = response.get("progress") or {}
    if not isinstance(progress, dict):
        raise ValueError("Service returned invalid progress metadata.")
    return {key: value for key, value in progress.items()
            if key in {"phase", "completed", "total", "succeeded", "failed", "timed_out",
                       "ready", "wait_timed_out", "downloaded", "download_failed", "report_errors"}
            and isinstance(value, (str, int, float, bool))}


def _availability(job, response):
    remaining = response.get("retry_remaining")
    supported = type(remaining) is int and 0 <= remaining <= job.items.count() and type(response.get("can_retry_download")) is bool
    return {"supported": supported, "remaining": remaining if supported else None,
            "can_retry": supported and response["can_retry_download"],
            "checked_at": timezone.now().isoformat()}


def _validate_remote_identity(job, execution, response):
    count = _remote_matches(job, response, execution)
    remote_id = response.get("id")
    if not isinstance(remote_id, str) or not SAFE_REMOTE_ID.fullmatch(remote_id):
        raise ValueError("Service returned an invalid job ID.")
    if execution.remote_id and execution.remote_id != remote_id:
        raise ValueError("Service returned a different job ID.")
    if job.attempts.filter(remote_id=remote_id).exclude(pk=execution.pk).exists():
        raise ValueError("Remote execution is already bound to another attempt.")
    return count


def _recover_execution(job, execution, client):
    candidates = []
    for response in client.json("/api/jobs").get("jobs", []):
        if execution.kind == "upload" and response.get("kind") == "retry_download":
            continue
        try:
            _validate_remote_identity(job, execution, response)
        except ValueError:
            continue
        candidates.append(response)
    if len(candidates) != 1:
        raise ValueError("Cannot identify one remote execution. No request was repeated; check service history and try Check progress again.")
    response = candidates[0]
    with transaction.atomic():
        execution = job.attempts.select_for_update().get(pk=execution.pk)
        execution.remote_id = response["id"]
        execution.remote_file_count = response["settings"]["file_count"]
        execution.status = "running"
        execution.save()
        if execution.kind == "upload":
            job.remote_id = execution.remote_id
            job.save(update_fields=["remote_id", "updated_at"])
        _job_event("crosscheck_service_recover", job, attempt_id=str(execution.pk), recovered_remote_id=execution.remote_id)
    return execution


def refresh_service_job(job_id):
    job = CrossCheckServiceJob.objects.get(pk=job_id)
    _configuration(job)
    execution = active_attempt(job) or job.attempts.exclude(remote_id="").last()
    if not execution:
        return job
    client = CrossCheckClient(job.service_url)
    if not execution.remote_id:
        execution = _recover_execution(job, execution, client)
    response = client.json(f"/api/jobs/{execution.remote_id}")
    count = _validate_remote_identity(job, execution, response)
    execution.remote_file_count = count
    execution.progress = _progress(response)
    execution.availability = _availability(job, response)
    if execution.archive_sha256:
        # Finished ZIPs are immutable. A later check updates retry availability,
        # never overwrites the archive or replays an applied result.
        execution.save(update_fields=["availability", "updated_at"])
        _discover_retained_retries(job, client)
        sync_batch_status(job)
        return job
    if response.get("can_download") is True:
        staged = archive_path(job, execution).parent / f".{uuid.uuid4().hex}.zip"
        staged.parent.mkdir(parents=True, exist_ok=True)
        try:
            client.download(execution.remote_id, staged)
            rows = parse_service_archive(job, staged, attempt=execution)
            execution.save(update_fields=["progress", "availability", "remote_file_count", "updated_at"])
            return record_attempt_results(job, execution, staged, rows)
        finally:
            staged.unlink(missing_ok=True)
    if response.get("status") in {"failed", "stopped", "cancelled", "completed"}:
        execution.status = "failed"
        execution.message = "Service execution ended without a downloadable ZIP. Earlier results are retained."
    else:
        execution.status = "running"
        execution.message = "Waiting for the service to finish this execution."
    execution.save()
    if execution.status == "failed":
        job.status = "failed"
        job.message = execution.message
        job.save(update_fields=["message", "updated_at"])
        _job_event("crosscheck_service_failed", job, attempt_id=str(execution.pk))
    sync_batch_status(job)
    return job


def _discover_retained_retries(job, client):
    original = initial_attempt(job)
    if not original.remote_id or active_attempt(job):
        return
    # A retry may have been started in the service UI or accepted before a lost
    # response. Recover lineage, never substitute a similarly named batch.
    responses = client.json("/api/jobs").get("jobs", [])
    related = [row for row in responses if row.get("kind") == "retry_download"
               and row.get("settings", {}).get("source_job_id") == original.remote_id]
    for response in sorted(related, key=lambda row: row.get("created_at", "")):
        remote_id = response.get("id")
        if job.attempts.filter(remote_id=remote_id).exists():
            continue
        parent = job.attempts.filter(remote_id=response.get("settings", {}).get("parent_job_id")).first()
        if not parent:
            raise ValueError("Service retry history has an unknown parent. No execution was guessed.")
        eligible = sorted(confirmed_upload_ids(job))
        with transaction.atomic():
            locked = CrossCheckServiceJob.objects.select_for_update().get(pk=job.pk)
            if locked.attempts.filter(remote_id=remote_id).exists():
                continue
            minutes = response.get("settings", {}).get("wait_minutes", parent.wait_minutes)
            if type(minutes) is not int or not 1 <= minutes <= 1440:
                raise ValueError("Service retry history contains invalid wait minutes.")
            execution = CrossCheckServiceAttempt(job=locked, number=(locked.attempts.aggregate(n=Max("number"))["n"] or 0) + 1,
                kind="retry_download", parent=parent, eligible_paper_ids=eligible,
                wait_minutes=minutes,
                status="pending_download" if response.get("can_download") is True else "running")
            execution.remote_file_count = _validate_remote_identity(job, execution, response)
            execution.remote_id = remote_id
            execution.progress = _progress(response)
            execution.availability = _availability(job, response)
            execution.save()
            _job_event("crosscheck_service_recover", job, attempt_id=str(execution.pk), recovered_remote_id=remote_id)
        if execution.status == "running":
            break


def retry_service_downloads(job_id, evidence_token, wait_minutes, *, acknowledge_stale=False):
    job = CrossCheckServiceJob.objects.get(pk=job_id)
    _configuration(job)
    require_evidence_token(evidence_token, "crosscheck-service-job", job_evidence(job))
    try:
        minutes = int(wait_minutes)
    except (ValueError, TypeError):
        raise ValueError("Use 1 to 1440 report wait minutes.") from None
    if isinstance(wait_minutes, bool) or str(minutes) != str(wait_minutes).strip() or not 1 <= minutes <= 1440:
        raise ValueError("Use 1 to 1440 report wait minutes.")
    if active_attempt(job):
        raise ValueError("An execution is still running or needs recovery. Use Check progress; no retry was sent.")
    parent = job.attempts.exclude(remote_id="").last()
    if not parent:
        raise ValueError("This batch has no retained remote job to retry.")
    client = CrossCheckClient(job.service_url)
    response = client.json(f"/api/jobs/{parent.remote_id}")
    _validate_remote_identity(job, parent, response)
    availability = _availability(job, response)
    parent.availability = availability
    parent.save(update_fields=["availability", "updated_at"])
    if not availability["supported"]:
        raise ValueError("This service does not support retry-download. Update the service first.")
    if not availability["remaining"]:
        raise ValueError("No confirmed uploads remain to download. Check progress to recover retained executions.")
    if not availability["can_retry"]:
        raise ValueError("The service is busy or the source job is not finished. No retry was sent.")
    confirmed = confirmed_upload_ids(job)
    eligible = sorted(confirmed - set(job.items.exclude(report_sha256="").values_list("paper_id", flat=True)))
    if not eligible or availability["remaining"] > len(eligible):
        raise ValueError("Service retry counts differ from local evidence. Check progress before retrying.")
    stale = [paper for paper in eligible if _crosscheck_batch_submission(paper, job.token)[1]]
    if len(stale) == len(eligible):
        raise ValueError("All remaining uploads are old publication versions. Prepare a new publication batch instead.")
    if stale and not acknowledge_stale:
        raise ValueError("Some original PDFs are no longer current. Confirm that any old-version reports will remain history only.")
    with transaction.atomic():
        job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
        require_evidence_token(evidence_token, "crosscheck-service-job", job_evidence(job))
        if active_attempt(job):
            raise ValueError("Another editor already started an execution. No duplicate request was sent.")
        execution = CrossCheckServiceAttempt.objects.create(job=job, number=(job.attempts.aggregate(n=Max("number"))["n"] or 0) + 1,
            kind="retry_download", parent=parent, eligible_paper_ids=eligible, wait_minutes=minutes, status="submitting")
        sync_batch_status(job)
        audit_requested("crosscheck_service_retry_download", "Retrying missing reports without uploading PDFs.",
            extra={"job_id": str(job.pk), "attempt_id": str(execution.pk), "parent_remote_id": parent.remote_id,
                   "eligible_paper_ids": eligible, "stale_paper_ids": stale})
    try:
        response = client.retry_download(parent.remote_id, minutes)
        execution.remote_file_count = _validate_remote_identity(job, execution, response)
        execution.remote_id = response["id"]
        execution.status = "running"
        execution.message = "Retry accepted; waiting for remaining reports."
        execution.save()
        sync_batch_status(job)
        _job_event("crosscheck_service_retry_download", job, attempt_id=str(execution.pk), retry_remote_id=execution.remote_id)
    except Exception as exc:
        rejected = isinstance(exc, CrossCheckServiceError) and exc.http_status in {400, 401, 403, 404, 409, 422}
        execution.status = "rejected" if rejected else "uncertain"
        execution.message = str(exc)
        execution.save()
        sync_batch_status(job)
        audit_failure("crosscheck_service_retry_download", exc, extra={"job_id": str(job.pk), "attempt_id": str(execution.pk)})
        raise
    return job


def apply_service_results(job_id, evidence_token):
    created = []
    try:
        with transaction.atomic():
            job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
            require_evidence_token(evidence_token, "crosscheck-service-job", job_evidence(job))
            if not job.items.filter(status="downloaded").exists():
                raise ValueError("No unapplied results are ready.")
            _assert_crosscheck_scope_unambiguous(PublicationReadContext.load(require_stable_database=True))
            changed, applied = [], []
            for item in job.items.select_for_update().filter(status="downloaded"):
                result = current_result(job, item)
                if result.applied_at or result.status != "downloaded":
                    raise ValueError("This report has already been applied or is not ready.")
                if any(getattr(result, name) != getattr(item, name) for name in
                       ("report_sha256", "report_member", "plagiarism_percent", "single_percent", "import_evidence")):
                    raise ValueError("Report bytes changed after preview.")
                submission, problem, _ = _crosscheck_batch_submission(item.paper_id, job.token, lock=True)
                if problem or _plagiarism_evidence(submission) != result.import_evidence:
                    item.status = result.status = "stale"
                    item.message = result.message = problem or "Submission/review data changed after result preview. No result applied."
                    item.save(update_fields=["status", "message"])
                    result.save(update_fields=["status", "message"])
                    continue
                report = result_pdf_bytes(job, result)
                folder = Path(AppSetting.read().plagiarism_reports_folder)
                if not folder.is_absolute():
                    folder = Path(settings.BASE_DIR) / folder
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{item.paper_id}_{job.token}_{uuid.uuid4().hex}_report.pdf"
                created.append(target)
                with target.open("xb") as handle:
                    handle.write(report)
                before = {"plagiarism_percent": str(submission.similarity_score), "single_percent": str(submission.single_similarity_score)}
                submission.similarity_score = result.plagiarism_percent
                submission.single_similarity_score = result.single_percent
                submission.plagiarism_report_path = str(target)
                submission.plagiarism_report_stale = False
                submission.plagiarism_imported_at = timezone.now()
                changed.append(submission)
                item.status = result.status = "applied"
                item.message = "Scores and report applied to the verified publication PDF."
                result.applied_at = timezone.now()
                item.save(update_fields=["status", "message"])
                result.save(update_fields=["status", "applied_at"])
                applied.append({"paper_id": item.paper_id, "final_submission_id": item.final_submission_id,
                                "attempt_id": str(result.attempt_id), "pdf_sha256": item.pdf_sha256})
                audit_success("crosscheck_service_item_apply", "Version-bound CrossCheck scores and report applied.",
                    submission=submission, before=before,
                    after={"plagiarism_percent": str(result.plagiarism_percent), "single_percent": str(result.single_percent)},
                    file_hashes={"publication_pdf": item.pdf_sha256, "report": result.report_sha256},
                    extra={"job_id": str(job.pk), "attempt_id": str(result.attempt_id), "result_id": result.pk, "token": job.token})
            bulk_update_submissions(changed, ["similarity_score", "single_similarity_score", "plagiarism_report_path",
                                             "plagiarism_report_stale", "plagiarism_imported_at"])
            job.imported_at = timezone.now()
            job.message = f"Applied {len(applied)} new paper(s); previous results and unavailable/stale items were retained."
            job.save(update_fields=["imported_at", "message", "updated_at"])
            sync_batch_status(job)
            _job_event("crosscheck_service_result_apply", job, applied=applied)
        return job
    except Exception as exc:
        for path in created:
            path.unlink(missing_ok=True)
        audit_failure("crosscheck_service_result_apply", exc, extra={"job_id": str(job_id)})
        raise


def stop_service_job(job_id):
    job = CrossCheckServiceJob.objects.get(pk=job_id)
    execution = active_attempt(job)
    if not execution or not execution.remote_id:
        raise ValueError("This batch has no running remote execution to stop.")
    client = CrossCheckClient(job.service_url)
    remote = client.json(f"/api/jobs/{execution.remote_id}")
    _validate_remote_identity(job, execution, remote)
    client.json(f"/api/jobs/{execution.remote_id}/stop", method="POST")
    _job_event("crosscheck_service_stop", job, attempt_id=str(execution.pk))


def repreview_service_results(job_id):
    with transaction.atomic():
        job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
        if not job.items.filter(status__in=["downloaded", "stale"]).exists():
            raise ValueError("Only unapplied results can be previewed again.")
        for execution in job.attempts.exclude(archive_sha256=""):
            for candidate, values in parse_service_archive(job, attempt_archive(job, execution), attempt=execution):
                item = job.items.select_for_update().get(pk=candidate.pk)
                result = execution.results.get(item=item)
                if result.applied_at or item.status == "applied":
                    continue
                if result.report_sha256 and result.report_sha256 != values.get("report_sha256"):
                    raise ValueError("Report evidence changed.")
                result.status = values["status"]
                result.message = values["message"]
                result.import_evidence = values["import_evidence"]
                result.upload_confirmed = values["upload_confirmed"]
                result.save(update_fields=["status", "message", "import_evidence", "upload_confirmed"])
                if item.report_sha256 == result.report_sha256:
                    item.status, item.message, item.import_evidence = result.status, result.message, result.import_evidence
                    item.save(update_fields=["status", "message", "import_evidence"])
        sync_batch_status(job)
        _job_event("crosscheck_service_result_preview", job)
    return job


def cancel_service_preview(job_id, evidence_token):
    with transaction.atomic():
        job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
        require_evidence_token(evidence_token, "crosscheck-service-job", job_evidence(job))
        if job.status != "preview":
            raise ValueError("Only an unsent preview can be cancelled.")
        job.status = "cancelled"
        job.save(update_fields=["status", "updated_at"])
        job.attempts.filter(number=1).update(status="cancelled")
        _job_event("crosscheck_service_preview_cancelled", job)
    return job
