# Architecture Notes

Conference Final Manager is a local Django application with SQLite storage and local file management. The application is intentionally no-login and single-machine.

This document explains system boundaries and safety rationale. The canonical
business rules are in [Publication Rules](publication_rules.md), and shared
presentation behavior is in [UI Conventions](ui_conventions.md). Procedures
belong in the audience-specific guides listed on the
[Documentation Home](README.md).

## Application Boundaries

- Controllers handle HTTP forms, redirects, Django messages, downloads, and template rendering.
- Application selectors assemble read-only page contexts and query composition.
- Application commands wrap state-changing workflows and return result objects.
- Services contain domain logic for imports, verification, PDF processing, formatting, title/author extraction, CrossCheck/plagiarism integration, exceptions, reports, storage cleanup, and backup/restore.
- Templates stay server-rendered Django pages using locally pinned Tabler 1.4.0 with Bootstrap-compatible markup; there is no React or separate frontend build.

Core route groups:

- `/papers/`: Paper Master List.
- `/submissions/`: Final Submissions and Editor Upload.
- `/reviews/`: Paper ID, title/author, formatting, exceptions, and Not Publishing workflows.
- `/processing/pdfs/`: page count, hashes, thumbnails, publication debug copies, and active-version recalculation.
- `/reports/`: readiness reports, author count, version history, and publication exports.
- `/reports/audit-log/`: searchable audit trail and raw audit log download.
- `/integrations/crosscheck/`: plagiarism/CrossCheck package export, score import, and report upload.
- `/integrations/system-state/`: complete System State backup and preview-before-apply restore.
- `/settings/`: app settings, active-version rule preview, storage management, and clear database.

## FinalSubmission State Split

`FinalSubmission` remains the compatibility record and behavior source of truth
for submission-version state. The Paper Master publication decision is the
intentional exception: `InitialPaper.publication_decision_status` is
authoritative and Final exclusion fields are mirrors for mapped Paper IDs.
Newer one-to-one state models mirror lifecycle domains:

- `FinalSubmissionIdentityState`
- `FinalSubmissionFileState`
- `FinalSubmissionReviewState`
- `FinalSubmissionPublicationState`
- `FinalSubmissionPlagiarismState`

The split supports gradual refactoring. Reads can move to state models one workflow at a time, but writes must stay synchronized until legacy fields are fully retired.

State persistence is centralized in
`submissions/services/final_submission_state.py`. Its domain mapping is the
single definition of how compatibility fields populate Identity, File, Review,
Publication, and Plagiarism state rows. Normal `FinalSubmission.save()` calls
upsert only affected domains. Full repair/restore synchronization performs bulk
upserts, and database-heavy workflows use the same service for bulk main-table
updates plus matching state upserts.

Do not call `FinalSubmission.objects.bulk_update()` directly for mirrored
fields. Use `bulk_update_submissions()` so derived review fields, timestamps,
and mirror rows remain synchronized in one transaction. Short import workflows
may defer mirror writes inside an outer transaction. Long PDF, file, or remote
service loops flush bounded batches so SQLite is not locked for the duration of
external processing.

## Workflow Rules

### Editorial State Ownership

- Paper Master List is the publication scope.
- Final Submissions can come from Start2 imports or Editor Uploads.
- Editor Uploads are prioritized over Start2 records, but unresolved Start2/Editor conflicts block final publication export until one side is discarded.
- Editor Upload and corrected-PDF formatting uploads share one server-rendered title
  safety component. Services build a common comparison payload, templates render the
  uploaded title once with vertically stacked references, and character-level detail
  remains collapsed by default. Preview files stay temporary until apply; opening,
  replacing, or canceling a preview never changes publication state. Editor
  Upload apply locks and rechecks Paper Master evidence plus the exact temporary
  PDF/source size and SHA-256 before creating a version.
