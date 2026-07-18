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

## Docker Environment

Docker support is intended for local/operator deployments, not as a hardened
internet-facing service. The container provides the Python/Django runtime while
the repository checkout and conference data stay bind-mounted on the host.

Use one env file and one compose project name per conference:

```bash
cp .env.example .env.conference-a
docker compose --env-file .env.conference-a -p sms-conf-a up -d --build
```

Important settings:

- `SMS_PORT`: host port for the instance.
- `SMS_DATA_DIR`: host folder mounted to `/app/data`; it contains
  `db.sqlite3`, media uploads, reports, audit logs, previews, and exports.
- `SMS_BIND_HOST`: defaults to `127.0.0.1`; set `0.0.0.0` only for trusted LAN
  access.
- `SMS_ALLOWED_HOSTS`: add the LAN hostname or IP when exposing beyond
  localhost.

The image installs dependencies from `requirements.txt`, but compose also
bind-mounts the working tree into `/app`. After `git pull`, restart or run
`up -d --build`; prefer rebuilding when dependencies or Docker files may have
changed.

The Docker entrypoint creates the standard `data/...` folders, runs migrations
unless `SMS_RUN_MIGRATIONS=0`, and starts Django on `0.0.0.0:8000` inside the
container. It does not change publication file selection rules.

After a checkout update, `scripts/rebuild_docker_instances.py` can rebuild every
existing Compose `web` container created from this checkout. It reads Docker
labels, the published host port, the `/app/data` bind mount, and SMS environment
variables from the existing container, then runs `docker compose up -d --build`
with the same project name. Use `--dry-run` to inspect the inferred settings.

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
- `submissions/templates/submissions/`: server-rendered Tabler/Bootstrap-compatible templates and shared partials.
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

Organized List `Details` is the publication-record view for the active row. Its
authors must come from that submission's `extracted_authors`, and its files must
come from the publication-facing helpers. Do not substitute Paper Master authors,
another Final Submission version, legacy current paths, or debug copies as the
publication source.

For display, the Details author list is parsed with the shared `split_authors()`
helper and numbered in publication order. This is presentation only; never
rewrite `extracted_authors` while preparing the display list.

## Worklist UI Conventions

- Tabler renders `.alert` as a horizontal flex row by default, but CFM alerts
  use normal vertical document flow unless the template explicitly adds
  `.d-flex`. Use `.cfm-alert-stack` for alerts containing tables, lists,
  multi-step forms, or several content blocks. Reserve explicit `.d-flex`
  alerts for a short message paired with a compact action group.
- Long editorial tables use the shared `cfm-table-sticky` class so column ownership remains visible while scrolling.
- Contextual links to Final Submission Edit pass a same-site `next` URL. Save must return to the originating worklist without accepting external redirects.
- System-generated cross-page links must use exact identifiers: `submission=<pk>`
  for Final Submission work, `paper_id=<exact Paper Master ID>` for Organized
  List, and `exception_key=<service row key>` for Exceptions. Reserve `q` for
  user-entered fuzzy search. Exact focus must never fall back to the first or
  nearest matching record.
- Exact-target worklists render the shared
  `partials/focused_worklist.html` context. When a target is outside a
  publication worklist's scope, render an explicit read-only explanation; do
  not widen the service queryset or change active/review state to make it appear.
- Formatting list mode is a compact queue with one Bootstrap-collapse paper open at a time; Single Paper Mode remains the full sequential workspace.
- Worklist GET filters/search are progressively enhanced with pinned local HTMX. Every enhanced endpoint must remain a valid normal GET, preserve URL query state, and render its named worklist container; never move service decisions into HTMX event handlers.
- Process PDF thumbnail strips remain expanded by design. `Needs processing`, `Page issues`, `Processed`, `All`, search, and paper jump may narrow or navigate display rows, but the UI must not hide pages inside a matching paper. Fixed thumbnail dimensions are required so lazy loading cannot shift the page.
- Organized List summary metrics must keep publication blockers separate from tracked information. Stable column widths and row panels are display concerns only and must not alter `_needs_attention()`, active candidates, or readiness services.
- Organized List is the only current-publication roster UI. `view=checklist` provides readiness detail and `view=compact` provides the former Publication Candidates roster. Keep the legacy route as a redirect, not a second query/template implementation.
- Final Submissions must keep Import/Re-upload collapsed by default and preserve preview-before-apply behavior.
- Upload zones may summarize/remove browser-selected files, but the server must continue extension/hash validation and preview-before-apply. Do not add direct-to-model uploads or bypass the import preview token.
- Destructive actions such as discard belong in a clearly separated, collapsed action area rather than before normal edit fields.
- Search/filter logic belongs in selectors/controllers and must not alter publication candidates, active-version rules, review flags, or export scope.

Return-context coverage includes Organized List (both views), Formatting Review, Title/Author Review, Not Publishing, Verify Paper IDs, and Exceptions. Use `url_has_allowed_host_and_scheme()` at the Final Submission controller boundary; do not trust or redirect directly to arbitrary `next` values. The normal edit form and version-action danger-zone form remain separate POST forms even though they share the same controller endpoint.

