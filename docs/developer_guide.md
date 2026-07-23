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
the repository checkout stays bind-mounted on the host. Conference runtime data
uses a Compose project-scoped named volume to avoid Docker Desktop bind-mount
I/O overhead.

Use one env file and one compose project name per conference:

```bash
cp .env.example .env.conference-a
docker compose --env-file .env.conference-a -p sms-conf-a up -d --build
```

Important settings:

- `SMS_PORT`: host port for the instance.
- `SMS_DATA_DIR`: raw host mirror destination. The mirror contains directly
  usable `db.sqlite3`, media uploads, reports, audit logs, previews, and exports.
- `SMS_BIND_HOST`: defaults to `127.0.0.1`; set `0.0.0.0` only for trusted LAN
  access.
- `SMS_ALLOWED_HOSTS`: add the LAN hostname or IP when exposing beyond
  localhost.

The image installs dependencies from `requirements.txt`, but compose also
bind-mounts the working tree into `/app`. After `git pull`, restart or run
`up -d --build`; prefer rebuilding when dependencies or Docker files may have
changed.

The Docker entrypoint creates the standard `data/...` folders, runs migrations
unless `SMS_RUN_MIGRATIONS=0`, and starts Gunicorn on `0.0.0.0:8000` inside the
container. Before Gunicorn starts, `collectstatic` copies the pinned local UI
assets into `STATIC_ROOT`; WhiteNoise serves that directory without requiring a
separate proxy. It defaults to one worker and four threads to avoid
multi-process SQLite write contention. `SMS_WEB_WORKERS`, `SMS_WEB_THREADS`,
and `SMS_WEB_TIMEOUT` are runtime overrides; keep one worker with SQLite. This
does not change publication file selection rules. Dynamic response gzip uses
`SelectiveGZipMiddleware`: only the explicit HTML/text/JSON/JavaScript/XML MIME
allowlist is compressed. Binary and unknown MIME types must bypass gzip so
download responses such as publication ZIPs retain their `Content-Length`.

After a checkout update, `scripts/rebuild_docker_instances.py` can rebuild every
existing Compose `web` container created from this checkout. It reads Docker
labels, the published host port, the `/app/data` bind or named-volume mount, and
SMS environment variables from the existing container, then runs
`docker compose up -d --build` with the same project name. Use `--dry-run` to
inspect the inferred settings.

Existing bind-mounted instances must be migrated with
`scripts/migrate_docker_data_volumes.py`. The migration builds the current
image, resolves the Compose project volume name, performs an online verified
pre-copy, gracefully stops one instance, performs the final verified sync,
checks SQLite integrity, and recreates the instance. On failure it starts the
old container or recreates it with `docker-compose.bind.yml`. It never deletes
the original host data folder.

`scripts/backup_docker_instances.py` discovers every named-volume instance for
the current checkout. It pre-syncs raw data to a sibling staging folder while
the app remains available, then briefly stops a running container for the final
consistent sync. A baseline hash manifest lets the final phase avoid rereading
unchanged host files. The script validates SQLite before promoting the mirror,
retains the previous complete mirror, always attempts to restore the original
running state, logs results beside the host mirrors, and returns nonzero if any
project fails. Bind-mounted instances are reported as already host-backed.

Both data scripts use `runtime/.docker-data-operation.lock` to prevent migration
and scheduled backup overlap. Locks older than 12 hours are treated as stale.
They support repeatable `--project`, `--dry-run`, and `--stop-timeout` options.
The transfer helper rejects symlinks, removes stale destination entries, copies
through per-file temporary paths, verifies SHA256 content, and runs SQLite
`PRAGMA integrity_check`.

The raw host mirror is an operational rollback copy, separate from portable
System State ZIPs. `docker-compose.bind.yml` mounts that mirror at `/app/data`
for rollback. Never use `docker compose down -v` during normal operation.

## Regression Commands

Run these before finishing code changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py scripts
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
- Signed multi-editor evidence: `workflow_evidence.py`.
- Readiness and author checks: `checks.py`.
- Exceptions: `exceptions.py`.
- Reports and publication ZIPs: `reports.py`.
- Storage cleanup: `storage_inventory.py`.
- Backup/restore: `system_state.py`.
- Audit logging: `audit.py`.
- Final Submission state persistence and batch writes:
  `final_submission_state.py`.
- Active/duplicate derived-state coordination: `recompute.py`.

Do not put processing or integration logic directly in views.