- Discard is version-level: it excludes one Final Submission version but does not mean the paper is not publishing.
- Not Publishing is Paper-Master-level: `InitialPaper.publication_decision_status`
  is authoritative even before a Final exists. It keeps records for
  traceability but excludes the paper from publication processing, reviews,
  readiness, and package output. Final Submission exclusion fields are
  synchronized compatibility mirrors for mapped Paper IDs.
- Publication PDF priority is corrected PDF, then original author PDF.

Dashboard readiness is derived from `publication_readiness_rows()`, the same service used to block Final Publication Package export. The Dashboard controller owns one workflow registry that groups those rows, derives both primary and breakdown counts as unique affected papers, and builds exact-category worklist links. It must not recreate publication-blocking rules with independent counters. The readiness header separately reports the number of individual blocker rows. An unregistered readiness category is rendered as `Other publication blockers` and suppresses the clear-workflow summary, so a new blocker cannot disappear from Next actions while final export remains blocked.
- Publication source priority is corrected source, then original source.
- Active version selection is previewed before changing the active-version rule in Settings.
- Import/re-upload workflows are preview-before-apply when they may change existing records or files.
- Paper Master and Final Submission imports are separate boundaries. Final
  re-import is keyed by Final ID and preserves an existing Official Paper ID
  while Author-entered ID is unchanged; Final Title alone never remaps it.
- Review flags are reset only when dependent data changes.
- Final Submission Edit owns submission metadata, original files, and P/S score/report entry. Processing, Title/Author Review, duplicate-author review, and Not Publishing decisions are read-only there and are changed only through their dedicated workflows.
- Manual Final Submission creation and editing are separate service operations. `create_final_submission_manual()` accepts only an unsaved form instance and owns initial Paper ID evaluation, file-path initialization, Pending review state, active/duplicate recalculation, and create audit logging. Existing edits first pass through `record_edit_preview.py`, then confirmation delegates to `apply_final_submission_manual_edit()`. The apply service requires an existing record and remains the sole owner of dependency-based reset rules; it must never receive `None` or synthesize an original record.
- Editorial worklists preserve navigation context when they link into Final Submission Edit. Organized List, Formatting Review, Title/Author Review, Not Publishing, Verify Paper IDs, and Exceptions pass a return URL that is restricted to the local host. The legacy Publication Candidates URL redirects to Organized List compact mode.
- Not Publishing List treats unresolved Final Submissions outside Paper Master
  as an attention-first decision queue. Its inline Paper Master search is only
  a second entry point to `verify_submission()` and the signed Paper ID review
  evidence contract; it is not a separate ID-editing implementation. The
  alternate action calls the existing orphan publication-decision service and
  requires an explicit reason.
- Cross-page record navigation is separate from search. System-generated links
  identify a Final Submission by database primary key, a Paper Master record by
  exact Paper ID, or an exception by its service-generated key. Controllers
  build a shared focused-worklist context and services keep their normal
  publication scope. If the exact target is outside that scope, the UI reports
  why; it never substitutes another fuzzy match. GET focus modes are read-only.
- Final Submission Edit separates editable identity/metadata/files/plagiarism data from a read-only workflow summary. Its normal Save form is structurally separate from the collapsed bottom version-action danger-zone form. Discard and undo continue to call the existing audited service; Not Publishing remains owned by its dedicated workflow.
- Formatting Review exposes a compact list, a stable Single Paper Mode queue,
  and a separate exact-record Focus mode. List rows show publication
  file/status context before expansion, Bootstrap's shared parent keeps one
  paper expanded at a time, and HTMX enhances GET-only filter/search navigation
  without owning workflow state. The Single Paper Mode entry lives inside that
  swapped worklist so it always carries the filter/search currently on screen.
  Starting the mode stores an ephemeral, naturally sorted snapshot of matching
  submission IDs plus the filter/search in the Django session. Status changes
  do not reorder that snapshot. Previous/Next skip IDs that later leave
  publication scope, and the queue expires after two hours. Focus mode never
  creates or mutates a queue.
