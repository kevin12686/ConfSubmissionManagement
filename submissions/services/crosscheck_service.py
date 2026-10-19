"""Previewed, version-bound automatic CrossCheck jobs.

The manual export service owns PDF selection and durable provenance. Remote
results can only update the exact Final/PDF pair recorded by that service.
"""

import csv
import hashlib
import io
import json
import re
import shutil
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from submissions.models import AppSetting, CrossCheckServiceJob, CrossCheckServiceJobItem
from submissions.services.audit import audit_failure, audit_preview, audit_requested, audit_success
from submissions.services.crosscheck import (
    CROSSCHECK_EXPORT_SCOPES, _crosscheck_batch_submission, _parse_percent,
    _sha256_path, _assert_crosscheck_scope_unambiguous, prepare_crosscheck_upload,
)
from submissions.services.crosscheck_client import CrossCheckClient, CrossCheckServiceError
from submissions.services.crosscheck_client import read_service_secrets
from submissions.services.final_submission_state import bulk_update_submissions
from submissions.services.publication_read import PublicationReadContext
from submissions.services.workflow_evidence import (
    final_submission_state_evidence, make_evidence_token, require_evidence_token,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9]+$")
SAFE_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def job_root(job):
    return Path(settings.BASE_DIR) / "data" / "crosscheck_service" / str(job.pk)


def batch_prefix(conference_name, day):
    name = re.sub(r"[^A-Za-z0-9]+", "_", conference_name).strip("_")
    if not name:
        raise ValueError("Conference name must contain at least one English letter or number.")
    return f"{name}_CFM_{MONTHS[day.month - 1]}{day.day:02d}{day.year}_"


def _configuration(job=None):
    config = AppSetting.read()
    if not config.crosscheck_service_enabled:
        raise ValueError("Enable automatic CrossCheck in Settings first.")
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
        audit_failure("crosscheck_service_preview", exc)
        raise


def job_evidence(job):
    return {"id": str(job.pk), "updated_at": job.updated_at.isoformat(), "archive": job.archive_sha256, "status": job.status}


def job_evidence_token(job):
    return make_evidence_token("crosscheck-service-job", job_evidence(job))


def _plagiarism_evidence(submission):
    state = final_submission_state_evidence(submission)
    return {key: value for key, value in state.items()
            if (key.startswith(("plagiarism_", "single_percent_exception_"))
                or key in {"similarity_score", "single_similarity_score"})
            and key != "plagiarism_report_path"}


def _remote_matches(job, response):
    opts = response.get("settings", {})
    if opts.get("folder") != job.folder or opts.get("postfix") != "_" + job.token or opts.get("file_count") != job.items.count():
        raise ValueError("Remote job does not match this conference folder, batch token, and file count.")


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
        audit_failure("crosscheck_service_submit", exc, extra={"job_id": str(job.pk), "token": job.token})
        raise
    job.save(update_fields=["remote_id", "status", "message", "updated_at"])
    _job_event("crosscheck_service_started", job)
    return job


def _result_archive(job):
    path = job_root(job) / "results.zip"
    if not path.is_file() or not job.archive_sha256 or _sha256_path(path) != job.archive_sha256:
        raise ValueError("Result ZIP is missing or changed. Retrieve it again from the service.")
    return path


def refresh_service_job(job_id):
    job = CrossCheckServiceJob.objects.get(pk=job_id)
    if job.status not in {"submitting", "uncertain", "running"}:
        return job
    _configuration(job)
    client = CrossCheckClient(job.service_url)
    if not job.remote_id:
        candidates = [r for r in client.json("/api/jobs").get("jobs", [])
                      if r.get("settings", {}).get("postfix") == "_" + job.token
                      and r.get("settings", {}).get("folder") == job.folder]
        if len(candidates) != 1:
            raise ValueError("Cannot identify one remote job. Inspect service job history; do not re-upload this batch.")
        remote = candidates[0]
        _remote_matches(job, remote)
        if not SAFE_REMOTE_ID.fullmatch(str(remote.get("id", ""))):
            raise ValueError("Service returned an invalid job ID.")
        job.remote_id = remote["id"]
    response = client.json(f"/api/jobs/{job.remote_id}")
    if response.get("id") != job.remote_id:
        raise ValueError("Service returned a different job ID.")
    _remote_matches(job, response)
    progress = response.get("progress") or {}
    # Retain structured operational counts only. Raw service logs may contain
    # machine paths or authentication details; they stay inside the evidence ZIP.
    job.progress = {key: value for key, value in progress.items()
                    if key in {"phase", "completed", "total", "succeeded", "failed", "timed_out", "ready", "wait_timed_out", "downloaded", "download_failed"}
                    and isinstance(value, (str, int, float, bool))}
    job.status = "running"
    job.message = "CrossCheck is processing this batch."
    if response.get("can_download") is True:
        root = job_root(job)
        root.mkdir(parents=True, exist_ok=True)
        staged = root / f".{uuid.uuid4().hex}.zip"
        try:
            client.download(job.remote_id, staged)
            rows = parse_service_archive(job, staged)
            with transaction.atomic():
                locked = CrossCheckServiceJob.objects.select_for_update().get(pk=job.pk)
                if locked.status not in {"submitting", "uncertain", "running"}:
                    return locked
                for item, values in rows:
                    for name, value in values.items():
                        setattr(item, name, value)
                    item.save()
                job.archive_sha256 = _sha256_path(staged)
                staged.replace(root / "results.zip")
                job.status = "ready" if all(values["status"] == "downloaded" for _, values in rows) else "partial"
                job.message = "Results retrieved. Review each paper before applying."
                job.save()
                _job_event("crosscheck_service_results_retrieved", job, progress=job.progress)
        finally:
            staged.unlink(missing_ok=True)
        return job
    if response.get("status") in {"failed", "stopped", "cancelled", "completed"}:
        job.status = "failed"
        job.message = "Service job ended without a downloadable result. Inspect the service job history."
        _job_event("crosscheck_service_failed", job, progress=job.progress)
    job.save(update_fields=["remote_id", "status", "message", "progress", "updated_at"])
    return job


