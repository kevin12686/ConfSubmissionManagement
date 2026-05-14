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
- Title/author extraction: `title_author_extraction.py` and `builtin_title_author_extractor.py`.
- Formatting workflow: `formatting.py`.
- CrossCheck/plagiarism: `crosscheck.py`.
- Readiness and author checks: `checks.py`.
- Exceptions: `exceptions.py`.
- Reports and publication ZIPs: `reports.py`.
- Storage cleanup: `storage_inventory.py`.
- Backup/restore: `system_state.py`.

Do not put processing or integration logic directly in views.

## Data And Review Reset Rules

When changing data that affects a review, reset only dependent review flags.

Examples:

- Changed PDF resets processing, title/author extraction, title match review, plagiarism scores, formatting review, and related file-derived exceptions.
- Changed source resets formatting review.
- Changed extracted authors resets author-number and duplicate-author review state.
- Changed Paper ID resets Paper ID verification and active-version grouping.
- Changed Paper Master notes must not reset any review/check status.
- Active-version rule changes must be previewed and applied without resetting review flags.

Prefer preview-before-apply for imports, re-uploads, restore, and any setting change that can materially alter current publication candidates.

## File Handling Rules

Use app-managed file helpers instead of ad hoc path logic.

- Publication PDF must resolve through corrected, active-final, then original priority.
- Publication source must resolve through corrected, then current/original priority.
- Do not delete old uploads for traceability.
- Do not expose editable path text fields for user-managed files when upload/link UI is safer.
- System State backup must include referenced review artifacts, including title/author verification images, PDF thumbnails, and format previews.
- System State restore must remap files into the current project `data/` tree and must not preserve old machine-specific absolute paths.

## Tests

Most regression coverage lives in `submissions/tests/test_acceptance.py`. Add scenario tests when changing:

- Active-version selection.
- Import preview/apply behavior.
- Review reset flags.
- Publication readiness and export blocking.
- File priority or publication package output.
- System State export/restore.
- Storage cleanup policy.
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