- Formatting previews and Title/Author verification images use one shared native
  Image Magnifier component. It initializes after lazy Bootstrap collapse
  loading and HTMX worklist swaps, runs only for fine hover pointers, requires
  the `Ctrl` modifier, resets on key release/window blur, and never supplies
  publication or workflow state. Normal verification-image links still open the
  complete image. The shared lens uses a responsive `3:2` landscape viewport
  constrained by the source image's displayed bounds. A shared in-image hint
  replaces the browser-native tooltip so it hides immediately while the lens is
  active and returns without browser-dependent delay when the lens closes.
- Process PDFs deliberately keeps complete page-thumbnail strips expanded. Search and `Needs processing / Page issues / Processed / All` filters narrow papers only; paper jump, sticky identity headers, fixed thumbnail geometry, lazy image loading, and the enlarged preview modal do not change processing scope. Its integrated formatting-triage action is the only state-changing exception: it appends to the existing `FinalSubmission.format_notes`, sets `format_status=needs_edit`, and clears the Formatting Review source binding. It does not create a second issue store or modify files and processing metadata.
- Organized List separates current-view publication blockers from tracked information and uses stable table columns. Paper Master rows whose active final is Not Publishing are omitted from this publication-current view, while replaced versions remain inactive history. Final Submissions keeps its Import/Re-upload workflow collapsed until requested.
- Organized List owns both the full Checklist and Compact candidates views. This removes a second publication-current UI implementation while preserving `/reports/active-versions/` as a compatibility redirect.
- `Review OK` is the single Title/Author completion decision. The Final-versus-extracted title comparison remains visible evidence; a reviewed difference is tracked but does not create a second blocker.
- Paper ID Review, Title/Author Review, and Formatting Review share one
  presentation-only post-action navigation component. Ordinary audited POSTs
  remain server-owned; the browser records the active card, adjacent cards,
  viewport offset, and expanded collapse state. The safe server redirect retains
  the complete worklist URL. After reload, the component returns to the same
  card or the next/previous visible card if the selected filter removed it.

### Server-Rendered Worklists

The UI remains server-rendered. Tabler 1.4.0 and HTMX 2.0.10 are vendored
locally. Normal worklist URLs support GET filter/search/tab/pagination
navigation, while HTMX replaces the named worklist container and updates
browser history. Dashboard readiness and global workflow alerts are separate
read-only partial endpoints so their global scans do not delay unrelated page
content. Global alerts may use a short display-only cache; publication
readiness and exports never do. State-changing POST actions remain normal
audited Django requests.

Large worklists use the shared `WorklistPage` boundary. The complete lightweight
scope is classified and sorted first, then the selected `25 / 50 / 100 / 200` page
is hydrated with file checks, previews, suggestions, and diffs. `page_size=all`
hydrates the complete filtered result and is the explicit compatibility path
for full-list inspection. Organized List, Process PDFs, Author Count,
Exceptions, and Old Versions expose separate lightweight-selection and
display-hydration functions; controllers must paginate between those two
steps.

Paper Master List and Final Submissions apply validated server-side sort keys
before `WorklistPage` pagination. Natural identifier ordering is shared through
`natural_text_key()`. Worklist tabs use the common `cfm-tabs` component, so
active state, count badges, and spacing remain consistent across reports and
review queues.

### Publication Read Boundary

`PublicationReadContext` is the request-scoped read boundary for Paper Master,
active Final Submissions, settings, and filesystem inspection. Dashboard
counts, publication readiness, duplicate detection, Error Report, and global
workflow alerts reuse this context rather than loading independent publication
scopes. It is immutable request data, not a publication cache, and GET requests
do not persist derived state.

`InitialPaper.publication_decision_status` is the authoritative publication
decision boundary. `PublicationReadContext` loads only `Publishing` Master
records into publication scope, keeps `Not Publishing` records as tracked
information, and reports `Decision Required` as structural ambiguity. Mark and
undo lock the Master plus mapped Final rows, validate signed evidence, update
the Master decision, and synchronize legacy Final exclusion fields in one
transaction. Later imports and Editor Uploads apply the same mirror.

