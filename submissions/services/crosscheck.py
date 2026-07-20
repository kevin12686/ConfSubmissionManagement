import csv
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import uuid
import zipfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from submissions.models import AppSetting, FinalSubmission, InitialPaper
from submissions.services.audit import audit_failure, audit_success
from submissions.services.file_manager import publication_pdf_info, resolve_folder
from submissions.services.final_submission_state import bulk_update_submissions
from submissions.services.import_export import clean_value, normalize_columns, read_table, round_percent
from submissions.services.publication_read import PublicationReadContext


CROSSCHECK_RESULT_TEMPLATE_COLUMNS = [
    "filename",
    "plagiarism_percent",
    "single_percent",
]

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
CROSSCHECK_EXPORT_ALL = "all"
CROSSCHECK_EXPORT_MISSING_RESULTS = "missing_results"
CROSSCHECK_EXPORT_SCOPES = {
    CROSSCHECK_EXPORT_ALL,
    CROSSCHECK_EXPORT_MISSING_RESULTS,
}
PROVENANCE_SCHEMA_VERSION = 1


def validate_token(token):
    token = clean_value(token)
    if not token:
        raise ValueError("Batch token is required.")
    if not TOKEN_PATTERN.match(token):
        raise ValueError("Batch token may only contain letters, numbers, underscore, or hyphen.")
    return token


def parse_crosscheck_filename(filename):
    stem = Path(clean_value(filename)).stem
    if "_" not in stem:
        return None, None, "Filename must use PaperID_token format."
    paper_id, token = stem.split("_", 1)
    if not paper_id:
        return None, None, "Paper ID is missing before underscore."
    if not token:
        return None, None, "Token is missing after underscore."
    if "_" in paper_id:
        return None, None, "Paper ID may not contain underscore."
    return paper_id, token, ""


def crosscheck_export_root():
    return resolve_folder("data/crosscheck_upload")


def crosscheck_provenance_root():
    return crosscheck_export_root().parent / "crosscheck_provenance"


