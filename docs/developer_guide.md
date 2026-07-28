# Developer Guide

This guide is for maintaining the Django project.

Use this guide for environment setup, service ownership, data dependencies,
tests, and release work. Shared presentation rules live in
[UI Conventions](ui_conventions.md); publication-facing business rules live in
[Publication Rules](publication_rules.md). Architecture rationale lives in
[Architecture Notes](architecture.md). The complete guide map is in the
[Documentation Home](README.md).

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

## Docker Development Boundary

Docker procedures are centralized in the [Docker Guide](docker_guide.md).
Developers should use that guide for instance creation, environment ownership,
updates, backups, migration, and rollback.

The development-facing boundaries are:

- Docker runs Django/Gunicorn as `web` behind the published Nginx `proxy`.
- Conference state lives in the project-scoped `sms_data` volume; collected
  static assets and gateway status are rebuildable or temporary.
- Keep one Gunicorn worker with SQLite.
- Nginx must preserve the public Host header, serve static/media independently
  of `SMS_DEBUG`, and never participate in publication file selection.
- Code or environment changes are applied through
  `scripts/update_docker_instances.py`, beginning with `--dry-run`.
- The Docker operation scripts share a lock and must retain the current data
  mount type until an explicit migration or rollback.
- Never use `docker compose down -v` for a conference instance.

Changes to Compose, Nginx, gateway behavior, or the Docker management scripts
must update the Docker Guide, affected troubleshooting entries, architecture
rationale, and Stage 8 of the acceptance runbook.

## Regression Commands

Run these before finishing code changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py scripts
```

For documentation-only changes, run at least:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
```

## Project Structure

- `conference_final_manager/`: Django settings and root URL config.
- `submissions/controllers/`: HTTP views grouped by workflow.
- `submissions/application/selectors.py`: page/query context builders.
- `submissions/application/commands.py`: workflow command wrappers.
- `submissions/presentation.py`: canonical user-facing worklist vocabulary;
  business logic must never branch on these display strings.
- `submissions/services/`: domain services.
- `submissions/templates/submissions/`: server-rendered Tabler/Bootstrap-compatible templates and shared partials.
- `submissions/tests/`: acceptance regression tests and factories.
- `sample_data/`: CSV templates.
- `docs/`: documentation home plus operator, Docker, developer, architecture,
  troubleshooting, rules, UI, and acceptance guides.

## README Preview Assets

README screenshots live under `docs/assets/readme/`. Capture them from a
disposable database and media root populated only with synthetic conference,
paper, author, and submission data. Never capture the operator's normal
`db.sqlite3` or `data/` tree.

Update the affected preview when a user-visible Dashboard or Organized List
change makes the current image misleading. Keep the image format, extension,
alternative text, and README link consistent, then run the documentation gate.

## Where Logic Belongs

Keep controllers thin. A controller can validate forms, choose commands, set messages, redirect, and render templates.

Put reusable workflow behavior in services:

- Import preview/apply: `import_preview.py`.
- CSV/XLSX parsing and templates: `import_export.py`.
- PDF processing: `pdf_processor.py`.
- Publication file resolution: `file_manager.py`.
- Paper Master publication decisions: `publication_decisions.py`.
- Paper ID verification: `verification.py`.
- Title/author extraction and manual override: `title_author_extraction.py`, `builtin_title_author_extractor.py`, and optional `grobid_extractor.py`.
- Formatting workflow: `formatting.py`.
- CrossCheck/plagiarism: `crosscheck.py`.
- Signed multi-editor evidence: `workflow_evidence.py`.
- Readiness and author checks: `checks.py`.
- Exceptions: `exceptions.py`.
- Report data and publication ZIPs: `reports.py`.
- Shared XLSX presentation: `excel_workbook.py`. Keep CSV/package output out of
  this formatter; CSV schemas are machine-readable contracts. Editorial
  Workbook supporting sheets must use the whitelist in `reports.py`; raw/debug
  sheets stay separate from the workbook selector.
- Storage cleanup: `storage_inventory.py`.
- Backup/restore: `system_state.py`.
- Audit logging: `audit.py`.
- Final Submission state persistence and batch writes:
  `final_submission_state.py`.
- Active/duplicate derived-state coordination: `recompute.py`.

Do not put processing or integration logic directly in views.

Shared worklist wording belongs in `submissions/presentation.py`. Keep database
choices, filter query values, readiness checks, and workflow transitions based
on stable internal values. A wording change must not alter publication
eligibility or require templates to infer state from rendered label text.

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