Storage inventory code must preserve the request boundary in
`storage_inventory.py`: collect database references once, scan each managed
root once, and classify from the resulting index and file records. Do not add
per-file database queries, per-reference filesystem walks, or repeated path
`stat()` calls. Treat directory references and exact file references
explicitly. Overlapping roots must use the documented category protection
priority; never use first-seen/last-seen iteration order to decide whether a
publication-managed file is generated cache. Cleanup apply must recheck both
current database references, current policy classification, and the previewed
filesystem identity. Report-folder cleanup must preserve known
non-regenerable managed subtrees even if folder settings overlap. The Settings
controller must not synchronously build the inventory
or contact GROBID; those operations belong to their separate UI/JSON
endpoints. Read-only middleware, context processors, Settings GET, and storage
inventory must use `AppSetting.read()`; `AppSetting.load()` is reserved for
workflows that are allowed to persist the default singleton.

Clear Database filesystem staging, rollback, and quarantine disposal belong to
`storage_inventory.py`. The Settings controller owns confirmation, the atomic
database reset, user messages, and audit orchestration; it must not recursively
delete configured folders directly.

## Final Submission Write Rules

`FinalSubmission` remains the compatibility source of truth while five
one-to-one state tables mirror its lifecycle domains.

- Keep all compatibility-to-state field mappings in
  `submissions/services/final_submission_state.py`.
- Ordinary model saves use the model `save()` path, which performs
  domain-aware state upserts.
- For several existing submissions, use `bulk_update_submissions()` instead of
  direct `bulk_update()`. It preserves derived review fields, timestamps, and
  state rows in one transaction.
- Use `sync_all_submission_state_records()` for repair/restore and specify
  domain keys when a workflow changed only one lifecycle domain.
- Use `defer_submission_state_sync()` only inside a short outer transaction.
  Long PDF, file, or remote-service loops must flush bounded batches.
- Use `recompute_active_and_duplicate_state()` whenever both active and
  duplicate/replaced values may change.
- Bulk APIs bypass model signals by design. Every new mirrored field must be
  added to the central mapping; mapping-coverage tests enforce this contract.

Organized List may expose paper-level exception actions, but it must reuse `exceptions.py` row builders and approve/remove services. Do not duplicate page/author/plagiarism exception validity rules in templates or controllers. Author paper-count exceptions remain author-level and belong in Author Count / Exceptions, not a single paper row.

Organized List exception POSTs replace one stable per-submission `<tbody>`.
After every action, rebuild and hydrate the complete row from a fresh
`PublicationReadContext`; do not patch badge text from JavaScript. Every
exception textarea has a type-specific draft field. The controller may carry
those drafts into the replacement row only when that section has no persisted
reason. Persisted backend state wins, successful remove/reset clears the target
draft, and validation failure preserves the submitted target draft with an
inline error. Drafts are presentation state only and must never be passed to
another exception service or stored implicitly. Keep ordinary POST/redirect as
the no-HTMX fallback.

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
- Django `messages` are rendered centrally as Toasts in `base.html`. Success and
  info may autohide; warning and error messages remain dismissible until the
  user closes them. Do not use Django messages for persistent workflow state,
  field validation, confirmation content, issue tables, or publication
  blockers; those remain inline alerts or page content.
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
- Formatting list mode is a compact worklist with one Bootstrap-collapse paper
  open at a time. Single Paper Mode must use the session-backed stable queue in
  `services/formatting.py`; do not recalculate Previous/Next from the current
  status order after every Save. Queue order is natural Paper ID/Final ID order,
  while the saved filter/search is used only to create the snapshot and return
  to the originating list. Keep the Single Paper Mode entry inside the
  HTMX-swapped Formatting worklist so its URL always reflects the displayed
  filter/search. Exact cross-page links use Focus mode and must not create a
  sequential queue. Single and Focus modes do not render normal worklist
  pagination.
- Worklist GET filters/search/pagination are progressively enhanced with pinned local HTMX. Every worklist URL must remain a valid normal GET, preserve URL query state, and render its named container; never move service decisions into HTMX event handlers.
- Shared post-action position restoration lives in
  `submissions/static/submissions/worklist_navigation.js`. Worklists opt in with
  `data-cfm-worklist`, and row cards use a stable
  `data-cfm-worklist-card` plus DOM id. The component adds the current same-site
  return URL to ordinary POSTs, restores the original viewport offset, falls
  forward/back when a filter removes the changed row, and can reopen a
  card-owned Bootstrap collapse through `data-cfm-worklist-collapse`. Keep
  workflow mutation, evidence validation, audit, filtering, and publication
  decisions server-side. Formatting title-guard confirmation defers restoration
  until confirm/cancel completes. Programmatic collapse restoration dispatches
  `cfm:worklist-expanded`; the shared component also translates normal
  Bootstrap collapse expansion into that event. Lazy Formatting previews,
  Manual Override forms, and the shared image magnifier use this lifecycle so
  normal clicks and restored cards load the same content instead of leaving an
  unloaded placeholder.