def prepare_crosscheck_upload(token, scope=CROSSCHECK_EXPORT_ALL):
    promoted_paths = []
    staging_dir = None
    try:
        token = validate_token(token)
        if scope not in CROSSCHECK_EXPORT_SCOPES:
            raise ValueError("Unknown CrossCheck export scope.")
        provenance_path = _claim_crosscheck_scope(token, scope)
        target_dir = _crosscheck_export_dir(token, scope)

        manifest_path = target_dir / _crosscheck_manifest_name(token, scope)
        zip_path = target_dir / _crosscheck_zip_name(token, scope)
        if _scope_artifacts_exist(target_dir, scope):
            raise ValueError(
                f"CrossCheck batch token '{token}' has already been used for this scope. "
                "Use a new token so returned results cannot be attached to a different batch."
            )
        exported = []
        skipped = []
        staged_pdfs = {}
        fieldnames = [
            "export_scope",
            "paper_id",
            "final_submission_id",
            "source_publication_pdf",
            "exported_filename",
            "publication_pdf_sha256",
        ]
        if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
            fieldnames.extend(["missing_plagiarism_percent", "missing_single_percent"])

        context = PublicationReadContext.load(require_stable_database=True)
        _assert_crosscheck_scope_unambiguous(context)
        operation_id = uuid.uuid4().hex
        staging_dir = (
            crosscheck_export_root()
            / ".staging"
            / f"{token}-{scope}-{operation_id}"
        )
        staging_dir.mkdir(parents=True, exist_ok=False)
        submissions = context.master_submissions
        if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
            submissions = tuple(
                submission
                for submission in submissions
                if submission.similarity_score is None
                or submission.single_similarity_score is None
            )
        for submission in sorted(submissions, key=lambda item: item.paper_id_filled):
            paper_id = clean_value(submission.paper_id_filled)
            if not paper_id:
                skipped.append(_skip_row(submission, "Missing Paper ID."))
                continue
            if "_" in paper_id:
                skipped.append(_skip_row(submission, "Paper ID contains underscore."))
                continue
            publication_pdf = publication_pdf_info(
                submission,
                context.file_inspection,
            )
            if not publication_pdf["exists"]:
                skipped.append(_skip_row(submission, "No publication PDF."))
                continue

            pdf_bytes = context.file_inspection.read_snapshot_bytes(
                publication_pdf["path"]
            )
            exported_filename = f"{paper_id}_{token}.pdf"
            staged_pdf = staging_dir / exported_filename
            staged_pdf.write_bytes(pdf_bytes)
            staged_pdfs[exported_filename] = staged_pdf
            row = {
                "export_scope": scope,
                "paper_id": paper_id,
                "final_submission_id": submission.final_submission_id,
                "source_publication_pdf": publication_pdf["path"],
                "exported_filename": exported_filename,
                "publication_pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            }
            if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
                row.update(
                    {
                        "missing_plagiarism_percent": submission.similarity_score is None,
                        "missing_single_percent": submission.single_similarity_score is None,
                    }
                )
            exported.append(row)

        if not exported:
            raise ValueError(
                "No unambiguous publication PDFs are available for this CrossCheck scope."
            )
        exported_names = [row["exported_filename"] for row in exported]
        if len(exported_names) != len(set(exported_names)):
            raise ValueError(
                "CrossCheck export filenames are not unique. Resolve active-version "
                "or Paper ID conflicts before preparing a batch."
            )
        _assert_token_bindings_compatible(token, scope, exported)

        manifest_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(manifest_buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(exported)
        manifest_bytes = ("\ufeff" + manifest_buffer.getvalue()).encode("utf-8")
        staged_manifest = staging_dir / manifest_path.name
        staged_zip = staging_dir / zip_path.name
        staged_manifest.write_bytes(manifest_bytes)

        with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for row in exported:
                archive.write(
                    staged_pdfs[row["exported_filename"]],
                    row["exported_filename"],
                )
            archive.writestr(manifest_path.name, manifest_bytes)
        with zipfile.ZipFile(staged_zip) as archive:
            if archive.testzip() is not None:
                raise ValueError("CrossCheck ZIP integrity validation failed.")
        context.assert_database_unchanged()
        _complete_crosscheck_scope(provenance_path, token, scope, exported)

        target_dir.mkdir(parents=True, exist_ok=True)
        for row in exported:
            target_pdf = target_dir / row["exported_filename"]
            staged_pdf = staged_pdfs[row["exported_filename"]]
            os.replace(staged_pdf, target_pdf)
            promoted_paths.append(target_pdf)
        os.replace(staged_manifest, manifest_path)
        promoted_paths.append(manifest_path)
        os.replace(staged_zip, zip_path)
        promoted_paths.append(zip_path)

        result = {
            "token": token,
            "scope": scope,
            "scope_label": _crosscheck_scope_label(scope),
            "target_dir": str(target_dir),
            "zip_path": str(zip_path),
            "zip_filename": zip_path.name,
            "download_url": "",
            "manifest_path": str(manifest_path),
            "exported_count": len(exported),
            "skipped_count": len(skipped),
            "skipped": skipped,
        }
        audit_success(
            "crosscheck_export",
            "CrossCheck upload ZIP prepared.",
            result_counts={
                "exported_count": len(exported),
                "skipped_count": len(skipped),
            },
            file_changes={"zip_path": str(zip_path), "manifest_path": str(manifest_path)},
            extra={"token": token, "scope": scope, "skipped": skipped[:20]},
        )
        return result
    except Exception as exc:
        for path in reversed(promoted_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        audit_failure(
            "crosscheck_export",
            exc,
            "CrossCheck upload ZIP preparation failed.",
            extra={"token": token, "scope": scope},
        )
        raise
    finally:
        if staging_dir is not None:
            staging_parent = staging_dir.parent
            shutil.rmtree(staging_dir, ignore_errors=True)
            try:
                staging_parent.rmdir()
            except OSError:
                pass


def _assert_crosscheck_scope_unambiguous(context):
    mixed_groups = context.mixed_publication_decision_groups
    if mixed_groups:
        paper_ids = ", ".join(sorted(mixed_groups))
        raise ValueError(
            "CrossCheck export blocked by mixed Not Publishing decisions for: "
            f"{paper_ids}. Resolve the publication decision for every version first."
        )
    active_groups = {}
    for submission in context.active_submissions:
        if submission.paper_id_filled not in context.valid_paper_ids:
            continue
        active_groups.setdefault(submission.paper_id_filled, []).append(submission)
    conflicts = {
        paper_id: submissions
        for paper_id, submissions in active_groups.items()
        if len(submissions) > 1
    }
    if conflicts:
        details = "; ".join(
            f"{paper_id}: {', '.join(sorted(item.final_submission_id for item in items))}"
            for paper_id, items in sorted(conflicts.items())
        )
        raise ValueError(
            "CrossCheck export blocked because multiple active Final Submissions "
            f"would produce ambiguous files ({details})."
        )


def _staged_path(target, operation_id):
    return target.with_name(f".{target.name}.{operation_id}.part")


def _scope_artifacts_exist(target_dir, scope):
    if not target_dir.exists():
        return False
    if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
        return any(target_dir.iterdir())
    return any(path.name != "missing" for path in target_dir.iterdir())


def crosscheck_zip_path(token, scope=CROSSCHECK_EXPORT_ALL):
    token = validate_token(token)
    if scope not in CROSSCHECK_EXPORT_SCOPES:
        raise ValueError("Unknown CrossCheck export scope.")
    return _crosscheck_export_dir(token, scope) / _crosscheck_zip_name(token, scope)


def _crosscheck_export_dir(token, scope):
    root = crosscheck_export_root() / token
    if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
        return root / "missing"
    return root


def _crosscheck_zip_name(token, scope):
    if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
        return f"crosscheck_missing_upload_{token}.zip"
    return f"crosscheck_upload_{token}.zip"


def _crosscheck_manifest_name(token, scope):
    if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
        return f"crosscheck_missing_manifest_{token}.csv"
    return f"crosscheck_manifest_{token}.csv"


def _crosscheck_scope_label(scope):
    if scope == CROSSCHECK_EXPORT_MISSING_RESULTS:
        return "Missing CrossCheck results only"
    return "All publication PDFs"


def _provenance_path(token, scope):
    return crosscheck_provenance_root() / token / f"{scope}.json"


def _claim_crosscheck_scope(token, scope):
    path = _provenance_path(token, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "status": "claimed",
        "token": token,
        "scope": scope,
        "claimed_at": timezone.now().isoformat(),
    }
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(claim, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValueError(
            f"CrossCheck batch token '{token}' has already been used for this scope. "
            "Export cleanup does not release batch tokens; use a new token."
        ) from exc
    return path


def _complete_crosscheck_scope(path, token, scope, rows):
    payload = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "status": "complete",
        "token": token,
        "scope": scope,
        "completed_at": timezone.now().isoformat(),
        "rows": [
            {
                "paper_id": clean_value(row["paper_id"]),
                "final_submission_id": clean_value(row["final_submission_id"]),
                "exported_filename": clean_value(row["exported_filename"]),
                "publication_pdf_sha256": clean_value(
                    row["publication_pdf_sha256"]
                ).lower(),
            }
            for row in rows
        ],
    }
    payload["signature"] = _provenance_signature(payload)
    staged = _staged_path(path, uuid.uuid4().hex)
    try:
        staged.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _provenance_signature(payload):
    unsigned = {
        key: value
        for key, value in payload.items()
        if key != "signature"
    }
    encoded = json.dumps(
        unsigned,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        django_settings.SECRET_KEY.encode("utf-8"),
        encoded,
        hashlib.sha256,
    ).hexdigest()


def _read_provenance(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"CrossCheck provenance cannot be read: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION
        or payload.get("status") != "complete"
        or payload.get("token") != path.parent.name
        or payload.get("scope") != path.stem
        or not isinstance(payload.get("rows"), list)
        or not hmac.compare_digest(
            clean_value(payload.get("signature")),
            _provenance_signature(payload),
        )
    ):
        raise ValueError(
            "CrossCheck provenance is incomplete or failed integrity validation."
        )
    return payload


def _token_provenance_payloads(token, *, ignored_scopes=frozenset()):
    root = crosscheck_provenance_root() / token
    payloads = []
    for scope in sorted(CROSSCHECK_EXPORT_SCOPES):
        if scope in ignored_scopes:
            continue
        path = root / f"{scope}.json"
        if not path.exists():
            continue
        payloads.append(_read_provenance(path))
    return payloads


def _assert_token_bindings_compatible(token, current_scope, rows):
    existing = {}
    for payload in _token_provenance_payloads(
        token,
        ignored_scopes={current_scope},
    ):
        for row in payload["rows"]:
            paper_id = clean_value(row.get("paper_id"))
            identity = (
                clean_value(row.get("final_submission_id")),
                clean_value(row.get("publication_pdf_sha256")).lower(),
            )
            previous = existing.setdefault(paper_id, identity)
            if previous != identity:
                raise ValueError(
                    "Existing CrossCheck provenance contains conflicting bindings "
                    f"for Paper ID {paper_id}."
                )
    for row in rows:
        paper_id = clean_value(row["paper_id"])
        identity = (
            clean_value(row["final_submission_id"]),
            clean_value(row["publication_pdf_sha256"]).lower(),
        )
        if paper_id in existing and existing[paper_id] != identity:
            raise ValueError(
                f"CrossCheck token '{token}' is already bound to a different "
                f"Final Submission or publication PDF for Paper ID {paper_id}. "
                "Use a new token."
            )


@transaction.atomic
def import_crosscheck_results(uploaded_file):
    try:
        frame = normalize_columns(read_table(uploaded_file))
        missing_columns = [
            column
            for column in CROSSCHECK_RESULT_TEMPLATE_COLUMNS
            if column not in frame.columns
        ]
        if missing_columns:
            raise ValueError(
                "CrossCheck result file is missing required columns: "
                + ", ".join(missing_columns)
            )
        updated = 0
        invalid = []
        unmatched = []
        stale = []
        changed_submissions = []
        applied = []
        parsed_rows = []
        provenance_cache = {}
        for index, row in enumerate(frame.to_dict("records"), start=2):
            filename = clean_value(row.get("filename"))
            paper_id, token, error = parse_crosscheck_filename(filename)
            if error:
                invalid.append({"row": index, "filename": filename, "message": error})
                continue
            parsed_rows.append((index, row, filename, paper_id, token))

        duplicate_keys = {
            key
            for key, count in Counter(
                (paper_id, token)
                for _index, _row, _filename, paper_id, token in parsed_rows
            ).items()
            if count > 1
        }
        for index, row, filename, paper_id, token in parsed_rows:
            if (paper_id, token) in duplicate_keys:
                invalid.append(
                    {
                        "row": index,
                        "filename": filename,
                        "message": (
                            "The same Paper ID and CrossCheck batch token appears more "
                            "than once. Remove duplicate rows before importing."
                        ),
                    }
                )
                continue

            submission, resolution_error, is_stale = _crosscheck_batch_submission(
                paper_id,
                token,
                lock=True,
                provenance_cache=provenance_cache,
            )
            if resolution_error:
                target = stale if is_stale else unmatched
                target.append(
                    {
                        "row": index,
                        "filename": filename,
                        "paper_id": paper_id,
                        "token": token,
                        "message": resolution_error,
                    }
                )
                continue
            if not submission:
                unmatched.append({"row": index, "filename": filename, "paper_id": paper_id})
                continue

            try:
                plagiarism_percent = _parse_percent(
                    row.get("plagiarism_percent"),
                    field_label="Plagiarism %",
                )
                single_percent = _parse_percent(
                    row.get("single_percent"),
                    field_label="Single %",
                )
            except ValueError as exc:
                invalid.append(
                    {
                        "row": index,
                        "filename": filename,
                        "message": str(exc),
                    }
                )
                continue
            previous_plagiarism_percent = submission.similarity_score
            previous_single_percent = submission.single_similarity_score
            score_changed = (
                submission.similarity_score != plagiarism_percent
                or submission.single_similarity_score != single_percent
            )
            submission.similarity_score = plagiarism_percent
            submission.single_similarity_score = single_percent
            if score_changed and submission.plagiarism_report_path:
                submission.plagiarism_report_stale = True
            submission.plagiarism_imported_at = timezone.now()
            changed_submissions.append(submission)
            applied.append(
                {
                    "paper_id": paper_id,
                    "token": token,
                    "final_submission_id": submission.final_submission_id,
                    "before_plagiarism_percent": (
                        str(previous_plagiarism_percent)
                        if previous_plagiarism_percent is not None
                        else None
                    ),
                    "before_single_percent": (
                        str(previous_single_percent)
                        if previous_single_percent is not None
                        else None
                    ),
                    "plagiarism_percent": (
                        str(plagiarism_percent)
                        if plagiarism_percent is not None
                        else None
                    ),
                    "single_percent": (
                        str(single_percent)
                        if single_percent is not None
                        else None
                    ),
                }
            )
            updated += 1

        bulk_update_submissions(
            changed_submissions,
            [
                "similarity_score",
                "single_similarity_score",
                "plagiarism_report_stale",
                "plagiarism_imported_at",
            ],
        )
        result = {
            "updated": updated,
            "invalid": invalid,
            "unmatched": unmatched,
            "stale": stale,
        }
        audit_success(
            "crosscheck_result_import",
            "CrossCheck result CSV imported.",
            result_counts={
                "updated": updated,
                "invalid": len(invalid),
                "unmatched": len(unmatched),
                "stale": len(stale),
            },
            extra={
                "invalid": invalid[:20],
                "unmatched": unmatched[:20],
                "stale": stale[:20],
                "applied": applied[:20],
            },
        )
        return result
    except Exception as exc:
        audit_failure("crosscheck_result_import", exc, "CrossCheck result CSV import failed.")
        raise


def upload_crosscheck_reports(files):
    staged_items = []
    promoted_items = []
    try:
        report_dir = resolve_folder(AppSetting.load().plagiarism_reports_folder)
        report_dir.mkdir(parents=True, exist_ok=True)
        updated = 0
        invalid = []
        unmatched = []
        stale = []
        changed_submissions = []
        applied = []
        parsed_files = []
        provenance_cache = {}
        for file_obj in files:
            filename = Path(getattr(file_obj, "name", "")).name
            paper_id, token, error = parse_crosscheck_filename(filename)
            if error or Path(filename).suffix.lower() != ".pdf":
                invalid.append({"filename": filename, "message": error or "Report must be a PDF."})
                continue
            parsed_files.append((file_obj, filename, paper_id, token))

        duplicate_keys = {
            key
            for key, count in Counter(
                (paper_id, token)
                for _file_obj, _filename, paper_id, token in parsed_files
            ).items()
            if count > 1
        }
        for file_obj, filename, paper_id, token in parsed_files:
            if (paper_id, token) in duplicate_keys:
                invalid.append(
                    {
                        "filename": filename,
                        "message": (
                            "The same Paper ID and CrossCheck batch token appears more "
                            "than once. Remove duplicate report files before uploading."
                        ),
                    }
                )
                continue
            target = report_dir / filename
            operation_id = uuid.uuid4().hex
            staged = _staged_path(target, operation_id)
            digest = hashlib.sha256()
            with staged.open("xb") as output:
                for chunk in file_obj.chunks():
                    digest.update(chunk)
                    output.write(chunk)
            staged_items.append(
                {
                    "filename": filename,
                    "paper_id": paper_id,
                    "token": token,
                    "target": target,
                    "staged": staged,
                    "backup": target.with_name(
                        f".{target.name}.{operation_id}.backup"
                    ),
                    "report_sha256": digest.hexdigest(),
                    "promoted": False,
                    "had_original": False,
                }
            )

        try:
            with transaction.atomic():
                resolved_items = []
                for item in staged_items:
                    submission, resolution_error, is_stale = (
                        _crosscheck_batch_submission(
                            item["paper_id"],
                            item["token"],
                            lock=True,
                            provenance_cache=provenance_cache,
                        )
                    )
                    if resolution_error:
                        result_group = stale if is_stale else unmatched
                        result_group.append(
                            {
                                "filename": item["filename"],
                                "paper_id": item["paper_id"],
                                "token": item["token"],
                                "message": resolution_error,
                            }
                        )
                        continue
                    item["submission"] = submission
                    resolved_items.append(item)

                duplicate_submission_ids = {
                    submission_id
                    for submission_id, count in Counter(
                        item["submission"].pk
                        for item in resolved_items
                    ).items()
                    if count > 1
                }
                for item in resolved_items:
                    submission = item["submission"]
                    if submission.pk in duplicate_submission_ids:
                        invalid.append(
                            {
                                "filename": item["filename"],
                                "message": (
                                    "Multiple report files in this upload resolve "
                                    "to the same Final Submission. Upload exactly "
                                    "one report per Final Submission."
                                ),
                            }
                        )
                        continue
                    promoted_items.append(item)
                    if item["target"].exists():
                        os.replace(item["target"], item["backup"])
                        item["had_original"] = True
                    os.replace(item["staged"], item["target"])
                    item["promoted"] = True

                    submission.plagiarism_report_path = str(item["target"])
                    submission.plagiarism_report_stale = False
                    submission.plagiarism_imported_at = timezone.now()
                    changed_submissions.append(submission)
                    applied.append(
                        {
                            "paper_id": item["paper_id"],
                            "token": item["token"],
                            "final_submission_id": submission.final_submission_id,
                            "report_path": str(item["target"]),
                            "report_sha256": item["report_sha256"],
                        }
                    )
                    updated += 1

                bulk_update_submissions(
                    changed_submissions,
                    [
                        "plagiarism_report_path",
                        "plagiarism_report_stale",
                        "plagiarism_imported_at",
                    ],
                )
        except Exception:
            _rollback_report_promotions(promoted_items)
            raise

        cleanup_warnings = []
        for item in promoted_items:
            try:
                item["backup"].unlink(missing_ok=True)
            except OSError as exc:
                cleanup_warnings.append(
                    f"Backup cleanup pending for {item['backup']}: {exc}"
                )
        result = {
            "updated": updated,
            "invalid": invalid,
            "unmatched": unmatched,
            "stale": stale,
            "warnings": cleanup_warnings,
        }
        audit_success(
            "crosscheck_report_upload",
            "CrossCheck report PDFs uploaded.",
            result_counts={
                "updated": updated,
                "invalid": len(invalid),
                "unmatched": len(unmatched),
                "stale": len(stale),
                "warnings": len(cleanup_warnings),
            },
            extra={
                "invalid": invalid[:20],
                "unmatched": unmatched[:20],
                "stale": stale[:20],
                "applied": applied[:20],
                "warnings": cleanup_warnings,
            },
        )
        return result
    except Exception as exc:
        audit_failure("crosscheck_report_upload", exc, "CrossCheck report upload failed.")
        raise
    finally:
        for item in staged_items:
            item["staged"].unlink(missing_ok=True)


def _rollback_report_promotions(items):
    rollback_errors = []
    for item in reversed(items):
        try:
            if item["promoted"]:
                item["target"].unlink(missing_ok=True)
            if item["had_original"] and item["backup"].exists():
                os.replace(item["backup"], item["target"])
        except OSError as exc:
            rollback_errors.append(f"{item['target']}: {exc}")
    if rollback_errors:
        raise RuntimeError(
            "CrossCheck report rollback could not fully restore files: "
            + "; ".join(rollback_errors)
        )


def _crosscheck_batch_submission(
    paper_id,
    token,
    *,
    lock=False,
    provenance_cache=None,
):
    try:
        token = validate_token(token)
    except ValueError as exc:
        return None, str(exc), False
    if provenance_cache is not None and token in provenance_cache:
        payloads, provenance_error = provenance_cache[token]
    else:
        try:
            payloads = _token_provenance_payloads(token)
            provenance_error = ""
        except ValueError as exc:
            payloads = []
            provenance_error = str(exc)
        if provenance_cache is not None:
            provenance_cache[token] = (payloads, provenance_error)
    if provenance_error:
        return None, provenance_error, True
    provenance_rows = [
        row
        for payload in payloads
        for row in payload["rows"]
        if clean_value(row.get("paper_id")) == paper_id
    ]
    if not provenance_rows:
        return (
            None,
            "No durable CrossCheck provenance was found for this Paper ID and "
            "token. Legacy or cleaned manifests are not trusted; prepare a new batch.",
            True,
        )

    identities = {
        (
            clean_value(row.get("final_submission_id")),
            clean_value(row.get("publication_pdf_sha256")).lower(),
        )
        for row in provenance_rows
    }
    if len(identities) != 1:
        return (
            None,
            "The token refers to different Final Submission versions or PDF hashes. "
            "Use the result from a single unambiguous batch.",
            True,
        )
    final_submission_id, exported_pdf_hash = identities.pop()
    if not final_submission_id or not exported_pdf_hash:
        return (
            None,
            "The durable batch record lacks version-bound CrossCheck provenance. "
            "Prepare and upload a new batch.",
            True,
        )
    queryset = FinalSubmission.objects
    if lock:
        queryset = queryset.select_for_update()
    submission = queryset.filter(
        final_submission_id=final_submission_id,
        paper_id_filled=paper_id,
    ).first()
    if not submission:
        return None, "The Final Submission recorded in the batch no longer exists.", True
    if (
        not submission.active_version
        or submission.discarded
        or submission.excluded_from_publication
        or not InitialPaper.objects.filter(paper_id=paper_id).exists()
    ):
        return (
            None,
            "The CrossCheck result belongs to a Final Submission that is no longer "
            "the active publication candidate.",
            True,
        )
    publication_pdf = publication_pdf_info(submission)
    if not publication_pdf["exists"]:
        return None, "The exported publication PDF is no longer available.", True
    current_hash = _sha256_path(publication_pdf["path"])
    if current_hash != exported_pdf_hash:
        return (
            None,
            "The publication PDF changed after this CrossCheck batch was prepared.",
            True,
        )
    return submission, "", False


def _sha256_path(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_percent(value, *, field_label="Percent"):
    value = clean_value(value).strip()
    if value == "":
        return None
    if re.fullmatch(r"<\s*1(?:\.0+)?\s*%?", value):
        return Decimal("1")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*%?", value)
    if not match:
        raise ValueError(
            f"{field_label} must be blank (meaning missing), a number from "
            "0 to 100, or '<1'."
        )
    try:
        parsed = Decimal(match.group(1))
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"{field_label} must be blank (meaning missing), a number from "
            "0 to 100, or '<1'."
        ) from None
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise ValueError(f"{field_label} must be between 0 and 100.")
    return round_percent(parsed)


def _skip_row(submission, reason):
    return {
        "paper_id": submission.paper_id_filled,
        "final_submission_id": submission.final_submission_id,
        "reason": reason,
    }
