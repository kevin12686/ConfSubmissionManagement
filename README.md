# Conference Final Manager

Conference Final Manager is a local no-login Django + SQLite application for preparing conference final submissions for publication.

It is designed for editorial use on one local machine. It manages Paper Master records, Final Submissions, PDFs, source files, editor uploads, review status, exceptions, reports, and full system backup/restore.

## Non-Goals

- No login or user accounts.
- No cloud database or remote service dependency.
- No plagiarism checking execution inside Django.
- No manual copying from `data/` for normal exports; use the app download buttons.

Plagiarism scores and reports are imported from CrossCheck/plagiarism outputs. Title/author extraction is built in and runs from the app. Optional GROBID fallback extraction can be enabled in Settings for difficult PDF formats, but its results still require the same manual review workflow.

## Quick Start

macOS or Linux:

```bash
./scripts/start_local.sh
```

On macOS, double-click `start.command` in Finder. The macOS script creates `.venv`, installs requirements, applies migrations, creates local data folders, starts the server, and opens <http://127.0.0.1:8000/> automatically.

Windows:

```text
start_windows.bat
```

Double-click it in File Explorer or run it from Command Prompt. The Windows script performs the same setup/start steps as the macOS script and opens <http://127.0.0.1:8000/> automatically.

If macOS says the script is not executable after copying the folder:

```bash
chmod +x start.command scripts/start_local.sh
```

To stop the app, press `Ctrl+C` in the Terminal or Command Prompt window running the server.

## Docker Quick Start

Docker is optional. It is useful when you want one machine to restart the app
automatically after reboot, or when you want to run multiple conference
instances from the same code checkout.

Create one environment file per conference:

```bash
cp .env.example .env.conference-a
```

Edit `.env.conference-a` so each conference has a distinct port and data folder:

```env
SMS_PORT=8001
SMS_DATA_DIR=./runtime/conference-a
```

Start the instance:

```bash
docker compose --env-file .env.conference-a -p sms-conf-a up -d --build
```

Open <http://127.0.0.1:8001/>. The compose file uses
`restart: unless-stopped`, so Docker will restart the app after the machine
reboots as long as Docker itself starts at login/boot.

Run another conference by copying another env file with a different port and
data folder:

```bash
cp .env.example .env.conference-b
docker compose --env-file .env.conference-b -p sms-conf-b up -d --build
```

In Docker, the SQLite database is stored at
`SMS_DATA_DIR/db.sqlite3`, and managed article files, reports, audit logs,
previews, and exports are stored under the same `SMS_DATA_DIR` folder. Back up
that folder before moving or archiving a conference.

To update after pulling a new version:

```bash
git pull
docker compose --env-file .env.conference-a -p sms-conf-a up -d --build
```

If several conference containers already exist for this checkout, rebuild and
restart all of them from their current Docker settings:

```bash
python3 scripts/rebuild_docker_instances.py
```

Preview what will be rebuilt without changing containers:

```bash
python3 scripts/rebuild_docker_instances.py --dry-run
```

The source checkout is bind-mounted into the container, so a plain restart is
often enough for code-only changes. Use `up -d --build` for the normal update
path because it also refreshes the image when `requirements.txt` or Docker
setup changes.

By default Docker binds the app to `127.0.0.1`. If another computer must access
the app over a trusted local network, set `SMS_BIND_HOST=0.0.0.0` and add the
machine hostname or IP address to `SMS_ALLOWED_HOSTS`.

## New Computer Setup

1. Install Python 3.12 or newer.
2. Copy the whole `SubmissionManagementSystem` folder.
3. Start the app with `start.command`, `start_windows.bat`, or `./scripts/start_local.sh`.
4. If you have a System State ZIP, open `/integrations/system-state/` and preview the restore before applying it.

The first run needs internet access to install Python packages from `requirements.txt`. After that, normal local use is offline. Tabler, HTMX, and their licenses are pinned under Django static files; the browser UI does not require CDN assets.

System State ZIP files are portable. They restore settings, conference name, database records, PDFs, source files, reports, previews, and managed files into the new computer's local `data/` folders.

## Documentation

- [Operator Guide](docs/operator_guide.md): daily editorial workflow from import to publication export.
- [Troubleshooting](docs/troubleshooting.md): common setup, import, PDF, extraction, export, cleanup, and restore issues.
- [Developer Guide](docs/developer_guide.md): local development, tests, migrations, versioning, and service boundaries.
- [Architecture Notes](docs/architecture.md): internal structure and workflow boundaries.
- [Editorial Acceptance Runbook](docs/editorial_acceptance_runbook.md): manual end-to-end validation before a real handoff.

## Main Workflow

