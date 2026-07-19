# Architecture Notes

Conference Final Manager is a local Django application with SQLite storage and local file management. The application is intentionally no-login and single-machine.

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

`FinalSubmission` remains the compatibility record and behavior source of truth. Newer one-to-one state models mirror lifecycle domains:

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

- Paper Master List is the publication scope.
- Final Submissions can come from Start2 imports or Editor Uploads.
- Editor Uploads are prioritized over Start2 records, but unresolved Start2/Editor conflicts block final publication export until one side is discarded.
- Editor Upload and corrected-PDF formatting uploads share one server-rendered title
  safety component. Services build a common comparison payload, templates render the
  uploaded title once with vertically stacked references, and character-level detail
  remains collapsed by default. Preview files stay temporary until apply; opening,
  replacing, or canceling a preview never changes publication state.
- Discard is version-level: it excludes one Final Submission version but does not mean the paper is not publishing.
- Not Publishing is paper/publication-decision-level: it keeps records for traceability but excludes the paper from publication readiness and package output.
- Publication PDF priority is corrected PDF, then original author PDF.

Dashboard readiness is derived from `publication_readiness_rows()`, the same service used to block Final Publication Package export. Controllers may group those rows for display, but must not recreate publication-blocking rules with independent counters. Dashboard workflow counts represent unique affected papers; the readiness header separately reports the number of individual blocker rows.
- Publication source priority is corrected source, then original source.
- Active version selection is previewed before changing the active-version rule in Settings.
- Import/re-upload workflows are preview-before-apply when they may change existing records or files.
- Review flags are reset only when dependent data changes.
- Final Submission Edit owns submission metadata, original files, and P/S score/report entry. Processing, Title/Author Review, duplicate-author review, and Not Publishing decisions are read-only there and are changed only through their dedicated workflows.
- Manual Final Submission creation and editing are separate service operations. `create_final_submission_manual()` accepts only an unsaved form instance and owns initial Paper ID evaluation, file-path initialization, Pending review state, active/duplicate recalculation, and create audit logging. `apply_final_submission_manual_edit()` requires an existing record and applies dependency-based reset rules; it must never receive `None` or synthesize an original record.
- Editorial worklists preserve navigation context when they link into Final Submission Edit. Organized List, Formatting Review, Title/Author Review, Not Publishing, Verify Paper IDs, and Exceptions pass a return URL that is restricted to the local host. The legacy Publication Candidates URL redirects to Organized List compact mode.
- Cross-page record navigation is separate from search. System-generated links
  identify a Final Submission by database primary key, a Paper Master record by
  exact Paper ID, or an exception by its service-generated key. Controllers
  build a shared focused-worklist context and services keep their normal
  publication scope. If the exact target is outside that scope, the UI reports
  why; it never substitutes another fuzzy match. GET focus modes are read-only.
- Final Submission Edit separates editable identity/metadata/files/plagiarism data from a read-only workflow summary. Its normal Save form is structurally separate from the collapsed bottom version-action danger-zone form. Discard and undo continue to call the existing audited service; Not Publishing remains owned by its dedicated workflow.
- Formatting Review exposes a compact queue plus a full Single Paper Mode. Queue rows show publication file/status context before expansion, Bootstrap's shared parent keeps one paper expanded at a time, and HTMX enhances GET-only filter/search navigation without owning workflow state.
- Process PDFs deliberately keeps complete page-thumbnail strips expanded. Search and `Needs processing / Page issues / Processed / All` filters narrow papers only; paper jump, sticky identity headers, fixed thumbnail geometry, lazy image loading, and the enlarged preview modal do not change processing scope.
- Organized List separates current-view publication blockers from tracked information and uses stable table columns. Paper Master rows whose active final is Not Publishing are omitted from this publication-current view, while replaced versions remain inactive history. Final Submissions keeps its Import/Re-upload workflow collapsed until requested.
- Organized List owns both the full Checklist and Compact candidates views. This removes a second publication-current UI implementation while preserving `/reports/active-versions/` as a compatibility redirect.
- `Review OK` is the single Title/Author completion decision. The Final-versus-extracted title comparison remains visible evidence; a reviewed difference is tracked but does not create a second blocker.

The UI remains server-rendered. Tabler 1.4.0 and HTMX 2.0.10 are vendored
locally. Normal worklist URLs support GET filter/search/tab/pagination
navigation, while HTMX replaces the named worklist container and updates
browser history. Dashboard readiness and global workflow alerts are separate
read-only partial endpoints so their global scans do not delay unrelated page
content. Global alerts may use a short display-only cache; publication
readiness and exports never do. State-changing POST actions remain normal
audited Django requests.

Large worklists use the shared `WorklistPage` boundary. The complete lightweight
scope is classified and sorted first, then the selected `50 / 100 / 200` page
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

`PublicationReadContext` is the request-scoped read boundary for Paper Master,
active Final Submissions, settings, and filesystem inspection. Dashboard
counts, publication readiness, duplicate detection, Error Report, and global
workflow alerts reuse this context rather than loading independent publication
scopes. It is immutable request data, not a publication cache, and GET requests
do not persist derived state.

