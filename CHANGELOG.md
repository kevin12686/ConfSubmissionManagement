# Changelog

This file records user-visible releases. Detailed implementation history remains
available in Git.

## 1.24.1 - Unreleased

- Validate native Python/venv, dependencies and actual comparison model sessions
  before migrations or opening the browser. Service deployments authenticate
  and validate worker compatibility/readiness instead of discovering missing
  prerequisites on the first comparison.
- Add a standalone worker deployment launcher with disposable CPU/CUDA runtime
  preflight and bounded health waiting. Failed candidate checks leave the
  existing worker running; no models or credentials are silently generated.
- Check candidate conference images before create/update/migration cutover,
  and check restart prerequisites before stopping a new-image instance for
  backup. Preserve legacy-image backup behavior and conference data.
- Publication file selection, OCR quality, comparison timeouts, review rules
  and State archive v8 are unchanged.

## 1.24.0 - Unreleased

- Separate comparison compute into an independently deployed, authenticated
  CPU/GPU worker shared by multiple conference installations. Keep native
  embedded CPU operation available and make deployment settings explicit.
- Add client-isolated persistent job/cache storage, bounded scheduling, an
  idempotent HTTP protocol and pre-acceptance remote-to-local routing. A lost
  response stays pinned to its original job; busy does not mean unavailable.
- Validate input hashes, engine contract, result schema and inspection-render
  hashes before accepting remote evidence. Recheck current publication scope,
  PDF bytes and upload revision after compute, as before.
- Keep the comparison UI and publication/readiness/reset rules unchanged.
  Worker data and credentials are deployment state, not conference backups.
  State archive remains v8; comparison method remains 7.

## 1.23.0 - Unreleased

- Reuse signed, byte- and runtime-bound CPU OCR evidence across saved PDF
  comparisons; share identical-pair deduplication between manual and automatic
  builds. Retain a completed original extraction even if the corrected side fails.
- Pass the same rendered BGR pixels directly to RapidOCR instead of encoding and
  decoding PNG between rendering and recognition. Keep models, DPI, batching,
  threads, time/resource limits and method-7 comparison rules unchanged.
- Bound the optional local cache to seven days, 512 MiB and 512 entries. Exclude
  it from System State, protect it from ordinary cleanup and clear its contents
  with Clear Database. Cache failures fall back to fresh computation, not approval.
- Preserve current-file/revision checks on every cache hit. State archive v8,
  publication selection, readiness, review flags and stored source files are unchanged.

## 1.22.1 - Unreleased

- Allow an explicit, validated `CFM_RAPIDOCR_THREADS` deployment setting (1-8,
  within visible CPU capacity), retained by Docker instance recreation. Keep
  rendering, models, comparison rules and the 240-second wall limit unchanged.
  Account for all supported threads in the worker's finite CPU-time limit.
- Fix Esc/Control dismissal of hover inspection when focus remains outside the
  comparison controls. Exclude experimental output/browser artifacts from images.
- State archive v8 and publication selection/review rules remain unchanged.

## 1.22.0 - Unreleased

- Advance temporary PDF comparison to method 7 with rendered-page RapidOCR,
  native geometry used only for reading-order guidance, unchanged-context
  alignment and bounded small-edit comparison. Rebuild method-6 and older results.
- Integrate all 38 approved direct visual-tolerance groups. Retain raw OCR,
  locations, rule IDs and risks as inspectable information. Real similar-character
  edits and whitespace-only changes remain explicit accepted blind spots;
  unlocated/empty-OCR ranges require manual review. No equality or safety claim
  follows from a lower finding count.
- Make annotated documents the primary comparison view. Hover, keyboard and
  touch inspection show page-pixel crops without moving the documents; explicit
  result navigation remains available. Use red text changes, amber unlocated
  ranges and neutral information, with only the active finding connected.
- Replace superseded method-6 rules in active documentation; retain historical
  release notes. Research/prototype verification is not production acceptance.
- Set app version 1.22.0. State archive v8, publication helpers, saved PDF bytes,
  review/readiness rules, upload revision binding and audit requirements remain
  unchanged. Release test and browser acceptance results must be recorded separately.

## 1.21.1 - Unreleased

- Advance temporary comparisons to method 6, requiring method-5 and older
  results to be rebuilt instead of reusing earlier evidence classifications.
- Classify supported connected table rulings and junctions as graphics, retaining
  their coverage limits. Resolve repeated row labels only within a uniquely
  corresponding complete ordered table, without skipping cell-content checks.
- Separate native boundary extraction from `ActualText` presentation substitutions
  and retain span evidence. Keep unresolved script attachment distinct from a
  supported baseline role, preserving genuine structural changes.
- Require actual-engine, content-preserving metadata transformations and mutation
  checks; a passing test total or lower warning count alone is not semantic acceptance.
- Pair unique vector occurrences using exact bounded page/resource-source
  identity plus identical appearance and position, including metadata/ID-only
  rewrites. Unsupported source graphs provide no proof; Unicode and graphics
  coverage limitations remain. This static-paint proof does not validate hyperlink
  navigation. Genuine script-role changes and unproved
  vector-to-font substitutions still require review.
- Consolidate the evidence contract in `docs/pdf_comparison.md`. Keep explicit
  coverage limitations, exact-evidence requirements and manual review; neither
  zero findings nor complete supported checks certifies full math or images.
- Set app version 1.21.1. State archive v8, publication selection, saved files,
  upload revision binding and review state remain unchanged by comparison.