New Master creation is centralized in
`create_paper_master_with_publication_guard()`. Existing orphan exclusion
evidence produces Decision Required instead of default Publishing. Mapped Final
mirror disagreement does not change scope or override the Master, but the
shared integrity checker blocks final, draft, and CrossCheck export. Decision
Required preserves legacy mirror evidence until an explicit decision
synchronizes it. Verify/remap checks the original and target Paper ID groups
before changing the ID. Mixed legacy Final decisions are inspected only for
records outside Paper Master, where no authoritative Master decision exists.
Start2/Editor conflict groups continue to use database conditional aggregation
and load details only for actual conflicts.

`FileInspectionContext` reuses request-local filesystem observations for normal
reads. SHA-256 results may be reused across requests only when device, inode,
size, mtime, and ctime all match. A strict fresh hash re-stats the path even
inside an existing context, and hashing verifies the signature again after
reading, so a file changed during inspection is rejected.

### Export Transaction

Final Publication Package export keeps the same `PublicationReadContext` from
readiness validation through manifest construction and ZIP assembly. PDF/source
entries are written from `FileInspectionContext.read_snapshot_bytes()`, which
rejects a path whose full filesystem signature changed after inspection.
Manifest and warning CSV bytes are immutable ZIP inputs rather than files that
are re-read after creation. The package is written to a temporary path, reopened
for entry and CRC verification, checked against the final database signature,
and atomically promoted only after every check succeeds.
Export also blocks when Paper ID/title sanitization would produce the same
case-insensitive ZIP base name for more than one publication record. Therefore
validated files cannot be silently replaced or overwritten by a later path
read or duplicate archive entry.

Editorial Excel reports are read-only views built separately from package
assembly. `reports.py` may reuse `PublicationReadContext`, readiness rows, and
exception rows to describe the current state, while `excel_workbook.py` owns
XLSX-only presentation such as headers, widths, wrapping, filters, and number
formats. `Publication Detail` is the mandatory editorial-workbook core;
supporting sheets are selected from an explicit whitelist, while raw active and
old-version data remain separate debug exports. Excel formatting and report
sheets must not feed publication selection or modify Final/Draft package CSV
schemas.

All Excel exports use one atomic audited transaction. They load a stable
`PublicationReadContext`, compute author/report rows without rebuilding
`PaperAuthor`, write a `.part.xlsx`, force formula-like report values to literal
text, reopen the workbook to validate its exact sheet set, recheck the database
snapshot, and then rename it into place. Requested, successful, and failed
events use the same canonical report action. If final audit persistence fails,
the promoted workbook is removed so an unaudited report is never left behind.
The Editorial Publication Workbook reuses the same context, author rows,
readiness rows, exception rows, Paper Master rows, and Not Publishing snapshot
for all selected sheets.

Final export also fingerprints publication-critical database state before
loading the snapshot and after ZIP assembly. A concurrent editor change to
Paper Master, submissions, settings, or author waivers deletes the
partial output and requires a fresh export. Publication source bytes are bound
to a completed Formatting Review by `source_hash`. A Pending or Needs Edit
Formatting status is blocked by `Formatting Not Review OK` and is not also
reported as a missing review hash. Once status is Review OK, a missing or
changed source hash is a Critical integrity blocker. A configured Corrected
PDF/source that is missing never falls back to the Original file.

### Stale-Write Protection

Formatting writes use a second ephemeral review snapshot containing the
submission update timestamp and filesystem identity of the selected publication
PDF/source. Save revalidates that snapshot under a database row lock before
calling the central formatting update service. Corrected-PDF title-guard
previews carry the same snapshot plus hashes of their temporary uploads, so a
later confirmation cannot bind an old review decision to changed publication
files. Preview SHA-256 is accumulated while the upload is streamed to temporary
storage; confirmation still performs a fresh independent hash. Abandoned
Editor Upload and Formatting preview directories expire after two hours, and a
changed preview is removed when it is rejected. Queue/review/title-guard tokens
are temporary workflow state and are not part of System State backup.
TTL cleanup removes only directories with a complete parseable payload; it
does not guess that a directory still being built by another request is stale.