`FileInspectionContext` reuses request-local filesystem observations for normal
reads. SHA-256 results may be reused across requests only when device, inode,
size, mtime, and ctime all match. A strict fresh hash re-stats the path even
inside an existing context, and hashing verifies the signature again after
reading, so a file changed during inspection is rejected.

Final Publication Package export keeps the same `PublicationReadContext` from
readiness validation through manifest construction and ZIP assembly. PDF/source
entries are written from `FileInspectionContext.read_snapshot_bytes()`, which
rejects a path whose full filesystem signature changed after inspection.
Export also blocks when Paper ID/title sanitization would produce the same
case-insensitive ZIP base name for more than one publication record. Therefore
validated files cannot be silently replaced or overwritten by a later path
read or duplicate archive entry.

Final export also fingerprints publication-critical database state before
loading the snapshot and after ZIP assembly. A concurrent editor change to
Paper Master, submissions, settings, author rows, or author waivers deletes the
partial output and requires a fresh export. Publication source bytes are bound
to Formatting Review by `source_hash`; missing or changed source hashes block
export. A configured Corrected PDF/source that is missing never falls back to
the Original file.

Error Report keeps duplicate categories and blocker messages unchanged in the
readiness/report services. Its HTML worklist uses a compact duplicate-group
summary and a read-only HTMX detail endpoint for the full matching-record list,
preventing `page_size=all` from repeating an O(n) group description in every
row. Rows are sorted by severity before pagination, tab badges retain full-list
totals, and the detail endpoint has a complete non-HTMX fallback page.

Final Submission and Paper Master upload zones are presentation helpers only. File extension/hash validation and preview/apply remain server-owned. The browser may summarize selected files or remove them before submit, but cannot classify publication files or bypass import preview.

Color and typography are centralized in `base.html`. Red is reserved for publication blockers/danger, amber for manual attention, blue for tracked/informational state, green for completed review, and gray for inactive/history. Semantic fills are deliberately muted so dense worklists do not become a collection of competing color blocks. The same tokens drive page background, muted cool-gray surfaces, cards, tables, forms, tabs, badges, alerts, diff panels, buttons, navbar, footer, inline code, and expanded code/JSON blocks. Large work surfaces intentionally avoid pure white: the page background is darkest, cards/tables use a middle surface, headers provide another visible layer, and editable controls are only slightly lighter. The fixed type scale uses 15px body/table text, 14px labels/buttons, 13px supporting text, and 12px badges; `.small` is rem-based so nested helpers cannot shrink repeatedly. Primary text uses dark ink, while secondary/help text uses a darker blue-gray instead of low-contrast gray. Inline code and multi-line JSON use explicit dark foreground colors on the same muted surface family; do not rely on Tabler's `pre` or theme-dependent code colors. Text labels always accompany color. Non-interactive status/category badges are compact borderless pills with no shadow; actionable buttons are taller rectangles with stronger borders, shadow, focus, and hover behavior. Button hierarchy is explicit: primary commands use solid fills, ordinary outline actions use a lightly tinted surface with a strong border, solid success/danger commands use dark semantic fills with white text, warning commands use dark text on amber, semantic actions retain their named color, and disabled controls remain visibly inactive. All tables use uniform row surfaces with horizontal separators and hover-only highlighting; zebra striping and its record-index classes are intentionally absent, so expandable child rows cannot disrupt row color. Organized List uses a red left-edge marker plus explicit issue labels for blockers, while routine author count, page OK, and original-source states stay neutral. The application header separates system/conference identity from workflow navigation. Its light navigation strip uses explicit high-contrast hover, focus, active, mobile-collapse, and dropdown states instead of relying on framework defaults; dropdown descriptions clarify destination purpose without changing route ownership.

Alert layout is centralized in `base.html` as well. Tabler's default horizontal
alert flexbox is overridden so ordinary alerts use vertical document flow.
Templates opt in with `.d-flex` only for a short message/action row. Alerts
containing tables, lists, confirmation forms, or several content blocks use
`.cfm-alert-stack`, which keeps their children and responsive tables at full
available width.

## Current Publication Resolution

Current active-version selection is implemented in `submissions/services/pdf_processor.py`.

1. All `active_version` flags are cleared.
2. Discarded submissions are excluded.
3. Submissions are grouped by `paper_id_filled`.
4. If a group has undiscarded Editor Uploads, only Editor Uploads are candidates.
5. Otherwise all undiscarded submissions for that Paper ID are candidates.
6. The selected candidate is determined by Settings:
   - `final_id`: numeric/natural Final ID sort.
   - `upload_date`: upload date, with Final ID sort as tie-breaker.
7. State mirror tables and `PaperAuthor` cache are refreshed after active selection.

Workflows that also recalculate duplicate/replaced status call
`recompute_active_and_duplicate_state()`. It computes both values in memory,
bulk-updates the compatibility table, bulk-syncs only Identity state, and
rebuilds the author cache once. Publication scope and Editor Upload priority
are unchanged.

Publication file resolution is implemented in `submissions/services/file_manager.py`.