## 1.21.0 - Unreleased

- Integrate method-5 coverage status/reasons, evidence
  counts, per-finding explanations and structure warnings. Separate supported
  check completion from limited coverage, parser failure and page-image failure;
  none is a publication approval or certification of images/full math structure.
  Keep long limitation lists in Coverage details while the brief warning remains
  visible.
- Keep content/structure warnings, uncertainty warnings and normalization Info
  distinct. Add a small per-side focus control for the selected actual finding
  region and an inline mark legend using the shared palette, retaining existing
  page images, Ctrl magnification and reference labels.
- Document the engine's per-occurrence evidence, strict paint-pair
  deduplication, independent-overprint ambiguity, exact evidence requirements,
  conservative vector coverage and one-to-one/context-aware alignment contract.
  Evidence inspection and primary alignment run inside the read-only worker,
  with a 90-second CPU soft limit and 95-second parent wall timeout, without DB
  access. Release acceptance is recorded separately from this design summary.
- Clarify that exact opaque glyph identity may align without Unicode recovery
  while only the coverage limitation may remain. Unmatched opaque glyphs and
  supported role changes still warn; unknown mathematical baseline attachment must not invent
  text edits. Unknown approximate vector candidates remain uncertainty warnings,
  never equality or Unicode guesses.
- Set app version 1.21.0. State archive v8, saved files, publication selection,
  automatic upload revision binding and review state are unchanged by the comparison workflow.

## 1.20.1 - 2026-11-08

- Distinguish text changes from text/formula regions with unreliable character
  mappings. Inspect used font codes and glyph ink; unknown text cannot act as
  an equality or page-pairing anchor. Keep these regions as visible warnings,
  not approvals or hidden differences.
- Normalize only non-drawing U+200B characters without detected mapping issues;
  retain their counts, locations and raw contexts as formatting information.
  Other controls and visible glyphs remain in comparison.
- Integrate separate counts, filters, amber verification outlines and extraction
  details into the existing comparison workspace and automatic-upload status.
- Comparison method v4 requires rebuilding previous temporary comparisons.
  Publication selection, saved PDFs, review flags and archive v8 are unchanged.

## 1.20.0 - 2026-11-08

- Read PDF text in native text-run order rather than interleaving columns
  by global coordinates; order detected ruled-table cells spatially.
  Isolate table detection from the web process's rendering settings.
  Account for exact moved spans one-to-one without listing them as changes.
- Highlight added/removed punctuation and refine single-token changes at
  glyph coordinates. Keep numbers, repeated occurrences, headers and footers.
- Integrate Added / Changed / Removed filters, remove same-text Info noise,
  and distinguish total from displayed findings. Text-layer encoding differences
  (especially equations) still require visual review, not automatic approval.
- Temporary comparison method v3 requires rebuilding older results.
  Publication selection, revision binding, review state, exports and State
  archive v8 are unchanged.

## 1.19.1 - 2026-11-04

- Show confirmed upload/report counts and execution phase while CrossCheck runs.
  Submitted rows say Awaiting results rather than incorrectly Awaiting upload
  until the result ZIP arrives. This is presentation-only; publication selection,
  result confirmation and State archive v8 are unchanged.

## 1.19.0 - 2026-11-04

- Retry missing CrossCheck report downloads within the original batch, with an
  explicit wait limit and confirmation. Reuse confirmed uploads, folder/token
  and manuscript identities; never re-upload PDFs or replay applied results.
- Retain each execution ZIP and paper result independently. Show cumulative
  paper counts and execution history, with scoped report/ZIP downloads.
  Progress updates preserve expanded history, scroll and retry-form inputs.
- Recheck active Final identity, publication PDF hash, retained archive/report
  hashes and current plagiarism evidence before applying newly available results.
  Uncertain requests are recovered by exact lineage, not repeated automatically.
- Add execution/result models and metadata-only capture for existing batches.
  State archive v8 includes this history; explicit v5/v6/v7 restore paths preserve
  existing files, approvals and timestamps. Publication selection is unchanged.

## 1.18.6 - 2026-10-18

- Test CrossCheck connections with entered service-login/IEEE credentials before
  saving settings. Persist optional credentials separately for the entered URL
  without leaving the page or enabling the integration. Keep credentials
  host-bound and out of audit/System State; environment overrides still win.
- Start new CrossCheck configurations with a blank service address. Explicit
  configuration is required to enable/send; existing saved addresses are kept.
- Reject stale connection responses after address/login edits and preserve newer
  inputs when a credential-save response arrives late. Publication file and
  result-apply rules are unchanged; State archive remains v7.
- Normalize decimal fields in Settings review evidence so background creation
  of identical defaults cannot invalidate a first-use form. Actual Settings
  changes still reject stale evidence.

## 1.18.5 - 2026-10-18

- Preserve archived automatic creation/update timestamps during System State
  restore, including Master/Final record dates and CrossCheck batch age.
  Restoring an expired upload preview no longer renews its two-hour limit;
  already-preserved Formatting revision times remain unchanged.
- Extend version-safety regression coverage for CrossCheck result binding,
  mixed stale/valid batches, atomic failures, corrupted evidence and v6/v7
  restore. Publication selection and archive schema remain unchanged (v7).

## 1.18.4 - 2026-10-18

### PDF Page Thumbnail Sidebar

- Add an integrated, independently scrolling thumbnail sidebar to the shared
  reader, with same-document page jumps, current-page tracking and keyboard focus.