def _unique_rows(rows, key, label):
    result = {}
    for row in rows:
        identity = row.get(key, "")
        if not identity or identity in result:
            raise ValueError(f"Missing or duplicate {label} identity in service ZIP.")
        result[identity] = row
    return result


def parse_service_archive(job, path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError("Service ZIP has duplicate or excessive entries.")
        if sum(x.file_size for x in infos) > 4 * 1024 ** 3:
            raise ValueError("Service ZIP expanded content is too large.")
        for info in infos:
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Unsafe path in service ZIP.")
        if archive.testzip():
            raise ValueError("Service ZIP failed integrity validation.")
        def text(name):
            if archive.getinfo(name).file_size > 8 * 1024 * 1024:
                raise ValueError("Service metadata file is too large.")
            return archive.read(name).decode("utf-8-sig")
        manifest = _unique_rows(json.loads(text(".info/upload_manifest.json")), "original_name", "manifest")
        uploads = _unique_rows(csv.DictReader(io.StringIO(text(".info/upload_results.csv"))), "UploadedFile", "upload")
        batches = _unique_rows(csv.DictReader(io.StringIO(text(".info/batch_results.csv"))), "UploadedFile", "batch")
        scores = _unique_rows(csv.DictReader(io.StringIO(text("CrossCheck_output.txt")), delimiter="\t"), "FileName", "score")
        items = list(job.items.all())
        expected = {f"{x.paper_id}_{job.token}.pdf" for x in items}
        if set(manifest) != {f"{x.paper_id}.pdf" for x in items}:
            raise ValueError("Service manifest does not match the submitted Paper IDs.")
        if not set(uploads).issubset(expected) or not set(batches).issubset(expected) or not set(scores).issubset(expected):
            raise ValueError("Service ZIP contains results from a different batch.")
        rows = []
        for item in items:
            original = f"{item.paper_id}.pdf"
            uploaded = f"{item.paper_id}_{job.token}.pdf"
            if manifest[original].get("upload_name") != uploaded:
                raise ValueError("Service renamed a file outside the expected batch contract.")
            batch = batches.get(uploaded, {})
            upload = uploads.get(uploaded, {})
            if batch.get("OriginalFile", original) != original or upload.get("OriginalFile", original) != original:
                raise ValueError("Service result has a conflicting original filename.")
            values = {"status": "unavailable", "message": "No confirmed downloadable report.",
                      "document_id": batch.get("DocumentID", "")[:150], "import_evidence": {}}
            if upload.get("Status") == "success" and batch.get("Status") == "downloaded":
                score = scores.get(uploaded, {})
                p = _parse_percent(score.get("TotalSimilarity"), field_label="Plagiarism %")
                s = _parse_percent(score.get("SingleSimilarity"), field_label="Single %")
                if p is None or s is None or str(score.get("Download", "")).lower() != "true":
                    raise ValueError("Downloaded report lacks valid P/S scores.")
                candidates = [f"reports/{uploaded}.pdf", f"reports/{uploaded}"]
                found = [name for name in candidates if name in names]
                if len(found) != 1:
                    raise ValueError("A downloaded report is missing or ambiguous in the ZIP.")
                report = archive.read(found[0])
                if not report.startswith(b"%PDF-"):
                    raise ValueError("Service report is not a PDF.")
                submission, problem, _ = _crosscheck_batch_submission(item.paper_id, job.token)
                values.update(status="stale" if problem else "downloaded", message=problem or "",
                    plagiarism_percent=p, single_percent=s, report_member=found[0],
                    report_sha256=hashlib.sha256(report).hexdigest(),
                    import_evidence=_plagiarism_evidence(submission) if submission and not problem else {})
            else:
                # Stable per-file status is displayed alongside the report, without
                # copying potentially sensitive upstream free-text into our audit.
                status = str(batch.get("Status") or upload.get("Status") or "unavailable")
                message = str(batch.get("Message") or upload.get("Message") or "Service result: " + status)
                for key, secret in read_service_secrets(job.service_url).items():
                    if key.endswith("password") and secret:
                        message = message.replace(secret, "[redacted]")
                message = re.sub(r"/(?:private/)?(?:tmp|var)/\S+", "[service path]", message)
                values.update(status=status[:40], message=message[:2000])
            rows.append((item, values))
        return rows


def apply_service_results(job_id, evidence_token):
    created = []
    try:
        with transaction.atomic():
            job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
            require_evidence_token(evidence_token, "crosscheck-service-job", job_evidence(job))
            if job.status not in {"ready", "partial"}:
                raise ValueError("Results are not ready, or have already been applied.")
            _assert_crosscheck_scope_unambiguous(PublicationReadContext.load(require_stable_database=True))
            archive_path = _result_archive(job)
            changed = []
            applied = []
            with zipfile.ZipFile(archive_path) as archive:
                for item in job.items.select_for_update().all():
                    if item.status != "downloaded":
                        continue
                    submission, problem, _ = _crosscheck_batch_submission(item.paper_id, job.token, lock=True)
                    if problem or _plagiarism_evidence(submission) != item.import_evidence:
                        item.status = "stale"
                        item.message = problem or "Submission/review data changed after result preview. No result applied."
                        item.save(update_fields=["status", "message"])
                        continue
                    report = archive.read(item.report_member)
                    if hashlib.sha256(report).hexdigest() != item.report_sha256:
                        raise ValueError("Report bytes changed after preview.")
                    folder = Path(AppSetting.read().plagiarism_reports_folder)
                    if not folder.is_absolute():
                        folder = Path(settings.BASE_DIR) / folder
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / f"{item.paper_id}_{job.token}_{uuid.uuid4().hex}_report.pdf"
                    created.append(target)
                    with target.open("xb") as handle:
                        handle.write(report)
                    before = {"plagiarism_percent": str(submission.similarity_score), "single_percent": str(submission.single_similarity_score)}
                    submission.similarity_score = item.plagiarism_percent
                    submission.single_similarity_score = item.single_percent
                    submission.plagiarism_report_path = str(target)
                    submission.plagiarism_report_stale = False
                    submission.plagiarism_imported_at = timezone.now()
                    changed.append(submission)
                    item.status = "applied"
                    item.message = "Scores and report applied to the verified publication PDF."
                    item.save(update_fields=["status", "message"])
                    applied.append({"paper_id": item.paper_id, "final_submission_id": item.final_submission_id,
                                    "pdf_sha256": item.pdf_sha256, "before": before,
                                    "after": {"plagiarism_percent": str(item.plagiarism_percent), "single_percent": str(item.single_percent)},
                                    "report_sha256": item.report_sha256})
                    audit_success("crosscheck_service_item_apply", "Version-bound CrossCheck scores and report applied.",
                        submission=submission, before=before,
                        after={"plagiarism_percent": str(item.plagiarism_percent), "single_percent": str(item.single_percent)},
                        file_hashes={"publication_pdf": item.pdf_sha256, "report": item.report_sha256},
                        extra={"job_id": str(job.pk), "token": job.token})
            bulk_update_submissions(changed, ["similarity_score", "single_similarity_score", "plagiarism_report_path", "plagiarism_report_stale", "plagiarism_imported_at"])
            job.status = "applied"
            job.imported_at = timezone.now()
            job.message = f"Applied {len(applied)} paper(s); unavailable or stale items were left unchanged."
            job.save(update_fields=["status", "imported_at", "message", "updated_at"])
            _job_event("crosscheck_service_result_apply", job, applied=applied)
        return job
    except Exception as exc:
        for path in created:
            path.unlink(missing_ok=True)
        audit_failure("crosscheck_service_result_apply", exc, extra={"job_id": str(job_id)})
        raise


def stop_service_job(job_id):
    job = CrossCheckServiceJob.objects.get(pk=job_id)
    if job.status not in {"running", "submitting", "uncertain"} or not job.remote_id:
        raise ValueError("This batch has no running remote job to stop.")
    client = CrossCheckClient(job.service_url)
    remote = client.json(f"/api/jobs/{job.remote_id}")
    _remote_matches(job, remote)
    client.json(f"/api/jobs/{job.remote_id}/stop", method="POST")
    _job_event("crosscheck_service_stop", job)


def repreview_service_results(job_id):
    with transaction.atomic():
        job = CrossCheckServiceJob.objects.select_for_update().get(pk=job_id)
        if job.status not in {"ready", "partial"}:
            raise ValueError("Only unapplied results can be previewed again.")
        for item, values in parse_service_archive(job, _result_archive(job)):
            for key, value in values.items():
                setattr(item, key, value)
            item.save()
        job.save(update_fields=["updated_at"])
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
        _job_event("crosscheck_service_preview_cancelled", job)
    return job
