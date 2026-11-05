"""Immutable execution/result evidence for a single CrossCheck upload batch."""

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from submissions.models import CrossCheckServiceAttempt, CrossCheckServiceJob, CrossCheckServiceResult
from submissions.services.audit import audit_success
from submissions.services.crosscheck import _crosscheck_batch_submission, _parse_percent, _sha256_path
from submissions.services.crosscheck_client import read_service_secrets
from submissions.services.workflow_evidence import final_submission_state_evidence, make_evidence_token

ACTIVE_ATTEMPT_STATES = ("submitting", "uncertain", "running", "pending_download")
SAFE_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
ITEM_FIELDS = ("status", "message", "document_id", "plagiarism_percent", "single_percent",
               "report_member", "report_sha256", "import_evidence")


def job_root(job):
    return Path(settings.BASE_DIR) / "data/crosscheck_service" / str(job.pk)


def initial_attempt(job):
    return job.attempts.get(number=1)


def active_attempt(job):
    return job.attempts.filter(status__in=ACTIVE_ATTEMPT_STATES).first()


def archive_path(job, attempt):
    if attempt.job_id != job.pk:
        raise ValueError("Execution belongs to another batch.")
    root = job_root(job)
    path = root / "results.zip" if attempt.number == 1 else root / "attempts" / str(attempt.pk) / "results.zip"
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Unsafe result evidence path.")
    return path


def attempt_archive(job, attempt):
    path = archive_path(job, attempt)
    if not path.is_file() or not attempt.archive_sha256 or _sha256_path(path) != attempt.archive_sha256:
        raise ValueError("Result ZIP is missing or changed. No result can be applied.")
    return path


def plagiarism_evidence(submission):
    state = final_submission_state_evidence(submission)
    return {key: value for key, value in state.items()
            if (key.startswith(("plagiarism_", "single_percent_exception_"))
                or key in {"similarity_score", "single_similarity_score"})
            and key != "plagiarism_report_path"}


def job_evidence(job):
    # Operational polling must not invalidate a review of unchanged results.
    return {"id": str(job.pk), "token": job.token, "service_url": job.service_url,
        "folder": job.folder, "conference": job.conference_name,
        "scope": job.scope, "wait_minutes": job.wait_minutes, "created_at": job.created_at.isoformat(),
        "phase": job.status if job.status in {"preview", "cancelled"} else "submitted",
        "attempts": [{"id": str(x.pk), "remote": x.remote_id, "archive": x.archive_sha256,
                      "kind": x.kind, "number": x.number, "parent": str(x.parent_id) if x.parent_id else None,
                      "eligible": x.eligible_paper_ids, "wait_minutes": x.wait_minutes,
                      "phase": x.status if x.status in {"preview", "submitting", "uncertain", "cancelled"} else "accepted"}
                     for x in job.attempts.all()],
        "items": list(job.items.values("id", "paper_id", "final_submission_id", "pdf_sha256", *ITEM_FIELDS))}


def job_evidence_token(job):
    return make_evidence_token("crosscheck-service-job", job_evidence(job))


def remote_matches(job, response, attempt=None):
    if not isinstance(response, dict) or not isinstance(response.get("settings"), dict):
        raise ValueError("Service returned invalid job metadata.")
    opts = response["settings"]
    if opts.get("folder") != job.folder or opts.get("postfix") != "_" + job.token:
        raise ValueError("Remote job does not match this conference folder and batch token.")
    count = opts.get("file_count")
    if type(count) is not int or count < 1:
        raise ValueError("Service returned an invalid file count.")
    if attempt is None or attempt.kind == "upload":
        if response.get("kind") == "retry_download" or opts.get("mode") == "retry_download" or count != job.items.count():
            raise ValueError("Remote job does not match this batch's file count.")
    else:
        parent = attempt.parent
        original = initial_attempt(job)
        if (not parent or parent.job_id != job.pk or response.get("kind") != "retry_download"
                or opts.get("mode") != "retry_download"
                or opts.get("source_job_id") != original.remote_id
                or opts.get("parent_job_id") != parent.remote_id
                or count > len(attempt.eligible_paper_ids)
                or (attempt.remote_file_count is not None and count != attempt.remote_file_count)):
            raise ValueError("Retry execution does not match its original batch, parent and allowed papers.")
    return count