- Draw near-visible thumbnails serially from the loaded PDF, keep a bounded
  browser cache, and cancel stale work on close/rotation/teardown. Thumbnail
  failures remain local and do not replace or block the main PDF.
- Start open on desktop and collapsed on narrow screens; mobile uses a
  reader-only overlay and closes after selection. Preserve existing native fit,
  numeric zoom, navigation, file/version binding and publication rules.
- No schema change; State archive remains v7.

## 1.18.3 - 2026-10-18

### CrossCheck Progress Countdown

- Show a live 15-second countdown beside batch progress, with explicit checking,
  hidden-tab pause and error-pause states. Resume with a fresh countdown when
  the tab becomes visible; keep one progress request in flight at a time.
- Preserve manual result apply and publication/version checks. State archive
  remains v7.

## 1.18.2 - 2026-10-18

### CrossCheck Settings Alignment

- Align Service base URL and Report wait minutes below a shared enable row.
- Separate batch result status, messages and Open report links into spaced lines.
- Explain when conference-folder confirmation is required, without changing
  confirmation rules or publication behavior. State archive remains v7.

## 1.18.1 - 2026-10-18

### Readable CrossCheck Folder Selection

- Show complete conference folder names in a full-width searchable selector,
  wrapping both dropdown options and the selected name without truncation.
- Preserve exact folder values, existing settings and publication selection.
  State archive remains v7.

## 1.18.0 - 2026-10-18

### Automatic CrossCheck Service

- Integrate an optional local CrossCheck service into Settings and the existing
  Plagiarism / CrossCheck page, with Automatic service / Manual workflow tabs.
- Preview version-bound publication PDFs before upload; use PaperID.pdf and a
  conference/date/daily-sequence postfix. Track jobs and recover uncertain
  submissions without automatic re-upload. Review per-paper results before apply.
- Validate result manifests, explicit upload/download outcomes, report bytes,
  current active Final/PDF hash and concurrent plagiarism edits. Retain partial
  results without clearing previous scores. Preserve existing review rules.
- Keep optional credentials write-only and outside DB/audit/System State.
  Use the service default IEEE account when no override is configured.
- Use State archive v7 for jobs/items/evidence and durable provenance; retain
  explicit v5/v6 readers. Protect evidence from ordinary cleanup and include
  jobs/evidence/local secrets in Clear Database.

## 1.17.6 - 2026-10-16

### Integrated PDF Keyboard Navigation

- Share page boundaries and entry positioning between wheel and keyboard in
  one reader navigation module. Left/Right turn pages, vertical/page keys pan
  first in Single page mode, and Home/End jump to the first/top and last/bottom.
- Require key release before another page turn; preserve inputs, toolbar,
  modifiers, search, IME and native continuous-mode vertical scrolling.
- Focus the PDF initially only when no control already has focus. Leave PDF
  selection, version binding, review state, publication exports and archive v6
  unchanged.

## 1.17.5 - 2026-10-16

### Single-Page Wheel Navigation

- Let vertical wheel gestures turn pages in the shared reader's default
  Fit page/Single page layout, with one turn per gesture and momentum protection.
- Keep native panning in zoomed pages; require a fresh gesture at the top/bottom
  before turning, and enter the previous page at its bottom.
- Leave continuous scrolling, horizontal/modified input, file selection,
  version binding, review state, publication exports and archive v6 unchanged.

## 1.17.4 - 2026-10-16

### PDF Page Layout Choice

- Open the shared reader in Fit page + Single page mode. Add an integrated
  Single page / Continuous scrolling control using PDF.js's native modes.
- Keep page navigation, cross-page search, rotation, zoomed-page panning and
  current-page/zoom preservation when switching layout. Reopening uses defaults.
- Reapply native fit presets after single-page navigation so mixed page sizes
  fit the new viewport; preserve explicit numeric zoom.
- Avoid redundant same-page navigation on page-input blur, which can otherwise
  reattach a single-page view during a subsequent PDF-link click.
- Use direct alpha-mask declarations for toolbar SVG icons to keep them visible
  in WebKit cached reloads as well as Chromium.
- Leave file sources, version binding, review state, exports and archive v6
  unchanged.

## 1.17.3 - 2026-10-16

### Full-Window PDF Reader

- Give every shared PDF reader source a dedicated viewport-filling layout
  with one compact wrapping toolbar and no work-page Navbar, footer or outer
  margins. Retain site typography/colors, file identity, direct opening,
  downloads and all PDF controls.
- Isolate the site's app-page flex/minimum-height styling from PDF.js's
  internal pages without overriding its calculated PDF dimensions.
- Keep ordinary work-page layout, source URLs, version/snapshot binding,
  publication selection, readiness and archive v6 unchanged.

## 1.17.2 - 2026-10-16

### Narrow-Screen Download Actions

- Constrain shared download forms and wrap their long button labels so Export
  Reports and blocked-package actions do not widen narrow screens.
- Keep download requests, publication selection, readiness and archive v6
  unchanged.

## 1.17.1 - 2026-10-16

### Local History Pagination

- Hide redundant top/bottom pagination bars for child lists that fit the
  smallest supported page size, including revision history in Formatting,
  Final Submission edit and the standalone history page.
- Label larger histories' pagination and page-size controls explicitly as
  revision history rather than the parent worklist. Retain both bars even
  when All or a larger page size is selected.
- Keep main worklist pagination, query/filter/slicing behavior, publication
  selection and review state unchanged. State archive stays v6.

## 1.17.0 - 2026-10-16