Error Report exception actions use the same exception-row keys, signed
evidence tokens, approve/remove services, and audit path. Readiness findings
may carry an additive `exception_key` plus the expected Final Submission scope;
that metadata must never replace or weaken the readiness condition. Rebuild the
complete Error Report worklist from a fresh `PublicationReadContext` after an
HTMX action because severity, category counts, pagination, and the finding
itself can all change. Preserve the active known category when its count drops
to zero so the editor can see that the selected blocker was resolved. Unknown
category query values remain invalid. Keep normal POST/redirect as the
non-HTMX fallback.

Organized List `Details` is the publication-record view for the active row. Its
authors must come from that submission's `extracted_authors`, and its files must
come from the publication-facing helpers. Do not substitute Paper Master authors,
another Final Submission version, legacy current paths, or debug copies as the
publication source.

For display, the Details author list is parsed with the shared `split_authors()`
helper and numbered in publication order. This is presentation only; never
rewrite `extracted_authors` while preparing the display list.

## Shared UI And Worklists

[UI Conventions](ui_conventions.md) is the canonical guide for worklists,
feedback, exact navigation, pagination, partial updates, evidence rendering,
and accessibility. Keep the following implementation boundaries when applying
that guide:

- Controllers perform lightweight selection, filtering, sorting, and
  pagination before expensive row hydration.
- Signed evidence is generated only for the displayed page or exact focused
  record.
- `PublicationReadContext` and its `FileInspectionContext` remain explicit
  request-scoped objects; do not replace them with controller caches, globals,
  or writes from GET requests.