`workflow_evidence.py` supplies signed, expiring digests for other multi-editor
mutation boundaries. Final Submission Edit, Paper Master Edit, Title/Author
Review, Exceptions, Settings, and Process PDF formatting triage compare the
submitted digest with current locked state. Settings active-rule confirmation
also locks the candidate set before recomputing active/duplicate state. Tokens
are generated only after pagination and require no database or file I/O. Paper
ID review canonicalizes the Paper Master evidence once per response and reuses
its digest in each displayed-row token; POST validation recomputes that digest
after locking the current Master rows.

Final import apply does not trust preview paths. Each selected upload is copied
into an operation-unique staging file while size and SHA-256 are checked against
preview evidence; FileField storage reads only that validated copy.
Built-in/GROBID extraction and Process PDFs capture semantic row/file evidence
before long work and lock/recheck before persistence. PDF thumbnails are
rendered into unique immutable directories so a rejected stale batch cannot
overwrite a newer editor's visible preview.

### Read-Only Reporting And Upload UI

Error Report keeps duplicate categories and blocker messages unchanged in the
readiness/report services. Its HTML worklist uses a compact duplicate-group
summary and a read-only HTMX detail endpoint for the full matching-record list,
preventing `page_size=all` from repeating an O(n) group description in every
row. Workflow-area filtering runs first. Category selections are validated
against that area, severity and category filtering are then applied on the
server, and sorting/pagination operate only on the selected result. Multiple
categories use OR; area, severity, and category dimensions use AND. Category
pill counts use the current area/severity rows before category filtering, while
severity tab counts reflect the current category selection. Repeated `category`
query parameters remain shareable and survive pagination. The detail endpoint
has a complete non-HTMX fallback page.

All paginated worklists render one shared pagination component above and below
their rows. The `WorklistPage.scroll_anchor` identifies the stable worklist
container. Normal links use the anchor fragment, while the shared HTMX handler
scrolls the swapped worklist into view after a successful page change.

Final Submission and Paper Master upload zones are presentation helpers only. File extension/hash validation and preview/apply remain server-owned. The browser may summarize selected files or remove them before submit, but cannot classify publication files or bypass import preview.

Presentation tokens, alert behavior, accessibility, and shared component rules
are centralized in `base.html` and documented in
[UI Conventions](ui_conventions.md). These are presentation boundaries only:
they must not alter publication scope, readiness, active versions, review state,
or file selection.

## Publication Read And Export Path

The canonical selection rules live in
[Publication Rules](publication_rules.md). Architecturally, publication reads
follow one path:

1. `PublicationReadContext` captures Paper Master scope, settings, active
   submissions, readiness inputs, and request-local file inspection.
2. Active-version services select the current undiscarded candidate by origin
   priority and the configured ordering rule.
3. `publication_pdf_info()` and `publication_source_info()` resolve the
   publication-facing files.
4. Readiness, Dashboard, Organized List, Error Report, CrossCheck, duplicate
   checks, and export reuse that shared scope instead of reconstructing it.
5. Final export performs strict fresh file reads, rechecks publication-critical
   database state, verifies the ZIP, and atomically promotes the result.

`FinalSubmission` remains the compatibility source of truth while its
one-to-one state tables mirror lifecycle domains. `PaperAuthor` is a
compatibility cache, not publication authority. Legacy current-path and
generated debug-copy fields remain diagnostic or restore-compatibility data and
never select package input.

## File And Path Safety

### Managed State And Restore

Managed files live under the project `data/` tree by default. Database fields may store file paths, but System State export/restore must remap managed paths into the receiving project folder. The snapshot includes referenced review artifacts such as title/author verification images, PDF thumbnails, and format previews because they preserve manual review context.

### Docker Runtime Boundary

Docker runs `web` behind the published Nginx `proxy`. Conference state lives
in the project-scoped `sms_data` volume; `sms_static` is rebuildable and
`sms_gateway_state` is temporary operational status. The proxy serves
static/media, preserves the browser-visible Host header, buffers large dynamic
responses without caching them, and shows a safe GET-only recovery page while
web is unavailable.