### Shared Read-Only PDF Reader

- Route PDF browsing links through one CFM-styled PDF.js reader, including
  publication/submission files, Original/Corrected uploads, revision history,
  reports, uploaded previews, debug PDFs and comparison snapshots.
- Add continuous lazy page rendering, text selection/search, page navigation,
  fit/zoom, rotation and password entry. Keep direct opening and PDF downloads
  on the exact original endpoint. No PDF editing, form filling or scripting is
  enabled.
- Vendor the pinned PDF.js 6.4.299 runtime, worker, fonts and decoders locally;
  no CDN, external PDF processing service or runtime npm install is required.
- Validate local PDF source URLs without fetching/proxying files or performing
  publication selection. Preserve missing-file, history-integrity and stale
  comparison failures rather than choosing a substitute.
- Keep publication scope, file priority, readiness, reset rules, ZIPs, review
  images and comparison algorithms unchanged. No migration or archive version
  change is needed; State archive remains v6.

## 1.16.0 - 2026-10-16

### Automatic Original / Corrected PDF Comparison

- After a successful Formatting corrected-PDF save (including confirmed title
  guards), queue a signed request bound to that exact revision, file identity,
  upload time, and PDF SHA256. Keep the existing save/reset behavior unchanged.
- Show comparison progress, Warning/Info counts, an Open comparison link, and
  Retry in the saved card, including when collapsed. Identify the latest saved
  paper separately when the current list filter no longer contains it.
- Build through a separate CSRF-protected POST after returning to the review
  page; GET only reads status. Do not auto-navigate, poll, create background
  workers, or run comparisons in the upload transaction.
- Reuse valid results under a portable file lock, reject newer PDF uploads and
  changed scope/files, and never substitute another baseline or submission.
- Preserve uploaded files/revisions and review state when comparison fails.
  Source-only/status/notes changes create no new comparison request. Manual
  Original/revision comparisons remain available.
- Keep request/result caches temporary, out of backups, and covered by Clear
  Database. No database migration or State archive v6 change is required.

## 1.15.3 - 2026-10-16

### Finding Group Hierarchy

- Separate static Warning/Info category headings from clickable findings using
  unfilled headings, bottom dividers, spacing, and indented finding lists.
- Preserve category colors, item selection/focus states, comparison logic,
  publication behavior, and State archive v6 unchanged.

## 1.15.2 - 2026-10-16

### Comparison Finding Presentation

- Present content/pattern findings as Warning and same-text/sequence-position
  records as Info, using the site's existing amber/blue color tokens and badges.
- Keep selection distinct through an outline and emphasis rather than changing
  classification color. Preserve PDF difference overlays, comparison results,
  navigation, publication rules, review state, and State archive v6 unchanged.

## 1.15.1 - 2026-10-16

### Reflow-Aware Read-Only PDF Comparison

- Retain all header/footer/body text while pairing unique exact unmatched
  passages one-to-one into visible same-text/sequence-position records.
  Repeated, partial-word, changed, or uncertain text remains a content finding.
- Track all actual passage pages separately from optional reference pages;
  provide passage-page navigation without inventing counterpart text locations.
- Separate content/pattern findings from same-text records within the existing
  workspace. Neither group approves publication or proves unchanged layout.
- Check token accounting, retain unpaired text when pairing reaches its bound,
  scan patterns even with a textless baseline, and require explicit rebuild for
  comparisons produced by the older method.
- Keep publication selection, files, flags, readiness, exports, models, and State
  archive v6 unchanged.

## 1.15.0 - 2026-10-16

### Read-Only Formatting PDF Comparison

- Add Compare PDF in Formatting Review List/Single/Focus, opening a separate
  workspace without submitting or discarding unsaved Formatting inputs.
- Compare the row's Original or an integrity-verified Formatting revision with
  its current publication-helper PDF; identify added/changed/unmatched text,
  newly occurring Word reference errors, possible unresolved references, and
  replacement characters without claiming content completeness.
- Render hash-verified full-page snapshots with selected-region highlights,
  text-linked or independent page navigation, and the shared loading/magnifier
  components. Figures, tables, equations, and layout remain manual checks.
- Audit comparison builds and reject stale scope/files, corrupt snapshots,
  expired tokens, cross-submission revisions, and excessive document sizes.
- Keep comparisons temporary and out of System State backups; preserve them
  during ordinary cleanup and remove them through expiry or Clear Database.
- Keep publication files, readiness, flags, exports, database schema, and State
  archive v6 unchanged. No QPDF/Poppler or other new runtime dependency is added.

## 1.14.0 - 2026-10-16

### Formatting Revision History

- Preserve a complete independent PDF/source snapshot after each successful
  corrected-file upload, with capture time, notes/status, original upload
  times, file origins, sizes, and SHA256 values.
- Add a lazy, paginated history panel shared by Formatting Review modes and
  Final Submission edit, with verified PDF viewing and source downloads.
- Add idempotent baseline capture for existing corrected files and preserve
  history through original-file replacement, Discard, and storage cleanup.
- Keep publication selection, readiness, and review-reset rules unchanged;
  history has no restore, switch, or delete action.
- Advance System State to v6 for revision records/files and retain an explicit,
  integrity-validated v5 restore path with empty revision history.

## 1.13.2 - 2026-08-24

### Exact Title Verification Evidence

- Kept the existing normalized title locator for finding title text, then
  changed the shared verification renderer to draw evidence from exact raw PDF
  character coordinates.
