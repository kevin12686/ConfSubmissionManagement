# Changelog

This file records user-visible releases. Detailed implementation history remains
available in Git.

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