1. Configure Settings, including conference name, folders, limits, thresholds, timezone, and active-version rule.
2. Import the Paper Master List from CSV/XLSX.
3. Import Final Submission metadata plus PDF/source files from CSV/XLSX and uploaded files.
4. Resolve Paper ID mapping, Not Publishing decisions, and Start2/Editor Upload conflicts.
   Editor Upload dry-runs title extraction before saving a record. Its responsive
   title safety check compares the uploaded PDF title vertically against Paper
   Master and Final titles, combines identical references, and lets you open,
   replace, or cancel the temporary PDF before confirming a mismatch.
5. Run Process PDFs to refresh page count, hash, thumbnails, and publication debug PDF copies.
6. Run Title/Author Review. Extraction, the verification image, title comparison, and authors are reviewed together; `Review OK` is the single completion state. Use optional GROBID fallback only for suspicious rows or individual papers that the built-in extractor handles poorly. Use Manual override only as a documented exception when extraction cannot be fixed through formatting/re-extraction.
7. Review formatting, upload corrected PDF/source files when needed, and re-run Process PDFs after corrected PDFs.
8. Export PDFs for CrossCheck/plagiarism, import Plagiarism % and Single %, and upload optional report PDFs.
9. Review author counts, duplicate authors, page exceptions, author-limit exceptions, and plagiarism score exceptions. Paper-level exceptions can be handled from Organized List; author paper-count exceptions remain in Author Count / Exceptions.
10. Use Dashboard, Organized List, and Error Report as the publication readiness checklist. Dashboard uses the same blocking checks as final package export.
11. Export the final publication package, or download a clearly marked draft package if blockers still exist.
12. Use Audit Log when tracing what changed, when it changed, and which paper/version was affected.
13. Download a System State ZIP before moving machines or archiving a conference.

Large worklists are organized for editorial scanning. They default to 100 rows
and offer `50 / 100 / 200 / All`; `All` preserves the former complete-list
behavior when a full comparison is required. Filtering and sorting happen
before pagination, while expensive row details and diffs are built only for the
displayed page. Final Submissions keeps `Import / Re-upload` collapsed until
needed. Formatting Review uses a compact queue with one expanded paper at a
time plus Single Paper Mode. Process PDFs keeps every page thumbnail for each
paper on the current page expanded. Organized List separates publication
blockers from tracked information and keeps stable table widths.

The UI uses locally pinned Tabler 1.4.0 and HTMX 2.0.10. Worklist GETs,
pagination, Dashboard readiness, and global workflow alerts load through
server-rendered endpoints; normal worklist URLs remain directly usable.
Upload zones show selected file counts/types and allow removal before submit,
but imports still require the server-side preview-before-apply step. Workflow
decisions, publication files, active versions, review resets, exceptions, and
state-changing actions remain server-owned; no publication-changing action
uses optimistic browser state.

Links opened from a specific Final Submission use exact record identifiers rather
than the normal fuzzy search box. Paper ID Review, Process PDFs, Title/Author
Review, Formatting Review, Not Publishing, Organized List, and Exceptions show a
consistent focused-record banner and only the intended record. If that version is
inactive, discarded, Not Publishing, or otherwise outside a page's workflow
scope, the page explains that condition instead of silently showing a different
match. User-entered search fields remain intentionally fuzzy.

## Current Final Publication Version Rules

The Paper Master List is the publication scope. A paper is considered for final publication only when its Paper ID exists in Paper Master List and the selected Final Submission is not discarded and not marked Not Publishing.

For each Paper ID, active version selection currently works this way:

1. Discarded submissions are excluded.
2. If any undiscarded Editor Upload exists for the Paper ID, the newest Editor Upload is active.
3. If no undiscarded Editor Upload exists, the newest Start2/imported submission is active.
4. "Newest" follows the Settings active-version rule: Final ID order or upload date with Final ID as tie-breaker.
5. If Start2 and Editor Upload both remain undiscarded, the Editor Upload is temporarily active, but final publication export is blocked until one side is discarded with a note.

Publication-facing PDF resolution uses this priority:

1. Corrected PDF, if uploaded and present.
2. Original PDF for the active submission, if present.

Publication-facing source resolution uses this priority:

1. Corrected source, if uploaded and present.
2. Original source for the active submission, if present.

`data/publication_pdf_debug/` is a generated inspection folder created by Process PDFs or Settings > Sync Debug PDFs. It is not the source of truth. Publication ZIPs, CrossCheck ZIPs, duplicate checks, and publication links read the active submission's Corrected PDF or Original PDF directly, not the debug copy.