def _unique_rows(rows, key, label):
    result = {}
    for row in rows:
        identity = row.get(key, "")
        if not identity or identity in result:
            raise ValueError(f"Missing or duplicate {label} identity in service ZIP.")
        result[identity] = row
    return result


def parse_service_archive(job, path, *, attempt=None):
    attempt = attempt or initial_attempt(job)
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
        all_items = {x.paper_id: x for x in job.items.all()}
        selected = {name[:-4] for name in manifest if name.endswith(".pdf")}
        if len(selected) != len(manifest) or not selected:
            raise ValueError("Service manifest has invalid Paper IDs.")
        if attempt.kind == "upload":
            if selected != set(all_items):
                raise ValueError("Service manifest does not match the submitted Paper IDs.")
        elif (not selected.issubset(set(attempt.eligible_paper_ids)) or not selected.issubset(all_items)
              or len(selected) != attempt.remote_file_count):
            raise ValueError("Retry manifest does not match its allowed papers and remote file count.")
        expected = {f"{paper}_{job.token}.pdf" for paper in selected}
        if any(not set(rows).issubset(expected) for rows in (uploads, batches, scores)):
            raise ValueError("Service ZIP contains results from a different batch.")
        expected_reports = {f"reports/{name}{suffix}" for name in expected for suffix in ("", ".pdf")}
        if any(not entry.is_dir() and entry.filename.startswith("reports/") and entry.filename not in expected_reports for entry in infos):
            raise ValueError("Service ZIP contains a report outside this execution.")
        rows = []
        for paper_id in sorted(selected):
            item = all_items[paper_id]
            original = f"{paper_id}.pdf"
            uploaded = f"{paper_id}_{job.token}.pdf"
            if manifest[original].get("upload_name") != uploaded:
                raise ValueError("Service renamed a file outside the expected batch contract.")
            batch, upload = batches.get(uploaded, {}), uploads.get(uploaded, {})
            if batch.get("OriginalFile", original) != original or upload.get("OriginalFile", original) != original:
                raise ValueError("Service result has a conflicting original filename.")
            confirmed = upload.get("Status") == "success"
            # Retry permission is captured from the verified original upload ZIP.
            if attempt.kind != "upload" and not upload:
                confirmed = paper_id in attempt.eligible_paper_ids
            values = {"status": "unavailable", "message": "No confirmed downloadable report.",
                      "upload_confirmed": confirmed, "document_id": batch.get("DocumentID", "")[:150],
                      "import_evidence": {}}
            if confirmed and batch.get("Status") == "downloaded":
                if attempt.kind == "retry_download" and item.document_id and batch.get("DocumentID") != item.document_id:
                    raise ValueError("Retry report does not match the original manuscript identity.")
                score = scores.get(uploaded, {})
                p = _parse_percent(score.get("TotalSimilarity"), field_label="Plagiarism %")
                s = _parse_percent(score.get("SingleSimilarity"), field_label="Single %")
                if p is None or s is None or str(score.get("Download", "")).lower() != "true":
                    raise ValueError("Downloaded report lacks valid P/S scores.")
                found = [name for name in (f"reports/{uploaded}.pdf", f"reports/{uploaded}") if name in names]
                if len(found) != 1:
                    raise ValueError("A downloaded report is missing or ambiguous in the ZIP.")
                report = archive.read(found[0])
                if not report.startswith(b"%PDF-"):
                    raise ValueError("Service report is not a PDF.")
                digest = hashlib.sha256(report).hexdigest()
                if attempt.kind != "upload" and item.report_sha256 and item.report_sha256 != digest:
                    raise ValueError("Retry tried to replace a report already retained for this paper.")
                submission, problem, _ = _crosscheck_batch_submission(paper_id, job.token)
                values.update(status="stale" if problem else "downloaded", message=problem or "",
                    plagiarism_percent=p, single_percent=s, report_member=found[0], report_sha256=digest,
                    import_evidence=plagiarism_evidence(submission) if submission and not problem else {})
                if item.report_sha256 and attempt.kind != "upload":
                    owner = item.results.filter(report_sha256=digest).first()
                    if not owner or owner.attempt_id != attempt.pk:
                        values.update(status="retained", message="This report is already retained; no values replaced.")
            else:
                status = (batch.get("Status") or "unavailable") if confirmed else (upload.get("Status") or "unconfirmed")
                if not confirmed and status in {"success", "downloaded"}:
                    status = "unconfirmed"
                message = str(batch.get("Message") or upload.get("Message") or "Service result: " + status)
                for key, secret in read_service_secrets(job.service_url).items():
                    if key.endswith("password") and secret:
                        message = message.replace(secret, "[redacted]")
                values.update(status=str(status)[:40], message=re.sub(r"/(?:private/)?(?:tmp|var)/\S+", "[service path]", message)[:2000])
            rows.append((item, values))
        return rows