- Excluded PDF-only trailing or internal punctuation from yellow title
  evidence while retaining symbols that are actually present in the extracted
  title.
- Kept Built-in, GROBID, Manual Override, extracted metadata, title matching,
  review state, publication files, and export behavior unchanged. State archive
  version remains 5 because persisted data and restore compatibility did not
  change.

## 1.13.1 - 2026-08-15

### Verification Header Font Fallback

- Added a bundled Noto Sans Math fallback to the shared Title/Author
  verification renderer so mathematical symbols such as `U+22C6` can appear
  in evidence headers without aborting image generation.
- Unified header font selection, width measurement, wrapping, and drawing
  through one font-run implementation, preventing fallback glyphs from
  overlapping adjacent primary-font text.
- Retained fail-closed behavior when neither bundled font supports a header
  character, and kept Built-in, GROBID, Manual Override, extraction metadata,
  review resets, publication files, and export rules unchanged.
- Kept State archive version 5 because the bundled code asset does not alter
  persisted state or the backup/restore contract.

## 1.13.0 - 2026-08-04

### Safe Record Editing And Readable Change History

- Changed existing Paper Master and Final Submission edits to a two-step
  Review Changes then Confirm and Save workflow; preview does not mutate the
  database or managed files.
- Added server-validated old/new values, file SHA-256 comparison, and an
  explicit workflow-impact summary before apply.
- Rejected confirmed edits when the database record, current file, or staged
  upload changed after preview, and excluded temporary edit previews from
  System State backup.
- Treated byte-identical replacement uploads as no effective change so review
  state is not reset merely because a file was selected again.
- Aligned Final Authors edits with the documented dependency rule by returning
  Title/Author Review to Pending and clearing duplicate-author and per-paper
  author-number approvals.
- Added integrated changed-field markers to both edit forms and human-readable
  User Changes, System Changes, Workflow Effects, and File Effects to expanded
  Audit Log events.
- Kept publication scope, active-version selection, publication file priority,
  readiness, and package export rules unchanged. State archive version remains
  5 because no persisted archive contract changed.

## 1.12.12 - 2026-07-31

### Dashboard Action Count Integrity

- Derived every Next action total and breakdown from the same publication
  readiness findings used by strict final-package export.
- Added the previously ungrouped publication-decision, corrected-file, source
  integrity, and filename-collision blockers to their owning workflows.
- Scoped Dashboard Error Report links to the exact blocking categories shown on
  each action so valid Allowed exceptions do not appear in the target result.
- Clarified allowed P/S tracking as affected papers plus active approval count,
  while keeping stale exceptions blocking and valid exceptions non-blocking.
- Added an unknown-category fail-safe action and aligned the desktop count
  column without changing readiness, exception, publication-file, or export
  rules.

## 1.12.11 - 2026-07-29

### Export Integrity And Report Safety

- Made every Excel report a read-only, atomically promoted, fully audited
  snapshot; failed validation, concurrent state changes, or audit persistence
  now remove partial output.
- Built all selected Editorial Publication Workbook sheets from one stable
  publication context without rebuilding the PaperAuthor cache.
- Preserved formula-like editorial text as non-executable XLSX values and
  neutralized formula-like strings in generated package CSV files.
- Extended generated-report cleanup to publication manifest/warning CSV files
  while retaining unrelated CSV files.
- Replaced the Draft package's abbreviated structural-error message with the
  same detailed blocker panel used by Final package failures.
- Kept publication scope, active-version selection, readiness rules, and
  publication PDF/source selection unchanged.

## 1.12.10 - 2026-07-29

### Built-In Author List Formatting

- Replaced the built-in extractor's comma-count author heuristic with one
  parsed author list shared by stored metadata, author count, and verification
  rendering.
- Standardized extracted author display as `A`, `A and B`, or
  `A, B, and C` without changing the extracted names themselves.
- Fixed multi-line author blocks that use `and` between every author so they
  no longer remain as an ambiguous all-`and` display string.
- Kept title extraction, review resets, GROBID, Manual Override, and
  publication readiness unchanged.

## 1.12.9 - 2026-07-28

### Expansion Reveal Event Fix

- Fixed shared row and review-card expansion positioning after Bootstrap had
  already updated the collapse trigger state.
- Preserved post-save worklist restoration while restoring conditional
  centering or owner-row alignment for user-opened panels.

## 1.12.8 - 2026-07-28

### Unified Worklist Vocabulary

- Standardized compact version, origin, Paper ID, review, processing,
  publication-decision, exception, and file labels across all editorial
  worklists.
- Removed the one-off Final Submissions status guide and kept each state in its
  owning table column, with origin shown as supporting metadata instead of a
  competing badge.
- Separated page-limit results from their exception decision in Organized List
  so the page condition and waiver state are both visible without duplicate
  labels.
- Kept workflow values, filters, readiness checks, and publication export rules
  unchanged.
- Extended the shared expansion reveal behavior to Formatting Review cards, so
  opening `Review paper` keeps the card header and review content in view
  without interfering with post-save worklist position restoration.

## 1.12.7 - 2026-07-27

### Stable Organized List Details Width

- Prevented an expanded Organized List Details panel from increasing the
  calculated table width at wider browser sizes.
- Kept the panel capped to the visible viewport when the checklist itself
  requires horizontal scrolling.
- Added one shared reveal behavior for table expansion rows so Details, notes,
  exception forms, JSON, and version actions keep their owning row in context:
  clipped groups are centered when they fit, while taller groups begin with
  the owning row below the app header.