- Heavy worklist evidence should not be eagerly decoded or rendered. Title/Author verification images use native lazy loading and dimensions read from the PNG header; do not decode each image just to size the worklist. Built-in, GROBID, and Manual Override must all use `submissions/services/title_author_verification.py`; extractor-specific renderers are not allowed. The renderer conservatively scans for visibly blank top-page pixels, reuses only verified whitespace, and computes `source_offset` so `header height + safety margin` never reaches the first non-white source content. If the invariant cannot be met with existing whitespace, extend upward. Author evidence uses one case-sensitive character path: internal extracted words and punctuation must match, while the final extracted word may match the beginning of a longer PDF word. Draw only raw character boxes represented by the extraction. A numeric or symbolic continuation is normal green metadata; an alphabetic continuation is an orange partial-word warning. Complete matches take precedence and suppress partial matches for the same author. Pass the resulting matched/partial/missing state into the shared header so each `A1...AN` legend entry uses the same green/orange/red evidence status. Do not normalize case, punctuation, hyphens, superscripts, or digits in this locator. If reliable raw character geometry is unavailable, return no author evidence instead of falling back to a whole-word box. This rule applies only to author evidence rendering; do not alter title evidence, extraction output, or title comparison. Each Manual Override form is fetched only when its collapsed panel is opened, including when the worklist restores that panel after a POST. The partial endpoint is read-only; the existing audited POST workflow remains the only mutation path.
- Shared pagination lives in `submissions/application/pagination.py`. Supported sizes are `25`, `50`, `100`, `200`, and `all`, with 25 as the centralized default. Individual worklists do not define their own default size. Filter and sort the complete lightweight result before pagination, then hydrate expensive file information, suggestions, previews, and diffs only for the selected page. Focused exact-record views force the complete focused result. Every paginated worklist renders the shared partial above and below its rows. Give the worklist a stable `.cfm-worklist-anchor`; pagination links retain GET state and return full-page or HTMX navigation to that anchor.
- Paper Master and Final Submission list sorting is defined in
  `submissions/application/selectors.py`; templates only submit the selected
  `sort` value. Identifier sorts use `natural_text_key()` so numeric chunks are
  ordered numerically. Search, filter tabs, sorting, and pagination must retain
  one another's GET parameters.
- Worklist tabs use `nav nav-tabs cfm-tabs` and the shared active/inactive count
  badge treatment. Do not create page-specific tab styling. A tab that changes
  the result set must be a server-side GET filter applied before pagination;
  client-only Bootstrap tabs must not partition only the current page.
- Worklists with expensive row details must expose separate lightweight row
  selection and hydration functions. Controllers paginate between them. Do not
  call PDF previews, thumbnail enumeration, publication links, exception
  panels, or text diff builders while classifying rows that will not be shown.
- Signed evidence tokens follow the same rule: generate them only for the
  hydrated page or exact focused record, never for the complete pre-pagination
  result set. Token creation and validation must perform zero database queries
  and zero publication-file reads. Paper ID review must compute the canonical
  Paper Master digest once per response and reuse it for every displayed row;
  do not serialize and hash the complete Master list once per token.
- Publication-wide read pages share
  `submissions.services.publication_read.PublicationReadContext`. Pass its
  `FileInspectionContext` through publication-facing helpers so a path is
  inspected once per request. Do not replace this explicit request context
  with module globals, controller-specific caches, or database writes from GET.
- Cross-request SHA-256 reuse is valid only through `FileInspectionContext`,
  whose cache key includes the complete stat signature and whose reader
  verifies that signature after hashing. Final publication export uses strict
  fresh hashing. UI caches and compact Error Report messages must never feed
  export decisions.
- Final publication export must reuse one `PublicationReadContext` for
  readiness, active submission selection, manifest data, and file selection.
  Write ZIP entries from `FileInspectionContext.read_snapshot_bytes()`, not a
  later `ZipFile.write(path)` call. Keep the sanitized, case-insensitive
  publication filename collision check and the start/end database fingerprint
  check in the central export path.
- Formatting `Review OK` must persist the SHA-256 of the selected publication
  source in `source_hash`. Source replacement clears it. Readiness compares the
  current source bytes with that hash only after Formatting status is
  `review_ok`. Pending/Needs Edit records are blocked by `Formatting Not Review
  OK`; an empty hash is expected before review and must not create a duplicate
  `Source Review Hash Missing` issue. A missing Corrected PDF/source is a
  blocker rather than permission to fall back to Original.