The proxy cannot write conference data and does not participate in publication
scope, file resolution, readiness, or export. Raw host mirrors are verified
operational rollback copies, while System State ZIPs are portable, versioned
application backups.

Deployment, update, backup, migration, and rollback procedures are centralized
in the [Docker Guide](docker_guide.md).

Do not preserve machine-specific absolute paths in restored state. Snapshot manifests may include portable path references and hashes, but restore must reject corrupted or unsupported archives. Temporary preview token folders are excluded from snapshots.
Restore extracts and verifies files into sibling staging directories before the
database transaction begins. Live files move to quarantine only after model
restore succeeds; Python, database-commit, or filesystem failures restore the
quarantine and roll back the database. Staging and quarantine live on the
target filesystem so promotion uses rename rather than cross-device copying.

### Storage Inventory And Cleanup

Storage cleanup is split by risk:

- Conservative cleanup removes only unreferenced regenerated cache. It does not delete publication debug, legacy active-final, or old-version output folders.
- Generated reports/exports cleanup removes regenerated Excel/ZIP downloads,
  generated publication manifest/warning CSV files, and external upload
  packages. Arbitrary editorial CSV files in the Reports folder are retained.
- Original uploads, corrected uploads, plagiarism report PDFs, system state backups, and referenced thumbnails/previews are retained.

`submissions/services/storage_inventory.py` builds one request-scoped
`StorageReferenceIndex` from the path fields needed by the inventory. Exact file
references use normalized canonical-path set lookup. Directory references such
as `thumbnail_folder` use a separate tree-path set and parent lookup, so every
file below a referenced directory remains protected. Existing references also
carry device/inode identity as a fallback, preventing case-only path spelling
differences on macOS from turning the same file into an orphan. The filesystem is scanned
once into immutable file records containing canonical path, category, and size;
inventory classification then uses those records without repeated `stat()` or
database queries. This keeps inventory work proportional to database references
plus managed files instead of comparing every file with every reference.
When configured roots overlap, category assignment uses an explicit protection
priority: canonical/corrected files and reports/backups outrank managed output,
and every publication-managed category outranks generated cache. Classification
must never depend on dictionary or root iteration order. Category and total
counts use that single primary classification, so overlapping roots do not
double-count one file.
Cleanup previews also bind each candidate to its filesystem identity. Apply
skips a candidate if the path now resolves to a different file, even when it is
still unreferenced, and rebuilds current classification so folder-setting
changes cannot turn a newly protected file into a stale cleanup target. Known
non-regenerable subtrees such as System State backups,
import/restore previews, extraction results, plagiarism reports, and managed
media remain protected if a configurable Reports folder overlaps them.
Per-file filesystem deletion errors are recorded as skipped items so a batch
never returns an opaque 500 after partially succeeding.
During a long SQLite cleanup batch, `PRAGMA data_version` is checked before
each candidate. A commit from another editor request rebuilds the reference
index and current candidate classification before deletion continues.
Unreadable managed roots are returned as explicit scan errors. Inventory still
renders the readable results, but cleanup preview fails closed until the scan is
complete. Apply also requires a complete fresh scan before deleting its first
candidate. Files that cannot be deleted are kept and counted in the UI/audit;
preview-file or empty-directory housekeeping failures are audited without
turning already completed candidate processing into an opaque server error.
Creating a cleanup preview removes expired or malformed temporary preview JSON
files while retaining every unexpired token.

The Settings form does not build this inventory during its main request.
Storage Management is loaded from `/ui/storage-inventory/` after the page opens,
and GROBID health uses its existing JSON endpoint. The inventory is not cached
across requests because paths and bind-mounted files may change independently
of Django. Cleanup preview builds a current inventory, and cleanup apply builds
a fresh reference index before deleting anything; a file newly referenced
after preview is skipped.
The storage endpoint renders only the panel for HTMX requests and a complete
base-layout page for ordinary GET/no-JavaScript navigation.
Settings, middleware context, and storage inventory use a non-persisting
default settings object when the singleton row does not exist. Read-only GETs
therefore do not initialize database state; the row is created only by a write
workflow or explicit reset.