## 1.12.6 - 2026-07-27

### Organized List Root-Cause Presentation

- Replaced repeated downstream warning badges for missing Finals, unresolved
  Paper IDs, publication decisions, and multiple active Finals with one
  structural blocker row and an exact resolution action.
- Assigned missing PDFs to the PDF column while Pages waits for a PDF, and
  assigned missing extracted metadata to Extraction while Title and Authors
  wait for extraction.
- Kept publication readiness and final-package blockers unchanged while making
  Organized List filters and summary counts follow the same root-cause
  presentation.

## 1.12.5 - 2026-07-27

### Process PDFs Scope Consistency

- Aligned Organized List and Dashboard processing indicators with the Process
  PDFs publication-candidate scope.
- Kept unmatched Final PDF state intact while deferring processing prompts
  until the Paper ID resolves to a Publishing Paper Master record.

## 1.12.4 - 2026-07-27

### Consistent Status Badges

- Removed the one-off hover explanation from Organized List status badges and
  retained the full Paper ID decision guidance in persistent Details content.
- Documented that status badges must not hide workflow or publication-safety
  explanations in hover-only text.

## 1.12.3 - 2026-07-27

### Multiline Title Editing

- Standardized editable Master, Final Submission, and Editor Upload titles as
  three-row textareas matching their author fields.
- Rebalanced Editor Upload metadata so Paper ID has a clear full-width context
  row and Final Title/Final Authors form one aligned pair.

## 1.12.2 - 2026-07-27

### Organized List Status Clarity

- Shortened the orphan Final Submission badge to `Needs decision`.
- Removed the duplicate `Not in Paper Master` line from the checklist and
  retained the complete correction/Not Publishing guidance in the badge
  tooltip and row Details.

## 1.12.1 - 2026-07-27

### Orphan Final Decision Workflow

- Moved Final Submissions outside Paper Master ahead of Missing Final and
  tracked Not Publishing records so unresolved publication-scope decisions
  receive attention first.
- Replaced the ambiguous `Edit Paper ID` route with an inline Paper Master
  search that uses the same signed-evidence verification service as Paper ID
  Review.
- Added full-width, expandable Resolve Paper ID and Mark Not Publishing
  workspaces without crowding the decision table.
- Removed the implicit `Unpaid` default. A valid Not Publishing reason is now
  required and enforced by the service before orphan Final state can change.
- Reused one Paper Master picker component across Verify Paper IDs and Not
  Publishing workflows.

## 1.12.0 - 2026-07-27

### Final Submission Re-import Safety

- Removed the unused combined Mapping Table workbook path and its legacy
  mapping metadata fields. Paper Master and Final Submission imports now remain
  separate, preview-before-apply workflows.
- Preserved an existing Official Paper ID when a re-import keeps the same
  Author-entered ID. A changed Final Title resets Paper ID review without
  silently remapping the submission.
- Re-resolved the Official Paper ID only when the Author-entered ID changes or
  the existing Official Paper ID is blank.
- Returned Title/Author Review to Pending when Final Authors change while
  preserving the existing extraction for comparison.
- Corrected Final Submission preview ordering so metadata review resets, file
  resets, new rows, metadata-only changes, and unchanged rows appear in their
  documented attention order.
- Advanced the System State archive to version 5 because removed model fields
  make older archive state incompatible with this schema.

## 1.11.1 - 2026-07-27

### Dashboard Wording

- Renamed `Current Not Publishing` to `Not Publishing Papers` so the metric
  clearly describes Paper Master publication decisions rather than active Final
  Submission versions.

## 1.11.0 - 2026-07-27

### Paper Master Publication Decisions

- Made Paper Master the authoritative owner of each paper's Publishing,
  Not Publishing, or Decision Required state.
- Added a safe Not Publishing workflow for unpaid or withdrawn Paper Master
  records that do not have a Final Submission yet.
- Required explicit publication-scope impact confirmation and a reason before
  excluding a paper; Undo returns the paper to publication scope without
  discarding review evidence.
- Kept Final Submission exclusion fields as compatibility mirrors and made
  later Final imports and Editor Uploads inherit their Paper Master's decision.
- Excluded Not Publishing papers consistently from Process PDFs, review queues,
  CrossCheck, duplicates, author counts, readiness, and final or draft packages.
- Added a blocking Decision Required state for ambiguous migrated records so
  publication code never guesses.
- Guarded Master creation and import so an orphan Final with an existing Not
  Publishing decision enters Decision Required instead of silently returning
  to publication scope.
- Prevented Paper ID verification/remapping from bypassing an existing
  Not Publishing decision.
- Added a non-bypassable publication-decision integrity check for Final,
  Draft, and CrossCheck exports, and preserved legacy decision evidence while
  a Master record remains Decision Required.
- Classified discarded legacy versions during migration so Undo Discard cannot
  revive an unreviewed publication decision.
- Advanced the System State archive to version 4 because Paper Master
  publication decisions are now required restore state.

## 1.10.43 - 2026-07-27

### Image Preview Sizing

- Limited deferred preview placeholder heights to loading and error states.
- Let loaded Formatting Review and Process PDFs preview frames follow each
  image's natural aspect ratio without stretching or cropping publication
  evidence.

## 1.10.42 - 2026-07-27

### Image Preview Loading

- Added one shared loading, failure, and retry presentation for deferred image
  previews.
- Kept Formatting Review previews hidden behind an integrated spinner until
  their review card is opened and the image is ready.