- Every formatting POST must provide a short-lived review snapshot created for
  the rendered row. `save_formatting_review()` rechecks active publication scope,
  the row update timestamp, and the selected PDF/source filesystem identity
  under `select_for_update()`. Corrected-PDF title-guard confirmation must reuse
  that snapshot and verify temporary upload size/SHA-256. Never bypass these
  checks with a direct controller call to `update_formatting_submission()`.
- Editor Upload and Formatting preview writes calculate SHA-256 while streaming
  the upload to disk. Confirmation must still fresh-hash the stored preview.
  Their token directories expire after two hours, and changed/missing preview
  bytes are rejected and removed. Do not trade away the confirmation hash to
  reduce low-frequency POST cost. TTL cleanup may remove only a directory with
  a complete parseable payload; an in-progress directory without one must not
  be deleted by another request.
- Formatting upload validation accepts a known PDF/source pair even if the two
  fields were swapped, but rejects two PDFs, two recognized source files, or an
  unknown file in the PDF field. Bound status/notes and field errors must remain
  visible after validation failure; browsers require file inputs to be selected
  again.
- Process PDF thumbnail strips remain expanded by design. `Needs processing`, `Page issues`, `Processed`, `All`, search, and paper jump may narrow or navigate display rows, but the UI must not hide pages inside a matching paper. Fixed thumbnail dimensions are required so lazy loading cannot shift the page.
- Organized List summary metrics must keep publication blockers separate from tracked information. Stable column widths and row panels are display concerns only and must not alter `_needs_attention()`, active candidates, or readiness services.
- Organized List is the only current-publication roster UI. `view=checklist` provides readiness detail and `view=compact` provides the former Publication Candidates roster. Keep the legacy route as a redirect, not a second query/template implementation.
- Final Submissions must keep Import/Re-upload collapsed by default and preserve preview-before-apply behavior.
- Upload zones may summarize/remove browser-selected files, but the server must continue extension/hash validation and preview-before-apply. Do not add direct-to-model uploads or bypass the import preview token.
- Final import compares uploaded bytes only with the canonical original
  `pdf_file` / `source_file`. If either canonical file is absent, a re-upload is
  `new` and must restore it even when a matching legacy
  `current_file_path` / `source_current_file_path` still exists.
- Destructive actions such as discard belong in a clearly separated, collapsed action area rather than before normal edit fields.
- Search/filter logic belongs in selectors/controllers and must not alter publication candidates, active-version rules, review flags, or export scope.
- Mixed Not Publishing and Start2/Editor conflict discovery must use database
  conditional aggregation and load detail rows only for conflicting Paper IDs.
  Avoid transferring every historical Final row to Python on routine GETs.

Return-context coverage includes Organized List (both views), Formatting Review, Title/Author Review, Not Publishing, Verify Paper IDs, and Exceptions. Use `url_has_allowed_host_and_scheme()` at the Final Submission controller boundary; do not trust or redirect directly to arbitrary `next` values. The normal edit form and version-action danger-zone form remain separate POST forms even though they share the same controller endpoint.

Paper ID Review and Title/Author Review continue to use the existing
`_worklist_return_url()` controller helper. Formatting List mode uses the same
helper from its dedicated `_formatting_redirect_after_save()` path so
`filter/q/page/page_size` and the card fragment survive Save and title-guard
confirm/cancel. Do not change `_worklist_return_url()` semantics casually: Not
Publishing and Exceptions also call it.

Tabler 1.4.0 and HTMX 2.0.10 live under `submissions/static/submissions/vendor/` with third-party licenses. Worklists use `hx-select` on normal server pages. Dashboard readiness and global workflow alerts use dedicated read-only partial endpoints so expensive global scans do not block every page response. Keep normal links/forms as fallback, retain CSRF on state-changing forms, and show the global partial-update error alert on transport/server failure. POST forms use a shared duplicate-submit guard but remain ordinary audited Django requests. UI caches must never feed publication/export decisions.

Shared image magnification lives in
`submissions/static/submissions/image_magnifier.js` and
`submissions/static/submissions/image_magnifier.css`. Formatting previews and
Title/Author verification images opt in with `data-cfm-image-magnifier`; do not
create page-specific lens implementations. New rendering paths must initialize
correctly after Bootstrap collapse lazy loading and HTMX swaps. HTMX worklist
replacement must initialize from the canonical `htmx:load` event's
`event.detail.elt`; `htmx:afterSwap`'s old `detail.target` may be detached
after an `outerHTML` swap. The shared `WeakSet` prevents duplicate
initialization. Magnification must remain disabled
for coarse/touch pointers, require `Ctrl`, clear modifier state on key
release/window blur, and must not write review or publication state. Supply hint
text through `data-cfm-image-magnifier-hint`; do not use the native image
`title` tooltip because browsers cannot dismiss an already-open tooltip
immediately.

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
- Organized List source classification must not infer a source-file issue only
  because Formatting is Pending/Needs edit and `source_hash` is empty. Source
  review binding is required only after Formatting Review OK; the independent
  Format Not OK status remains the blocker before then.