Tabler 1.4.0 and HTMX 2.0.10 live under `submissions/static/submissions/vendor/` with third-party licenses. Enhanced GET requests use `hx-select` on the normal server page, avoiding duplicate fragment-only rendering paths. Keep normal links/forms as fallback, retain CSRF on state-changing forms, and show the global partial-update error alert on transport/server failure. POST forms use a shared duplicate-submit guard but remain ordinary audited Django requests. Alpine.js and Uppy are not dependencies; do not add them unless a future requirement needs durable client-side state or per-file retry/progress that cannot be met cleanly.

## Data And Review Reset Rules

When changing data that affects a review, reset only dependent review flags.

Examples:

- Changed PDF resets processing, title/author extraction, title match review, plagiarism scores, formatting review, and related file-derived exceptions, including plagiarism score exceptions.
- Changed source resets formatting review.
- Changed extracted authors resets author-number and duplicate-author review state.
- Changed Paper ID resets Paper ID verification and active-version grouping.
- Changed Paper Master notes must not reset any review/check status.
- Active-version rule changes must be previewed and applied without resetting review flags.

Workflow ownership is also a reset-safety boundary. `FinalSubmissionForm` must not expose processing messages/status, Title/Author Review status, duplicate-author review, or Not Publishing fields. Use the dedicated services and pages so required resets and audit events cannot be bypassed.

Manual Final Submission create and edit paths are intentionally separate. Create must use `create_final_submission_manual()` so Paper ID evaluation, file paths, initial review state, active/duplicate selection, and audit logging happen atomically. Edit must use `apply_final_submission_manual_edit()` with an existing record; do not pass `None` or create a placeholder original record.

Prefer preview-before-apply for imports, re-uploads, restore, and any setting change that can materially alter current publication candidates.

## Dashboard Readiness Rules

Dashboard must consume `publication_readiness_rows()` through the application selector. Do not build a second list of blockers from `dashboard_counts()`; otherwise Dashboard can appear clear while final export is blocked.

`dashboard_counts()` is for display details, conference totals, and non-blocking tracking information. Counts labeled as papers must deduplicate by active publication paper. Inactive, discarded, and Not Publishing versions must not inflate active issue counts. Keep verified/reviewed title differences separate from unverified title-mapping blockers. Title/Author `Review OK` is the completion decision for both extracted metadata and its title comparison; do not add a second publication blocker for a reviewed title difference.

When adding or renaming a publication readiness category, update the Dashboard workflow category grouping and add an acceptance test proving Dashboard and final package export still agree.

## File Handling Rules

Use app-managed file helpers instead of ad hoc path logic.

- `source_pdf_path()` is processing/extraction input: corrected PDF, then original PDF.
- `publication_pdf_info()` is publication-facing output: corrected PDF, then original PDF.
- `publication_source_info()` is publication-facing output: corrected source, then original source.
- `publication_debug_pdf_info()` describes generated inspection copies. It is never the source for publication package export or CrossCheck export.
- Publication package export, CrossCheck export, duplicate checks, and both Organized List views use publication-facing helpers.
- Final Submissions list file links are row-scoped display links and intentionally show only Original/Corrected files for that row, not another active submission's publication files.
- Do not delete old uploads for traceability.
- Do not expose editable path text fields for user-managed files when upload/link UI is safer.
- System State backup must include referenced review artifacts, including title/author verification images, PDF thumbnails, and format previews.
- System State restore must remap files into the current project `data/` tree and must not preserve old machine-specific absolute paths.

Process PDFs is not a read-only page-count operation. It recalculates active versions, then processes only Paper Master publication candidates that are active, undiscarded, and not Not Publishing. For those candidates it calculates page/hash/thumbnails from the Corrected/Original PDF source, resets page-limit exceptions when page count changes, rebuilds author cache, and syncs the publication PDF debug folder. Historical, discarded, Not Publishing, and invalid-ID records must not create processing errors. It must not scan incoming folders, create submissions, rewrite original/corrected files, or update publication source selection through `current_file_path`. Any future refactor that changes this behavior must update Operator Guide, Architecture Notes, Troubleshooting, and acceptance tests together.

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
- Worklist UI or local frontend assets. The publication byte-level regression must keep ZIP entry names, PDF/source SHA256 values, manifest rows, and readiness categories unchanged across UI-only requests.

Title-upload safeguards must use `build_title_guard_context()` and the shared
`includes/title_guard_comparison.html` partial. Do not create separate three-column
Master/Final/PDF title layouts. Full titles remain in a single-column
`minmax(0, 1fr)` flow with explicit wrapping; word-level differences are primary and
character-level differences are optional detail. Preview open/cancel/replace actions
must operate on the server-owned preview token and write audit events without creating
or modifying a submission before confirmation.

Use factories in `submissions/tests/factories.py` rather than duplicating setup when possible.

## Version And Release Checklist

The app version is `APP_VERSION` in `conference_final_manager/settings.py`. The footer displays it.

Increment `APP_VERSION` for user-visible workflow, docs, UI, schema, or export changes.

Increment `STATE_ARCHIVE_VERSION` only when System State ZIP structure or restore compatibility changes.

Exact-navigation and focused-worklist changes do not alter System State archive
contents, so they require an app version change but not an archive version
change.

Before release:

1. Run regression commands.
2. Confirm docs match current routes and feature names.
3. Confirm `README.md` points to new or changed docs.
4. Export a System State ZIP and verify manifest version fields.
5. If publication export changed, test both final and draft package paths.
6. Commit code, migrations, templates, docs, and sample data together when they describe one user-facing change.