- Reset Process PDFs modal previews between pages so a loading state replaces
  empty, stale, or broken-looking image content.

## 1.10.41 - 2026-07-26

### Exception Filter Counts

- Made Exceptions summary metrics and status-tab counts recalculate within the
  selected exception type and search scope.
- Kept status tabs as a distribution of the scoped results, so switching
  between Not allowed, Allowed, Stale, and All does not lose the current type
  or search context.

## 1.10.40 - 2026-07-26

### Error Report Exception Actions

- Added integrated exception panels to exception-capable Error Report findings
  so editors can inspect evidence and approve, re-approve, or remove an
  exception without searching for the same item on another page.
- Kept exception validity, stale-evidence protection, audit events, readiness,
  and final publication blocking on the existing shared exception services.
- Made each action rebuild the complete filtered Error Report worklist so an
  approved blocker moves to Info immediately without stale severity/category
  results.

## 1.10.39 - 2026-07-26

### Dashboard Navigation

- Made tracked-information links open the exact worklist represented by their
  count: verified Paper Master title differences, reviewed extracted-title
  differences, and allowed Plagiarism/Single exceptions.
- Added dedicated Organized List and Exceptions Center filters for those
  focused destinations without changing readiness or publication behavior.

## 1.10.38 - 2026-07-26

### Documentation Redesign

- Redesigned the repository README as a concise product overview with a visual
  workflow, quick start, publication-safety summary, and real UI previews built
  from a disposable example conference.
- Added a documentation home that routes editors, Docker operators,
  developers, maintainers, and release reviewers to the correct guide.
- Centralized Docker creation, update, backup, migration, recovery, and
  rollback procedures in one Docker Guide.
- Removed repeated Docker and UI explanations from the README, Operator Guide,
  Developer Guide, Architecture Notes, and Troubleshooting while preserving
  their audience-specific instructions.

## 1.10.37 - 2026-07-26

### Docker Service Recovery

- Made the Nginx fallback return automatically to the Dashboard after two
  successful readiness checks.
- Kept recovery safe for interrupted POST requests by replacing the fallback
  document with `/` instead of reloading or resubmitting the original URL.
- Retained the immediate Return to workspace action and resumed polling if the
  second readiness check fails.

## 1.10.36 - 2026-07-26

### Docker Instance Updates

- Added one unified updater that discovers `.env` and `.env.*`, applies
  environment changes, rebuilds the current code, and verifies every matching
  Docker conference instance.
- Added stable `COMPOSE_PROJECT_NAME` ownership, plan-first validation for host
  ports and data directories, masked secret reporting, and explicit
  `--create-missing` protection for new instances.
- Kept the legacy rebuild command as a recovery path that preserves settings
  recovered from running containers when no maintained env file exists.

## 1.10.35 - 2026-07-25

### Docker Service Recovery

- Added a theme-matched Nginx fallback gateway for planned backup, storage
  migration, application update, restart, unexpected outage, and
  operator-attention states.
- Kept Nginx available while backup and update operations restart Gunicorn,
  added project-scoped operation status with heartbeat and expiry handling, and
  prevented interrupted requests from being submitted again automatically.
- Changed multi-instance rebuilds to build first, replace only `web`, validate
  readiness, reload or upgrade the proxy, and retain the existing Nginx,
  static, and same-origin CSRF smoke checks.
- Enabled finite Nginx response buffering for large Django-generated downloads
  so a slow browser does not hold a Gunicorn thread for the entire transfer;
  no response cache or alternate publication download path was introduced.
- Added a database-backed readiness endpoint and serialized rebuild, migration,
  and backup operations with the existing Docker operation lock.

## 1.10.34 - 2026-07-25

### Docker Deployment

- Strengthened the multi-instance rebuild tool to recover each existing
  deployment's effective environment, public port, project name, and data mount
  before updating it.
- Added Compose validation, forced web/proxy recreation, bind-versus-volume
  preservation, loaded Nginx configuration verification, static asset checks,
  and a non-mutating same-origin CSRF POST smoke test.
- Kept generated environment files temporary and continued masking secrets in
  console output.

## 1.10.33 - 2026-07-25

### Docker Deployment

- Preserved the browser-visible host port when Nginx proxies requests to
  Django, so same-origin CSRF validation continues to work on non-default
  conference ports such as `:9000`.
- Kept CSRF token and origin validation fully enabled; no publication,
  workflow, database, or System State behavior changed.

## 1.10.32 - 2026-07-25

### Docker Deployment

- Added an Nginx proxy that serves all Docker static and media requests while
  Gunicorn remains responsible for Django pages and controlled downloads.
- Made `SMS_DEBUG=0` the normal Docker default without hiding uploaded PDFs,
  verification images, thumbnails, or formatting previews.
- Added a rebuildable static volume and kept the conference data volume
  read-only from Nginx.
- Updated named-volume backup, bind-to-volume migration, bind rollback, and
  multi-instance rebuild tools to understand both `web` and `proxy` services
  while remaining compatible with older single-service instances.

## 1.10.31 - 2026-07-25

### Audit Log

- Added a syntax-highlighted, safely rendered JSON viewer with Formatted and
  Plain modes plus Copy JSON.
- Kept JSON parsing lazy so large Audit Log result sets remain responsive, and
  retained the original text when a historical line cannot be parsed.

## 1.10.30 - 2026-07-25

### Audit Log

- Centralized production audit action names under the
  `<domain>_<operation>[_<phase>]` convention without changing workflow or
  publication logic.