`SelectiveGZipMiddleware` compresses only an explicit allowlist of dynamic
HTML, text, JSON, JavaScript, and XML MIME types. ZIP, PDF, image, Office, and
unknown binary responses bypass dynamic gzip; this avoids recompressing archive
downloads and preserves their `Content-Length` for proxies and tunnels.

Plagiarism exceptions are per FinalSubmission publication-version decisions.
`Plagiarism %` and `Single %` exceptions are approved separately, require a
reason, and are valid only while the current score still matches the approved
score. They affect readiness/export blocking but do not change the score
itself. Their current status, values, limits, and reason appear in the
Publication Package manifest's shared `Exceptions` summary.

Organized List exposes row-level exception panels for page count, authors-in-paper, plagiarism scores, and duplicate-author review. Those panels reuse the same exception service rows and approve/remove commands as Exceptions Center. Author paper-count exceptions remain author-level records and are not attached to a single Organized List row.

Each Organized List checklist record is a stable per-submission table body.
An exception action remains a normal audited Django POST, then reloads and
hydrates that complete row from current database/file state. HTMX selects only
the matching row body from the server-rendered response. Other unsaved reason
fields are submitted as typed drafts; the controller reattaches a draft only
when no stored reason exists, while stored data, successful remove/reset, and
validation errors follow explicit server-side precedence. The browser never
calculates exception validity or merges persisted workflow state.

## Audit Log

Audit logging is file-based, not database-backed. The active log is `data/logs/audit.log`, written as JSON Lines. Keeping it outside the database lets Clear Database preserve the trail by default.

Each event includes timestamp, event ID, app version, state archive version,
actor (`local_user`), action, status, request path, Paper ID, Final Submission
ID, changed fields, before/after snapshots, reset flags, file changes, hashes,
result counts, and error text when applicable. Canonical actions are registered
in `submissions/services/audit_actions.py` and use
`<domain>_<operation>[_<phase>]`. Result words such as success, failed, and
blocked belong in `status`, not the action name.

Use `submissions/services/audit.py` for all audit writes. Do not open-write the log directly from controllers or other services. File paths in events must be portable: use project/media-relative paths, hashes, sizes, and filenames instead of machine-specific temp paths or binary content.

The default Audit Log view reads a bounded UTF-8 tail. A non-empty search or
structured category/action/status filter may scan the complete append-only
file. Legacy action aliases are resolved only while reading; the original JSON
is never rewritten. Archive filenames include microseconds and a random
identity so repeated operations cannot replace an earlier log.

System State backup includes `data/logs/audit.log` and `data/logs/archive/*.log`. Restore brings those logs back with the rest of the managed state. Temporary preview tokens, including `data/record_edit_previews/`, are excluded.

`record_edit_preview.py` owns the common two-step edit boundary for existing
Paper Master and Final Submission records. It stores validated proposed values
and staged uploads for two hours, compares files by SHA-256, and revalidates
signed database evidence plus current-file hashes on apply. It never selects a
publication version and never applies reset rules itself; confirmed changes
flow through `paper_master.py` or `manual_edit.py` inside their existing atomic
transactions.

Clear Database writes `database_clear_request` first. If the audit-clear
checkbox is selected, it archives the current log, creates a new log with
`audit_log_archive_clear`, and then writes `database_clear_complete` after the
wipe succeeds.

Django admin registrations for Paper Master, Final Submission, Settings,
author waivers, and `PaperAuthor` are read-only. Admin must not become an
unaudited mutation path around editorial services.

Clear Database never recursively empties arbitrary configured absolute folders.
Only app-owned paths below `BASE_DIR/data` or the configured application
`MEDIA_ROOT` are staged. Staging uses same-filesystem sibling directories; the
database transaction runs while those files remain recoverable, failed database
deletes restore them, and successful commits then remove the staged content.
Existing configured external folders are preserved and reported in the result
and audit event.

