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

Edit `.env.conference-a` so each conference has a distinct port and host mirror:

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

Docker runs the SQLite database and managed files from a Compose
project-scoped named volume. `SMS_DATA_DIR` is the host mirror destination; it
keeps the same directly usable `db.sqlite3`, media, reports, audit logs,
previews, and export layout as the former bind mount.

For an existing bind-mounted Docker installation, pull this version and migrate
all instances before using the normal rebuild command:

```bash
python3 scripts/migrate_docker_data_volumes.py --dry-run
python3 scripts/migrate_docker_data_volumes.py
```

The migration scans all Compose `web` containers from this checkout, pre-copies
each live host data folder into its new named volume, briefly stops that
instance for a verified final sync, and recreates it. It processes conferences
one at a time and leaves the original host folder unchanged as the first
rollback copy.

Refresh every named-volume instance's raw host mirror with:

```bash
python3 scripts/backup_docker_instances.py --dry-run
python3 scripts/backup_docker_instances.py
```

The backup command discovers all current instances automatically; no env file
arguments are required. It performs a verified online pre-sync, gracefully
stops one running instance for the final consistent SQLite/file sync, validates
the copied database, promotes the staging folder, and restarts the instance.
The prior complete mirror is retained beside `SMS_DATA_DIR` with the
`.backup-previous` suffix. The host mirror can be mounted directly if rollback
is required.

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

To temporarily run a conference from its latest host mirror instead of the
named volume:

```bash
docker compose -f docker-compose.yml -f docker-compose.bind.yml \
  --env-file .env.conference-a -p sms-conf-a up -d --build
```

Return to the named volume by running the normal Compose command without
`docker-compose.bind.yml`. Do not run `docker compose down -v`; `-v` deletes the
conference named volume.

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
   Confirmation is rejected if the temporary PDF bytes or Paper Master record
   changed after preview.
5. Run Process PDFs to refresh page count, hash, thumbnails, and publication debug PDF copies. If a thumbnail reveals a formatting problem, record it directly from the paper card or enlarged page preview; the note is added to the existing Formatting Review workflow as `Needs edit`.
6. Run Title/Author Review. Extraction, the verification image, title comparison, and authors are reviewed together; `Review OK` is the single completion state. Built-in, GROBID, and Manual Override results use the same collision-safe verification renderer: a review header lists the source, filename, extracted title, and numbered authors, reuses verified blank space above the PDF title, and expands upward only when needed. The PDF evidence uses title underlines and separate author boundaries. Hold `Ctrl` over the verification image to inspect those markings with the shared magnifier. Use optional GROBID fallback only for suspicious rows or individual papers that the built-in extractor handles poorly. Use Manual override only as a documented exception when extraction cannot be fixed through formatting/re-extraction.
7. Review formatting, upload corrected PDF/source files when needed, and re-run Process PDFs after corrected PDFs. On desktop, hold `Ctrl` while pointing at the first-page preview to inspect a wide title/author area without opening another window.
8. Export PDFs for CrossCheck/plagiarism, import Plagiarism % and Single %, and upload optional report PDFs.
9. Review author counts, duplicate authors, page exceptions, author-limit exceptions, and plagiarism score exceptions. Paper-level exceptions can be handled from Organized List; author paper-count exceptions remain in Author Count / Exceptions.
10. Use Dashboard, Organized List, and Error Report as the publication readiness checklist. Dashboard uses the same blocking checks as final package export.
11. Export the final publication package, or download a clearly marked draft package if blockers still exist.
12. Use Audit Log when tracing what changed, when it changed, and which paper/version was affected.
13. Download a System State ZIP before moving machines or archiving a conference.

