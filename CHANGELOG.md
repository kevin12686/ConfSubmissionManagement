# Changelog

This file records user-visible releases. Detailed implementation history remains
available in Git.

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
