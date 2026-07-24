# Conference Final Manager

Conference Final Manager is a local Django + SQLite application for preparing
conference final submissions for publication. It manages the Paper Master
scope, Final Submission versions, PDFs and source files, editorial reviews,
exceptions, publication exports, and portable System State backups.

The application is designed for editors working on one local machine. It has no
login system and does not depend on a cloud database.

## Start Here

Choose the document that matches your task:

| Need | Document |
| --- | --- |
| Install the app and understand the main workflow | This README |
| Run the conference workflow | [Operator Guide](docs/operator_guide.md) |
| Resolve an error or unexpected result | [Troubleshooting](docs/troubleshooting.md) |
| Understand publication scope, version, and file rules | [Publication Rules](docs/publication_rules.md) |
| Develop or review code changes | [Developer Guide](docs/developer_guide.md) |
| Understand service boundaries and safety design | [Architecture Notes](docs/architecture.md) |
| Change worklists or shared UI behavior | [UI Conventions](docs/ui_conventions.md) |
| Validate a release before a real handoff | [Editorial Acceptance Runbook](docs/editorial_acceptance_runbook.md) |
| Review version history | [Changelog](CHANGELOG.md) |

`docs/publication_rules.md` is the canonical description of publication-facing
behavior. Other guides describe how to operate or implement those rules without
redefining them.

## Non-Goals

- No login or user accounts.
- No hardened public internet deployment.
- No cloud database or remote service dependency.
- No plagiarism checking execution inside Django.
- No manual copying from `data/` for normal exports; use app download actions.

Plagiarism scores and reports are imported from CrossCheck or equivalent output.
Title/author extraction runs locally. Optional GROBID fallback can be enabled
for difficult PDFs, but its output still requires manual review.

## Quick Start

Python 3.12 or newer is required.

macOS or Linux:

```bash
./scripts/start_local.sh
```

On macOS, `start.command` can also be opened from Finder.

Windows:

```text
start_windows.bat
```

The start scripts create `.venv`, install requirements, apply migrations,
prepare local data folders, and start <http://127.0.0.1:8000/>.

If macOS reports that the scripts are not executable:

```bash
chmod +x start.command scripts/start_local.sh
```

Press `Ctrl+C` in the terminal running the server to stop the app.

The first run needs internet access to install Python dependencies. Normal local
operation is offline afterward; Tabler and HTMX are pinned under Django static
files and do not require a CDN.

## Docker Quick Start

Docker is optional. Use a distinct Compose project, port, and data mirror for
each conference:

```bash
cp .env.example .env.conference-a
docker compose --env-file .env.conference-a -p sms-conf-a up -d --build
```

The default example opens the app at <http://127.0.0.1:8000/>. Change
`SMS_PORT` in the environment file when several conferences run on the same
machine.

Runtime data lives in a Compose project-scoped named volume.
`SMS_DATA_DIR` is a verified, directly usable host mirror. Refresh every current
instance mirror with:

```bash
python3 scripts/backup_docker_instances.py --dry-run
python3 scripts/backup_docker_instances.py
```

Existing bind-mounted installations must first be migrated:

```bash
python3 scripts/migrate_docker_data_volumes.py --dry-run
python3 scripts/migrate_docker_data_volumes.py
```

Never use `docker compose down -v` for a conference instance; `-v` deletes its
named data volume. See the [Operator Guide](docs/operator_guide.md#backup-cleanup-and-clear-database)
for backup and rollback procedures and the
[Developer Guide](docs/developer_guide.md#docker-environment) for deployment
details.

## New Computer Or Restored Conference

1. Install Python 3.12 or newer.
2. Copy the complete project folder.
3. Start the app with the appropriate start script.
4. Open `/integrations/system-state/`.
5. Preview the System State ZIP, then apply it only after the preview is correct.

System State ZIPs restore settings, conference records, managed files, reports,
review artifacts, and audit logs. Paths are remapped into the current
installation instead of preserving old machine-specific absolute paths.

## Editorial Workflow

1. Configure Settings, including conference name, folders, limits, thresholds,
   timezone, and the active-version rule.
2. Import the Paper Master List.
3. Import Final Submission metadata and matching PDF/source files.
4. Resolve Paper ID mappings, Not Publishing decisions, and Start2/Editor
   Upload conflicts.
5. Run Process PDFs to refresh page counts, hashes, thumbnails, and publication
   debug copies.
6. Complete Title/Author Review.
7. Complete Formatting Review and upload corrected files where necessary.
8. Export CrossCheck PDFs, import plagiarism scores, and attach optional reports.
9. Review author counts, duplicates, page limits, and approved exceptions.
10. Use Dashboard, Organized List, and Error Report severity/category filters
    to clear publication blockers.
11. Export the final publication package.
12. Review the Audit Log and download a System State ZIP before handoff.

Paper selection controls in Editor Upload, Paper ID Review, and Process PDFs
search on demand instead of loading the complete Paper Master List. Type a
Paper ID, Master Title, or Master Author; exact Paper ID matches are shown
first.

The [Operator Guide](docs/operator_guide.md) explains each stage. The
[Publication Rules](docs/publication_rules.md) define which records and files
may enter publication output.

## Publication Safety Summary

These rules are intentionally fail-closed:

- Paper Master defines publication scope.
- Editor Upload outranks Start2, but an unresolved mixed-source conflict blocks
  final export.
- Corrected files outrank Original files; a selected Corrected file that is
  missing does not fall back to Original.
- Publication output never reads from the generated debug-copy folder.
- Dashboard and final export use the same readiness findings.
- Final export rejects ambiguous active state, duplicate sanitized filenames,
  changed files, and concurrent publication-state changes.
- Review state resets only when its documented dependency changes.
- State-changing workflows and exports are audited.

The complete rules and implementation map are in
[Publication Rules](docs/publication_rules.md).

## Important Pages

| Page | URL |
| --- | --- |
| Dashboard | `/` |
| Paper Master List | `/papers/` |
| Final Submissions | `/submissions/` |
| Organized List | `/submissions/organized/` |
| Process PDFs | `/processing/pdfs/` |
| Paper ID Review | `/reviews/paper-ids/` |
| Title/Author Review | `/reviews/title-authors/` |
| Formatting Review | `/reviews/formatting/` |
| Exceptions | `/reviews/exceptions/` |
| Error Report | `/reports/errors/` |
| Export Reports | `/reports/` |
| CrossCheck | `/integrations/crosscheck/` |
| System State | `/integrations/system-state/` |
| Settings | `/settings/` |

The complete page map and page responsibilities are in the
[Operator Guide](docs/operator_guide.md#page-map).

## Templates And Data

Import templates can be downloaded inside the app. Static examples are stored
in `sample_data/`:

- `paper_master_list_template.csv`
- `final_submissions_template.csv`

Managed runtime data is stored under `data/` by default. Do not select
publication files by browsing that folder. Use application links and export
actions, which apply the publication-facing rules.

## Development

Manual environment setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Required regression gate:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py scripts
```

See the [Developer Guide](docs/developer_guide.md) before changing workflow,
publication, storage, export, or review behavior.