- `publication_debug_pdf_info()` describes generated inspection copies. It is never the source for publication package export or CrossCheck export.
- Publication package export, CrossCheck export, duplicate checks, and both Organized List views use publication-facing helpers.
- Final Submissions list file links are row-scoped display links and intentionally show only Original/Corrected files for that row, not another active submission's publication files.
- Do not delete old uploads for traceability.
- Do not expose editable path text fields for user-managed files when upload/link UI is safer.
- System State backup must include referenced review artifacts, including title/author verification images, PDF thumbnails, and format previews.
- System State restore must remap files into the current project `data/` tree and must not preserve old machine-specific absolute paths.

Process PDFs is not a read-only page-count operation. It recalculates active versions, then processes only Paper Master publication candidates that are active, undiscarded, and not Not Publishing. For those candidates it calculates page/hash/thumbnails from the Corrected/Original PDF source, resets page-limit exceptions when page count changes, rebuilds author cache, and syncs the publication PDF debug folder. Historical, discarded, Not Publishing, and invalid-ID records must not create processing errors. It must not scan incoming folders, create submissions, rewrite original/corrected files, or update publication source selection through `current_file_path`. Any future refactor that changes this behavior must update Operator Guide, Architecture Notes, Troubleshooting, and acceptance tests together.

Thumbnail rendering must use operation-unique directories. Batch persistence
compares `final_submission_state_evidence()` under row locks; stale generated
directories are removed, and replaced directories are removed only after
commit when no row references them. Never render directly over a shared
Final-ID directory.

Process PDFs also exposes formatting triage through
`record_formatting_issue_from_pdf_preview()`. Keep this action in the Formatting
service and persist only through the existing `format_status`, `format_notes`,
and `source_hash` fields. Notes are appended after `clean_note_text()`;
Review OK becomes Needs edit and its source binding is cleared. The action must
not reset Title/Author, Paper ID, plagiarism, page, hash, thumbnail, or file
state. It must reject records that are no longer current Paper Master
publication candidates and must write an audit event.

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

The default Audit Log request must use the bounded tail reader. Full-file scans
are reserved for explicit search. Django admin remains read-only for
publication-critical models; new writes belong in audited services.

## Tests

Most regression coverage lives in `submissions/tests/test_acceptance.py`. Add scenario tests when changing:

- Active-version selection.
- Import preview/apply behavior.
- Preview-file byte changes between preview and apply, including Final import,
  Editor Upload, and Formatting title guards.
- Review reset flags.
- Publication readiness and export blocking.
- File priority or publication package output.
- File replacement between readiness and ZIP writing, and sanitized ZIP
  filename collisions.
- System State export/restore.
- Storage cleanup policy.
- Storage inventory exact-file and referenced-directory protection, including
  the fresh reference check between cleanup preview and apply.
- Audit logging for state-changing workflows.
- Editor Upload, discard, and Not Publishing behavior.
- Multi-editor long-running Process PDFs/extraction races, including generated
  file output as well as database fields.
- Worklist UI or local frontend assets. The publication byte-level regression must keep ZIP entry names, PDF/source SHA256 values, manifest rows, and readiness categories unchanged across UI-only requests.
- Pagination performance coverage should assert expensive helper call counts,
  not wall-clock thresholds: normal pages must hydrate only the selected page,
  while `page_size=all` hydrates the complete filtered result.
- Natural sorting may load IDs and sort keys before pagination, but must not
  materialize full Paper Master or Final Submission rows until the page is
  selected.
- Settings performance coverage must assert that its main request does not call
  `build_storage_inventory()` or `check_grobid_api()`. Storage scale benchmarks
  should use generated fixtures outside the committed test suite; functional
  tests should assert call boundaries and cleanup behavior rather than
  machine-dependent wall-clock limits.

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
   Draft export may include ordinary readiness warnings, but structural ambiguity
   (`Multiple Active Final Submissions` or `Duplicate Publication Filename`) must
   fail closed rather than selecting or overwriting a file.
6. Commit code, migrations, templates, docs, and sample data together when they describe one user-facing change.
