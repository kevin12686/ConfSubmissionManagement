# Developer Guide

This guide is for maintaining the Django project.

## Local Environment

Use the same virtual environment as the app:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

macOS operators usually run `start.command` or `./scripts/start_local.sh`. Windows operators run `start_windows.bat`.

## Regression Commands

Run these before finishing code changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```

For documentation-only changes, run at least:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

## Project Structure

- `conference_final_manager/`: Django settings and root URL config.
- `submissions/controllers/`: HTTP views grouped by workflow.
- `submissions/application/selectors.py`: page/query context builders.
- `submissions/application/commands.py`: workflow command wrappers.
- `submissions/services/`: domain services.
- `submissions/templates/submissions/`: Bootstrap templates.
- `submissions/tests/`: acceptance regression tests and factories.
- `sample_data/`: CSV templates.
- `docs/`: operator, developer, architecture, troubleshooting, and acceptance docs.

## Where Logic Belongs

Keep controllers thin. A controller can validate forms, choose commands, set messages, redirect, and render templates.

Put reusable workflow behavior in services:

- Import preview/apply: `import_preview.py`.
- CSV/XLSX parsing and templates: `import_export.py`.
- PDF processing: `pdf_processor.py`.
- Publication file resolution: `file_manager.py`.
- Paper ID verification: `verification.py`.
- Title/author extraction and manual override: `title_author_extraction.py`, `builtin_title_author_extractor.py`, and optional `grobid_extractor.py`.
- Formatting workflow: `formatting.py`.
- CrossCheck/plagiarism: `crosscheck.py`.
- Readiness and author checks: `checks.py`.
- Exceptions: `exceptions.py`.
- Reports and publication ZIPs: `reports.py`.
- Storage cleanup: `storage_inventory.py`.
- Backup/restore: `system_state.py`.
- Audit logging: `audit.py`.

Do not put processing or integration logic directly in views.

Organized List may expose paper-level exception actions, but it must reuse `exceptions.py` row builders and approve/remove services. Do not duplicate page/author/plagiarism exception validity rules in templates or controllers. Author paper-count exceptions remain author-level and belong in Author Count / Exceptions, not a single paper row.

## Data And Review Reset Rules

When changing data that affects a review, reset only dependent review flags.

Examples:

- Changed PDF resets processing, title/author extraction, title match review, plagiarism scores, formatting review, and related file-derived exceptions, including plagiarism score exceptions.
- Changed source resets formatting review.
- Changed extracted authors resets author-number and duplicate-author review state.
- Changed Paper ID resets Paper ID verification and active-version grouping.
- Changed Paper Master notes must not reset any review/check status.
- Active-version rule changes must be previewed and applied without resetting review flags.

Prefer preview-before-apply for imports, re-uploads, restore, and any setting change that can materially alter current publication candidates.

## File Handling Rules

Use app-managed file helpers instead of ad hoc path logic.

- `source_pdf_path()` is processing/extraction input: corrected PDF, then original PDF.
- `publication_pdf_info()` is publication-facing output: corrected PDF, then original PDF.
- `publication_source_info()` is publication-facing output: corrected source, then original source.
- `publication_debug_pdf_info()` describes generated inspection copies. It is never the source for publication package export or CrossCheck export.
- Publication package export, CrossCheck export, duplicate checks, Organized List publication links, and Active Versions use publication-facing helpers.
- Final Submissions list file links are row-scoped display links and intentionally show only Original/Corrected files for that row, not another active submission's publication files.
- Do not delete old uploads for traceability.
- Do not expose editable path text fields for user-managed files when upload/link UI is safer.
- System State backup must include referenced review artifacts, including title/author verification images, PDF thumbnails, and format previews.
- System State restore must remap files into the current project `data/` tree and must not preserve old machine-specific absolute paths.

Process PDFs is not a read-only page-count operation. It calculates page/hash/thumbnails from the Corrected/Original PDF source, resets page-limit exceptions when page count changes, recalculates active versions, rebuilds author cache, and syncs the publication PDF debug folder. It must not scan incoming folders, create submissions, rewrite original/corrected files, or update publication source selection through `current_file_path`. Any future refactor that changes this behavior must update Operator Guide, Architecture Notes, Troubleshooting, and acceptance tests together.

## Audit Logging Requirements

Any new workflow that changes records, files, review status, publication readiness, settings, exports, cleanup, or backup/restore must write an audit event through `submissions/services/audit.py`.

Use the helper that matches the result:

- `audit_preview()` for preview-before-apply steps.
- `audit_requested()` for dangerous requests such as Clear Database.
- `audit_success()` after a successful state change or export.
- `audit_failure()` when an operation fails.
- `audit_blocked()` when the app intentionally blocks an export or workflow because readiness checks failed.

Audit events should include the relevant Paper ID, Final Submission ID, changed fields, before/after values, reset flags, file changes, file hashes, and result counts. Store paths as portable project/media-relative references; never log binary PDF/source/report content.

Clear Database must preserve `data/logs/audit.log` unless the user explicitly checks the audit-clear checkbox. System State backup must include the active audit log and archived logs.

## Tests

Most regression coverage lives in `submissions/tests/test_acceptance.py`. Add scenario tests when changing:

- Active-version selection.
- Import preview/apply behavior.
- Review reset flags.
- Publication readiness and export blocking.
- File priority or publication package output.
- System State export/restore.
- Storage cleanup policy.
- Audit logging for state-changing workflows.
- Editor Upload, discard, and Not Publishing behavior.

Use factories in `submissions/tests/factories.py` rather than duplicating setup when possible.

## Version And Release Checklist

The app version is `APP_VERSION` in `conference_final_manager/settings.py`. The footer displays it.

Increment `APP_VERSION` for user-visible workflow, docs, UI, schema, or export changes.

Increment `STATE_ARCHIVE_VERSION` only when System State ZIP structure or restore compatibility changes.

Before release:

1. Run regression commands.
2. Confirm docs match current routes and feature names.
3. Confirm `README.md` points to new or changed docs.
4. Export a System State ZIP and verify manifest version fields.
5. If publication export changed, test both final and draft package paths.
6. Commit code, migrations, templates, docs, and sample data together when they describe one user-facing change.