`source_pdf_path()` is used for processing/extraction input and resolves:

1. Corrected PDF.
2. Original uploaded PDF.

`publication_pdf_info()` is used for publication-facing links, CrossCheck export, duplicate checks, and publication package export. It currently resolves:

1. Corrected PDF.
2. Original uploaded PDF.

`publication_source_info()` resolves corrected source, then original uploaded source.

This distinction matters. Process PDFs recalculates active versions and then calculates page/hash/thumbnails only for current, non-discarded, non-Not-Publishing submissions whose Paper ID is in Paper Master. It may sync `data/publication_pdf_debug/` for inspection, but that debug folder is not read by publication package export, CrossCheck export, duplicate checks, or publication-facing links.

Legacy `current_file_path`, `source_current_file_path`, `active_final_folder`, and `old_versions_folder` values are retained for compatibility with older restored data and debug traces. They are not publication source-of-truth fields.

## File And Path Safety

Managed files live under the project `data/` tree by default. Database fields may store file paths, but System State export/restore must remap managed paths into the receiving project folder. The snapshot includes referenced review artifacts such as title/author verification images, PDF thumbnails, and format previews because they preserve manual review context.

Docker deployments mount a Compose project-scoped named volume at `/app/data`.
The separately configured `SMS_DATA_DIR` is a raw, directly mountable host
mirror maintained by the Docker backup script. Migration and backup use
verified two-phase synchronization: an online pre-copy followed by a brief
graceful stop for the final database/file-consistent sync. Mirror promotion
occurs only after SHA256 comparison and SQLite integrity validation, and the
previous complete mirror is retained. This operational mirror does not change
System State archive structure or publication-facing file resolution.

Do not preserve machine-specific absolute paths in restored state. Snapshot manifests may include portable path references and hashes, but restore must reject corrupted or unsupported archives. Temporary preview token folders are excluded from snapshots.

Storage cleanup is split by risk:

- Conservative cleanup removes only unreferenced regenerated cache. It does not delete publication debug, legacy active-final, or old-version output folders.
- Generated reports/exports cleanup removes regenerated Excel/ZIP downloads and external upload packages.
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

Plagiarism exceptions are per FinalSubmission publication-version decisions. `Plagiarism %` and `Single %` exceptions are approved separately, require a reason, and are valid only while the current score still matches the approved score. They affect readiness/export blocking but do not change the score itself or the final package manifest.

Organized List exposes row-level exception panels for page count, authors-in-paper, plagiarism scores, and duplicate-author review. Those panels reuse the same exception service rows and approve/remove commands as Exceptions Center. Author paper-count exceptions remain author-level records and are not attached to a single Organized List row.

## Audit Log

Audit logging is file-based, not database-backed. The active log is `data/logs/audit.log`, written as JSON Lines. Keeping it outside the database lets Clear Database preserve the trail by default.

Each event includes timestamp, event ID, app version, state archive version, actor (`local_user`), action, status, request path, Paper ID, Final Submission ID, changed fields, before/after snapshots, reset flags, file changes, hashes, result counts, and error text when applicable.

Use `submissions/services/audit.py` for all audit writes. Do not open-write the log directly from controllers or other services. File paths in events must be portable: use project/media-relative paths, hashes, sizes, and filenames instead of machine-specific temp paths or binary content.

System State backup includes `data/logs/audit.log` and `data/logs/archive/*.log`. Restore brings those logs back with the rest of the managed state. Temporary preview tokens are still excluded.

Clear Database writes `clear_database_requested` first. If the audit-clear checkbox is selected, it archives the current log, creates a new log with `audit_log_archived_and_cleared`, and then writes `clear_database_applied` after the wipe succeeds.

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

## Optional GROBID Fallback

The built-in title/author extractor remains the primary extractor. `submissions/services/grobid_extractor.py` is an optional fallback client for trusted local/internal GROBID services and is disabled by default in `AppSetting`.

GROBID extraction is never a publication-ready shortcut. Successful GROBID results write to the same extracted title/authors fields, create a verification image under `data/media/title_author_verification/`, reset Title/Author Review to Pending, and recalculate Extracted Title Match with the same normalized-title logic used by the built-in extractor. Manual Review OK is still required before final export. Failed GROBID attempts must not modify existing extracted data.

GROBID actions run an `/api/isalive` health check before extraction. Single-row extraction skips without changing the row if the API is unavailable. Batch suspicious-row extraction checks once before processing and aborts the batch with zero row errors when the service is unavailable. Batch rows are processed sequentially, not in background threads; if connection or timeout errors indicate the service became unavailable mid-run, the batch stops and counts the current/unprocessed rows as skipped.

Manual title/author override is implemented as a first-class exception workflow in the title/author service, not as ordinary Final Submission editing. It writes `title_author_source=manual_override`, stores a required reason/time, creates a new verification image when a PDF is available, resets review-dependent flags, and logs before/after values. Re-extraction or PDF/source changes clear manual override metadata.

## Regression Gate

Run these checks before merging or handing off changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```

For documentation-only changes, `check` and `makemigrations --check --dry-run` are usually enough, plus a link/stale-term review.