def confirmed_upload_ids(job):
    original = initial_attempt(job)
    rows = parse_service_archive(job, attempt_archive(job, original), attempt=original)
    return {item.paper_id for item, values in rows if values["upload_confirmed"]}


def current_result(job, item):
    result = item.results.filter(report_sha256=item.report_sha256).exclude(report_sha256="").select_related("attempt").first()
    if not result or result.attempt.job_id != job.pk or item.job_id != job.pk:
        raise ValueError("Report bytes changed after preview or are not bound to this batch and paper.")
    return result


def result_pdf_bytes(job, result):
    if result.attempt.job_id != job.pk or result.item.job_id != job.pk:
        raise ValueError("Report evidence belongs to another batch.")
    with zipfile.ZipFile(attempt_archive(job, result.attempt)) as archive:
        report = archive.read(result.report_member)
    if not result.report_sha256 or hashlib.sha256(report).hexdigest() != result.report_sha256:
        raise ValueError("Report bytes changed after preview.")
    return report


def sync_batch_status(job):
    active = active_attempt(job)
    statuses = list(job.items.values_list("status", flat=True))
    if active:
        job.status = "running" if active.status == "pending_download" else active.status
        job.progress = active.progress
    elif statuses and all(value == "applied" for value in statuses):
        job.status = "applied"
    elif job.attempts.filter(archive_sha256__gt="").exists():
        job.status = "ready" if statuses and all(value == "downloaded" for value in statuses) else "partial"
    job.save(update_fields=["status", "progress", "updated_at"])


def retry_candidates(job):
    confirmed = confirmed_upload_ids(job)
    eligible = sorted(confirmed - set(job.items.exclude(report_sha256="").values_list("paper_id", flat=True)))
    stale = [paper for paper in eligible if _crosscheck_batch_submission(paper, job.token)[1]]
    return {"paper_ids": eligible, "stale_paper_ids": stale,
            "all_stale": bool(eligible) and len(eligible) == len(stale)}


