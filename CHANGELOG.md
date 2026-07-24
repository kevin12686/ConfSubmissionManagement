# Changelog

This file records user-visible releases. Detailed implementation history remains
available in Git.

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