- Kept historical JSONL events append-only while mapping legacy action names
  to canonical categories and labels for display and filtering.
- Reworked the Audit Log into a structured review page with category, action,
  status, search, and row-limit controls plus clearer record and JSON details.

## 1.10.29 - 2026-07-25

### Formatting Review

- Reworked the Corrected PDF Title Safety Check pending-save details into a
  responsive summary for PDF, source, workflow status, and formatting notes.
- Kept title comparison, upload confirmation, and publication-file behavior
  unchanged.

## 1.10.28 - 2026-07-25

### Publication Package

- Added an `Exceptions` column to Final and Draft Publication Package manifests.
- Reused the Editorial Publication Workbook's per-paper exception summary,
  including status, current and approved values, limits, and recorded reasons.
- Kept publication scope, readiness checks, file selection, and packaged
  PDF/source bytes unchanged.

## 1.10.27 - 2026-07-25

### Paper Selection

- Unified Editor Upload and Paper ID Review search results as Master Paper ID,
  Master Title, and Master Authors while keeping the selected control compact.
- Distinguished Title and Authors with a consistent typographic hierarchy,
  spacing, and Authors icon without adding longer field labels.
- Increased the open Paper picker height so editors can scan more matching
  records without making the closed form taller.
- Removed the redundant Process PDFs `Find paper` picker. Its existing Paper
  ID / Final ID / title worklist search filters before pagination and remains
  the single search control on that page.
- Kept exact focused Process PDF links and all publication, active-version,
  review-state, and export behavior unchanged.

## 1.10.26 - 2026-07-23

### Paper Selection

- Added one shared searchable Paper picker to Editor Upload, Paper ID Review,
  and Process PDFs.
- Paper Master searches rank exact Paper ID matches first, search Master Title
  and Master Authors, return at most 20 results, and never load the full list
  before the editor types.
- Each new query discards unselected results from the previous query so cached
  items cannot appear ahead of a newly returned exact Paper ID match.
- Paper ID Review and Editor Upload show Master Title in results; Process PDFs
  keeps its compact Paper ID / Final ID display and opens the exact focused
  publication candidate across pagination.
- Kept all submitted selections under existing server-side Master Paper and
  workflow validation. No active-version, review-state, publication-file, or
  export rule changed.

## 1.10.25 - 2026-07-23

### Pagination

- Removed the visible smooth-scroll trip after changing pages or page size.
- Top pagination now keeps its visible position when the worklist header is
  already on screen; bottom pagination returns immediately to the refreshed
  worklist start, including when the next page is shorter.
- Centralized pagination positioning in the shared worklist component so every
  paginated review and report page follows the same behavior.

## 1.10.24 - 2026-07-23

### Error Report

- Added workflow-grouped category filters with per-category counts and
  multi-select support while retaining the existing Critical, Medium, and Info
  severity model.
- Applied area, severity, and category filters on the server before pagination;
  repeated category parameters remain shareable and are preserved by paging.
- Added scoped, severity-colored Error Report pills in a balanced workflow
  matrix; selecting or clearing a category now updates the HTMX worklist
  immediately without changing publication readiness categories, blocker
  rules, or exports.

## 1.10.23 - 2026-07-23

### Navigation

- Added the configured Conference Name to every browser page title so tabs and
  saved bookmarks clearly identify the conference instance.

## 1.10.22 - 2026-07-23

### Organized List

- Shortened the row-level `Manage exceptions` control to `Exceptions` so the
  action column uses less horizontal space without changing exception behavior.

## 1.10.21 - 2026-07-23

### Final Submission Import

- Realigned the Metadata and PDF/Source upload zones into equal-width desktop
  columns with a stacked narrow-screen layout.
- Moved Preview Changes into its own action row so unequal help text no longer
  shifts the upload controls or compresses the action button.

## 1.10.20 - 2026-07-23

### Navigation

- Simplified page headers so ordinary cross-page navigation remains in the
  Navbar while local commands, view switches, summaries, and focused Back
  actions stay next to each page title.
- Moved readiness, PDF-issue, plagiarism-review, and author-exception links
  beside the condition or result that makes each action relevant.
- Linked Dashboard ready state directly to the Final Publication Package
  section and clarified Checklist versus Compact publication-candidate
  switching.

## 1.10.19 - 2026-07-23

### Exports

- Reorganized the Excel-only portion of Export Reports around one Editorial
  Publication Workbook and a collapsed advanced/debug area.
- Added readable Publication Detail and Exception Detail sheets without
  changing Final or Draft Publication Package behavior.
- Publication Detail is the fixed workbook core; supporting sheets are now
  explicitly selected at download time. Raw active and old-version data remain
  separate debug exports.
- Standardized generated XLSX files with frozen/filterable headers, bounded
  column widths, wrapped long text, readable date/percentage formats, and a
  consistent restrained visual style.
- Fixed POST-based report and package downloads so their buttons are available
  again after each completed download without requiring a page refresh.

## 1.10.18 - 2026-07-23

### Documentation

- Reorganized documentation by audience and responsibility.
- Reduced README to installation, navigation, workflow, and safety essentials.
- Added canonical Publication Rules and shared UI Conventions.
- Added explicit document ownership so publication and UI rules have one
  maintained source of truth.
- Added a dependency-free documentation link and heading-anchor validator to
  the regression gate.
- Added this changelog for future release summaries.

## Earlier Releases

Versions through 1.10.17 predate the maintained changelog. Use Git history and
the corresponding application version in audit and System State metadata when
tracing those releases.