- Final export uses strict fresh file validation and snapshot byte reads as
  defined in [Publication Rules](publication_rules.md#export-integrity).
- `_worklist_return_url()` and `_formatting_redirect_after_save()` preserve
  filter, search, page size, page, and card context for audited POST redirects.
- Final Submission return URLs are same-site only and validated with
  `url_has_allowed_host_and_scheme()`.
- Tabler 1.4.0, HTMX 2.0.10, and Tom Select 2.6.2 remain pinned under
  `submissions/static/submissions/vendor/` with their licenses.
- The shared Paper picker is limited to Paper Master selection workflows. Its
  read-only endpoint returns no results for an empty query, caps responses at
  20, and returns Master ID, Title, and Authors. Keep Paper selection validation
  in Django forms/services; picker values are presentation input, not workflow
  authority.
- Shared behavior belongs in the existing pagination, navigation, magnifier,
  focus, tabs, and alert components; do not create page-specific alternatives.
- Normal links and forms remain the fallback, CSRF remains enabled, and UI
  caches never feed publication or export decisions.

## Data And Review Reset Rules

When changing data that affects a review, reset only dependent review flags.

Examples:

- Changed PDF resets processing, title/author extraction, title match review, plagiarism scores, formatting review, and related file-derived exceptions, including plagiarism score exceptions.
- Changed source resets formatting review.
- Changed extracted authors resets author-number and duplicate-author review state.
- Changed Paper ID resets Paper ID verification and active-version grouping.
- Changed Paper Master notes must not reset any review/check status.
- Active-version rule changes must be previewed and applied without resetting review flags.
- Changed Paper Master publication decision recalculates publication scope and
  Final compatibility mirrors, but must not reset review/check evidence.
- New Paper Master creation must use
  `create_paper_master_with_publication_guard()`. Direct model creation is
  acceptable only in controlled tests and migrations. Matching orphan
  exclusion evidence must produce Decision Required.

Workflow ownership is also a reset-safety boundary. `FinalSubmissionForm` must
not expose processing messages/status, Title/Author Review status,
duplicate-author review, or Not Publishing fields. Paper Master publication
decisions must use `publication_decisions.py`; mapped Final exclusion fields are
compatibility mirrors only. Use the dedicated services and pages so required
locking, evidence checks, synchronization, and audit events cannot be bypassed.
`Decision Required` must preserve legacy mirror evidence. Final, draft, and
CrossCheck exports must call the shared publication-decision integrity check;
do not rely on UI state or Master scope filtering alone.

Manual Final Submission create and edit paths are intentionally separate. Create must use `create_final_submission_manual()` so Paper ID evaluation, file paths, initial review state, active/duplicate selection, and audit logging happen atomically. Edit must use `apply_final_submission_manual_edit()` with an existing record; do not pass `None` or create a placeholder original record.

Prefer preview-before-apply for imports, re-uploads, restore, and any setting change that can materially alter current publication candidates.

Final re-import is keyed by Final ID. Keep Official Paper ID resolution in
`preview_final_import()`:

- preserve a nonblank Official ID when Author-entered ID is unchanged;
- resolve when Author-entered ID changes or Official ID is blank;
- never use a Final Title change alone to remap an existing submission;
- never infer Not Publishing from a missing/invalid Official ID.

Paper Master and Final Submission imports are separate services. Do not
reintroduce a combined Mapping Table workbook or hidden mapping metadata path.

An orphan Final Paper ID may be resolved from Not Publishing List, but that
controller must call `verify_submission()` with a current `paper-id-review`
evidence token. Use `paper_id_review_snapshot()` to construct that token and
the shared Paper Master picker partial for selection. Never assign
`paper_id_filled` directly from this UI. Orphan Not Publishing actions must
pass an explicit reason; `mark_not_publishing()` validates the reason in the
service layer and has no semantic default.

## Dashboard Readiness Rules

Dashboard must consume `publication_readiness_rows()` through the application selector. Do not build a second list of blockers from `dashboard_counts()`; otherwise Dashboard can appear clear while final export is blocked.

`dashboard_counts()` is for display details, conference totals, and non-blocking tracking information. Counts labeled as papers must deduplicate by active publication paper. Inactive, discarded, and Not Publishing versions must not inflate active issue counts. Keep verified/reviewed title differences separate from unverified title-mapping blockers. Title/Author `Review OK` is the completion decision for both extracted metadata and its title comparison; do not add a second publication blocker for a reviewed title difference.

When adding or renaming a publication readiness category, update the Dashboard workflow category grouping and add an acceptance test proving Dashboard and final package export still agree.

Error Report category selection is presentation-only and must filter the
already annotated rows in `checks.py`; it must never reimplement readiness
conditions in the controller or template. Preserve repeated `category` query
parameters, validate them against the current workflow-area rows, apply the
selection before pagination, and keep multi-category matching as OR.

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

Process PDFs is not a read-only page-count operation. It recalculates active
versions, then processes only active, undiscarded Final versions whose Paper
Master decision is Publishing. For those candidates it calculates
page/hash/thumbnails from the Corrected/Original PDF source, resets page-limit
exceptions when page count changes, rebuilds author cache, and syncs the
publication PDF debug folder. Historical, discarded, Master Not Publishing,
Decision Required, and invalid-ID records must not create processing errors.
It must not scan incoming folders, create submissions, rewrite
original/corrected files, or update publication source selection through
`current_file_path`. Any future refactor that changes this behavior must update
Operator Guide, Architecture Notes, Troubleshooting, and acceptance tests
together.

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

Register every production action in
`submissions/services/audit_actions.py` and use its canonical
`<domain>_<operation>[_<phase>]` code at the call site. Keep workflow phases
such as `preview`, `apply`, `cancel`, and `undo` in the action only when they
represent distinct operations. Keep outcomes such as `success`, `failed`, and
`blocked` in the event status. Aliases are only for reading historical logs;
new code must not introduce another spelling for an existing action.

Use the helper that matches the result:

- `audit_preview()` for preview-before-apply steps.
- `audit_requested()` for dangerous requests such as Clear Database.
- `audit_success()` after a successful state change or export.
- `audit_failure()` when an operation fails.
- `audit_blocked()` when the app intentionally blocks an export or workflow because readiness checks failed.

Audit events should include the relevant Paper ID, Final Submission ID, changed fields, before/after values, reset flags, file changes, file hashes, and result counts. Store paths as portable project/media-relative references; never log binary PDF/source/report content.

Clear Database must preserve `data/logs/audit.log` unless the user explicitly checks the audit-clear checkbox. System State backup must include the active audit log and archived logs.

The default Audit Log request must use the bounded tail reader. Full-file scans
are reserved for explicit search or structured category/action/status filters.
Legacy aliases may be normalized for display and filtering, but stored JSONL
events remain append-only and must never be rewritten. Django admin remains
read-only for publication-critical models; new writes belong in audited
services.

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

Archive version 5 matches the schema after removal of the unused Mapping Table
metadata fields. Version 4 introduced authoritative Paper Master publication
decisions. Do not accept an older archive as equivalent without an explicit
migration path; either missing decision state or unknown removed fields can
make a restore ambiguous.

Exact-navigation and focused-worklist changes do not alter System State archive
contents, so they require an app version change but not an archive version
change.

Before release:

1. Run regression commands.
2. Confirm docs match current routes and feature names.
3. Update the canonical owner for each changed rule:
   `docs/publication_rules.md` for publication behavior,
   `docs/ui_conventions.md` for shared UI behavior, and this guide for
   implementation and release requirements.
4. Confirm `README.md` points to new or renamed docs and update `CHANGELOG.md`.
5. Export a System State ZIP and verify manifest version fields.
6. If publication export changed, test both final and draft package paths.
   Draft export may include ordinary readiness warnings, but structural ambiguity
   (`Multiple Active Final Submissions` or `Duplicate Publication Filename`) must
   fail closed rather than selecting or overwriting a file.
7. Commit code, migrations, templates, docs, and sample data together when they describe one user-facing change.