def batch_context(job):
    runs = list(job.attempts.prefetch_related("results__item"))
    items = list(job.items.prefetch_related("results__attempt"))
    active = next((run for run in runs if run.status in ACTIVE_ATTEMPT_STATES), None)
    counts = {"total": len(items), "applied": 0, "ready": 0, "waiting": 0, "attention": 0}
    labels = {"prepared": "Awaiting upload" if job.status == "preview" else "Awaiting results",
              "downloaded": "Ready to apply", "applied": "Applied",
              "wait_timeout": "Awaiting report", "download_failed": "Download failed",
              "unconfirmed": "Upload unconfirmed", "timed_out": "Upload unconfirmed",
              "report_error": "Website report error", "stale": "Stale result", "retained": "Already retained"}
    for item in items:
        item.report_result = next((result for result in item.results.all()
                                   if result.report_sha256 and result.report_sha256 == item.report_sha256), None)
        if item.status == "applied":
            category = "applied"
        elif item.status == "downloaded" and item.report_result:
            category = "ready"
        elif item.status in {"prepared", "wait_timeout", "waiting", "unavailable", "download_failed"}:
            category = "waiting"
        else:
            category = "attention"
        counts[category] += 1
        item.status_label = labels.get(item.status, item.status.replace("_", " ").capitalize())
        item.status_level = {"applied": "success", "ready": "primary", "waiting": "secondary", "attention": "warning"}[category]
    for run in runs:
        run.label = "Original upload" if run.number == 1 else f"Retry {run.number - 1}"
        run.result_count = sum(bool(result.report_sha256) for result in run.results.all())
        for result in run.results.all():
            result.status_label = labels.get(result.status, result.status.replace("_", " ").capitalize())
    latest = next((run for run in reversed(runs) if run.remote_id), None)
    availability = latest.availability if latest else {}
    display_status = batch_status_label(job, counts, active)
    return {"job": job, "items": items, "attempts": runs, "active_attempt": active,
            "counts": counts, "display_status": display_status, "availability": availability,
            "is_running": bool(active), "poll_enabled": bool(active and active.status in {"running", "pending_download"}),
            "can_check": bool(active or latest), "can_apply": bool(counts["ready"]),
            "can_repreview": any(item.status in {"downloaded", "stale"} for item in items),
            "can_retry": bool(not active and availability.get("can_retry") and availability.get("remaining")),
            "retry_wait_minutes": latest.wait_minutes if latest else job.wait_minutes,
            "job_evidence_token": job_evidence_token(job)}


def batch_status_label(job, counts, active):
    if active:
        display_status = "In progress" if active.status in {"running", "pending_download"} else "Connection needs confirmation"
    elif job.status == "preview":
        display_status = "Upload preview"
    elif job.status == "cancelled":
        display_status = "Cancelled"
    elif counts["total"] and counts["applied"] == counts["total"]:
        display_status = "Completed"
    elif counts["ready"]:
        display_status = "Results ready to review"
    elif counts["attention"]:
        display_status = "Needs attention"
    else:
        display_status = "Reports still missing"
    return display_status


def record_attempt_results(job, attempt, staged, rows):
    target = archive_path(job, attempt)
    digest = _sha256_path(staged)
    promoted = False
    try:
        with transaction.atomic():
            job = CrossCheckServiceJob.objects.select_for_update().get(pk=job.pk)
            attempt = job.attempts.select_for_update().get(pk=attempt.pk)
            if attempt.archive_sha256:
                attempt_archive(job, attempt)
                return job
            if target.exists():
                raise ValueError("Execution evidence already exists; it cannot be overwritten.")
            for candidate, values in rows:
                item = job.items.select_for_update().get(pk=candidate.pk)
                CrossCheckServiceResult.objects.create(attempt=attempt, item=item, **values)
                if not item.report_sha256 and item.status != "applied":
                    for name in ITEM_FIELDS:
                        if name in values:
                            setattr(item, name, values[name])
                    item.save(update_fields=list(ITEM_FIELDS))
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
            promoted = True
            attempt.archive_sha256 = digest
            attempt.status = "received"
            attempt.message = "Results retrieved; available reports require confirmation."
            attempt.save()
            job.message = f"Execution {attempt.number} retrieved. Review new results; earlier results are retained."
            if attempt.number == 1:
                job.archive_sha256 = digest
            job.save(update_fields=["message", "archive_sha256", "updated_at"])
            sync_batch_status(job)
            audit_success("crosscheck_service_results_retrieved", "Execution evidence retained without replacing earlier results.",
                extra={"job_id": str(job.pk), "attempt_id": str(attempt.pk), "remote_id": attempt.remote_id},
                file_hashes={"archive": digest}, result_counts={"papers": len(rows)})
        return job
    except Exception:
        if promoted:
            target.unlink(missing_ok=True)
        raise