Large worklists are organized for editorial scanning. They default to 25 rows
and offer `25 / 50 / 100 / 200 / All`; `All` preserves the former complete-list
behavior when a full comparison is required. Filtering and sorting happen
before pagination, while expensive row details and diffs are built only for the
displayed page. Pagination controls appear above and below each worklist, and
changing the page or page size returns the viewport to the worklist controls
instead of leaving the editor at the bottom of the new page. Final Submissions keeps `Import / Re-upload` collapsed until
needed. Formatting Review uses a compact list with one expanded paper at a
time plus a stable Single Paper Mode queue. Starting Single Paper Mode snapshots
the selected filter/search order; Save stays on the same paper, and changing its
status does not remove or reorder the queue's Previous/Next destinations. Exact
links from another workflow open a separate focused review instead of silently
creating a queue. Formatting previews and Title/Author verification images use
the same `Ctrl`-activated desktop magnifier; touch devices keep the normal
static image and full-file link. Process PDFs keeps every page thumbnail for each paper on the
current page expanded. Its integrated formatting triage appends page-specific or
paper-level notes to the same Formatting Review record, clears a previous Review
OK source binding, and leaves the PDF, processing result, and unrelated review
states unchanged. Organized List separates publication
blockers from tracked information and keeps stable table widths.

Paper Master List and Final Submissions provide server-side Sort controls next
to Search. Paper ID and Final ID options use natural numeric ordering, so `P2`
and Final ID `2` appear before `P10` and `10`. Final Submission tabs preserve
the selected sort and search context.

Publication-wide read pages share one request snapshot for Paper Master,
active submissions, settings, and file status. Repeated PDF/source hashes are
reused only while the complete filesystem signature is unchanged; final
publication export performs strict fresh validation and writes the exact
validated file snapshot. Final and draft exports block unresolved active-version
ambiguity and sanitized ZIP filename collisions instead of allowing duplicate
entries, reject concurrent publication-state changes, and require Formatting
Review to bind the exact source bytes. A selected
Corrected file that is missing never falls back to Original. Error Report keeps
large duplicate groups compact and loads the full matching-record list on
demand, including when Page size is `All`.

Author paper counts and their publication blockers are derived from that same
active Paper Master snapshot, not from the persisted `PaperAuthor`
compatibility cache. A stale or empty cache cannot change readiness or package
eligibility.

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

1. Corrected PDF, if selected. If its file is missing, publication is blocked.
2. Original PDF for the active submission, if present.

Publication-facing source resolution uses this priority:

1. Corrected source, if selected. If its file is missing, publication is blocked.
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
- `/settings/` Settings and asynchronously loaded Storage Management

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

Settings renders its editable fields immediately. Storage Management scans the
configured folders in a separate panel request, and the optional GROBID health
check also runs after the page opens. A slow bind mount or unavailable GROBID
service therefore does not block the Settings form.

## Audit Log

The app writes key actions to `data/logs/audit.log` as JSON Lines. Events include imports, applies, manual edits, uploads, editor uploads, discard/undo, Not Publishing, verification, extraction, formatting, Process PDFs, CrossCheck export/import, exceptions, settings changes, publication export, backup/restore, storage cleanup, and Clear Database.

Open `/reports/audit-log/` to search by Paper ID, Final ID, action, status, or message. The page shows the latest events and can download the raw log. The default latest-events view reads only the tail of a large log; search intentionally scans the complete log.

Clear Database preserves `audit.log` by default. If you check `Also archive and clear audit log`, the current log is moved to `data/logs/archive/` and a new `audit.log` is created with an event recording that archive action.

The Django `/admin/` pages are read-only diagnostics for
publication-critical models. Make changes through the audited editorial
workflows so reset, concurrency, and active-version rules cannot be bypassed.

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
in the Compose project named volume. `SMS_DATA_DIR` receives the separately
verified raw host mirror. One worker avoids multi-process SQLite write
contention; `SMS_WEB_THREADS` and `SMS_WEB_TIMEOUT` may be overridden for a
deployment. Startup also collects the pinned local UI assets, and WhiteNoise
serves them through Gunicorn without a separate web server. Dynamic response
compression is limited to an explicit HTML/text/JSON/JavaScript/XML allowlist;
ZIP, PDF, image, Office, and unknown binary responses are not recompressed.