## Versioning

The app version is defined in `conference_final_manager/settings.py` as `APP_VERSION`.

The System State archive format is defined separately as `STATE_ARCHIVE_VERSION`. Increment the archive version only when backup/restore structure or compatibility changes. Increment the app version for user-visible behavior, workflow, docs, or schema changes.

The footer displays both values so a user can match a System State ZIP to the expected application version.

Archive version 5 matches the schema after removal of obsolete Mapping Table
metadata. Archive version 4 introduced authoritative Paper Master publication
decisions. Older archives must be restored with a compatible application before
upgrading; treating missing decision state or removed fields as equivalent
would be unsafe.

## Optional GROBID Fallback

The built-in title/author extractor remains the primary extractor. `submissions/services/grobid_extractor.py` is an optional fallback client for trusted local/internal GROBID services and is disabled by default in `AppSetting`.

GROBID extraction is never a publication-ready shortcut. Successful GROBID results write to the same extracted title/authors fields, create a verification image under `data/media/title_author_verification/`, reset Title/Author Review to Pending, and recalculate Extracted Title Match with the same normalized-title logic used by the built-in extractor. Manual Review OK is still required before final export. Failed GROBID attempts must not modify existing extracted data.

GROBID actions run an `/api/isalive` health check before extraction. Single-row extraction skips without changing the row if the API is unavailable. Batch suspicious-row extraction checks once before processing and aborts the batch with zero row errors when the service is unavailable. Batch rows are processed sequentially, not in background threads; if connection or timeout errors indicate the service became unavailable mid-run, the batch stops and counts the current/unprocessed rows as skipped.

Manual title/author override is implemented as a first-class exception workflow in the title/author service, not as ordinary Final Submission editing. It writes `title_author_source=manual_override`, stores a required reason/time, creates a new verification image when a PDF is available, resets review-dependent flags, and logs before/after values. Re-extraction or PDF/source changes clear manual override metadata.

Metadata extraction and evidence rendering are deliberately separate.
`builtin_title_author_extractor.py` keeps the established built-in metadata
heuristics. Built-in, GROBID, and Manual Override all pass their resulting
title/authors to `title_author_verification.py`, which is the only verification
renderer. It uses a conservative grayscale scan of the rendered first-page
upper area to measure genuinely blank top space. The dynamically sized header
uses that space first and shifts the source page down only by the remaining
required height. A fixed safety gap keeps the header away from the first
non-white PDF content; uncertain or occupied space is never reused. Title
evidence keeps its normalized word-sequence locator. Author evidence uses a
separate case-sensitive character locator. Internal extracted words and
punctuation must match; the final extracted word may be a prefix of the PDF
word so attached affiliations, ORCIDs, separators, and alphabetic continuations
cannot suppress otherwise visible evidence. Its outline is built solely from
raw PDF character boxes corresponding to extracted characters. Numeric or
symbolic trailing metadata remains green; an alphabetic continuation is orange
to expose a partial word such as `Smith` in `Smithson`. Unavailable or ambiguous
geometry produces no author outline rather than a whole-word fallback. This
evidence rule does not modify stored extraction. Title evidence uses yellow
marking plus blue underlines; each parsed author receives an independent
outline/underline and a numbered header legend. If an author has any complete
green match, orange partial matches for that author are suppressed. The header
legend reflects the selected evidence state: green for complete/metadata,
orange for partial-only, and red when no evidence was found.

The Title/Author worklist keeps its large verification images lazy-loaded and
reads each PNG header for its actual intrinsic dimensions, avoiding distortion
and unnecessary image decode/layout work even though header height varies.
Manual Override forms are loaded from a read-only HTMX partial only when an
editor expands that action; the state-changing submission still posts through
the audited Title/Author controller and service.

## Regression Gate

Run these checks before merging or handing off changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```

For documentation-only changes, `check` and `makemigrations --check --dry-run` are usually enough, plus a link/stale-term review.