Legacy fields such as `current_file_path`, `source_current_file_path`, `active_final_folder`, and `old_versions_folder` may exist in older restored data for traceability/debugging, but they are not used to choose the final publication PDF/source.

## Important Pages

- `/` Dashboard: final-package readiness and only the editorial workflows that currently need action
- `/papers/` Paper Master List
- `/submissions/` Final Submissions
- `/submissions/editor-upload/` Editor Upload
- `/submissions/organized/` Organized List
- `/processing/pdfs/` Process PDFs
- `/reviews/paper-ids/` Verify Paper IDs
- `/reviews/title-authors/` Title/Author Review
- `/reviews/formatting/` Formatting Review
- `/reviews/not-publishing/` Not Publishing List
- `/reviews/exceptions/` Exceptions
- `/reports/errors/` Error Report
- `/reports/author-count/` Author Count
- `/reports/audit-log/` Audit Log
- `/reports/` Export Reports
- `/reports/active-versions/` legacy Publication Candidates URL; redirects to Organized List `Compact candidates`
- `/integrations/crosscheck/` Plagiarism / CrossCheck
- `/integrations/system-state/` System Backup / Restore
- `/settings/` Settings and Storage Management

## Templates

CSV templates can be downloaded inside the app. Static examples are also in `sample_data/`:

- `paper_master_list_template.csv`
- `final_submissions_template.csv`

The CrossCheck/plagiarism result template is downloaded from the CrossCheck / Plagiarism page.

CrossCheck export ZIPs use the same publication PDF source rule as final publication exports: Corrected PDF, then Original PDF from the active publishable submission in Paper Master List scope.

Paper Master imports include `paper_id`, `acceptance_status`, `title`, `authors`, and `notes`.

Final Submission imports include `final_submission_id`, `author_entered_paper_id`, `final_submission_title`, `final_submission_authors`, `upload_date`, and `uploaded_fields`.

PDF/source files are matched by Final Submission ID with names such as:

- `34_file_Submit_PDF.pdf`
- `34_file_Submit_Source.docx`

The app checks file extensions, so a PDF uploaded in the source slot can still be recognized as the PDF.

Large Final Submission file batches can be selected at once. The app allows up to 5000 uploaded files per request, which is a Django request-parsing limit rather than a CSV row limit. Split larger PDF/source uploads into multiple batches.

## Data Folders

The app stores local data under `data/` by default:

- `data/media/final_submissions/`: original uploaded PDFs.
- `data/media/source_submissions/`: original uploaded source files.
- `data/media/formatted_pdfs/`: corrected PDFs.
- `data/media/formatted_sources/`: corrected source files.
- `data/publication_pdf_debug/`: generated inspection copies of current publication PDFs. These copies should match the bytes used by the publication ZIP, but they are never used as export input.
- `data/logs/audit.log`: append-only JSON Lines audit trail for key user and system actions.
- `data/logs/archive/`: archived audit logs created when Clear Database is run with the audit-clear option.
- `data/reports/`: generated Excel/ZIP exports.
- `data/plagiarism_reports/`: uploaded plagiarism report PDFs.
- `data/media/pdf_thumbnails/`, `data/media/format_previews/`, `data/media/title_author_verification/`: generated UI/review artifacts used by Process PDFs, Formatting Review, and Title/Author Review.

Folder paths can be changed in Settings. System State ZIPs include referenced review artifacts such as title/author verification images, page thumbnails, and format previews, while excluding temporary preview tokens. System State restore remaps managed paths into the current computer's local project folder instead of preserving old absolute paths.

## Audit Log

The app writes key actions to `data/logs/audit.log` as JSON Lines. Events include imports, applies, manual edits, uploads, editor uploads, discard/undo, Not Publishing, verification, extraction, formatting, Process PDFs, CrossCheck export/import, exceptions, settings changes, publication export, backup/restore, storage cleanup, and Clear Database.

Open `/reports/audit-log/` to search by Paper ID, Final ID, action, status, or message. The page shows the latest events and can download the raw log.

Clear Database preserves `audit.log` by default. If you check `Also archive and clear audit log`, the current log is moved to `data/logs/archive/` and a new `audit.log` is created with an event recording that archive action.

## Manual Commands

If you do not want to use the start scripts:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Development sanity checks:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
```

Docker startup uses Gunicorn with one worker and four threads by default, runs
the same Django application, and stores its SQLite database and managed files
in the bind-mounted `SMS_DATA_DIR` folder. One worker avoids multi-process
SQLite write contention; `SMS_WEB_THREADS` and `SMS_WEB_TIMEOUT` may be
overridden for a deployment. Startup also collects the pinned local UI assets,
and WhiteNoise serves them through Gunicorn without a separate web server.
