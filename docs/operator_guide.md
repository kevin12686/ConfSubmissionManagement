# Operator Guide

This guide is for editors running the local system to prepare final submissions for publication.

## Start And Restore

1. Start the app with `start.command`, `start_windows.bat`, or `./scripts/start_local.sh`.
2. Open <http://127.0.0.1:8000/>.
3. If continuing an existing conference on a new machine, go to `/integrations/system-state/` and preview the System State ZIP before applying it.
4. Set the conference name and limits in `/settings/`.

The Conference Final Manager icon appears in the browser tab and beside the application name in the top navigation bar. The navigation bar uses the high-resolution app icon so it remains sharp on high-density displays.

The two-level navigation separates context from work. The top identity row shows
the application and current conference. The workflow row keeps `Dashboard` and
`Organized List` as direct links; `Submissions` contains Paper Master, Final
Submission, Editor Upload, and Not Publishing records; `Reviews` contains ID,
PDF, title/author, formatting, and exception work; `Reports & Output` contains
readiness reports, versions, exports, and CrossCheck/plagiarism; `Administration`
contains Audit Log, System State backup/restore, and Settings. Each dropdown item
includes a short purpose statement, and the current page is marked by a blue
underline and soft blue background.

## Page Map

| Page | URL | Main use |
| --- | --- | --- |
| Dashboard | `/` | Final-package readiness and current editorial actions |
| Paper Master List | `/papers/` | Official publication scope, titles, authors, acceptance status, editorial notes |
| Final Submissions | `/submissions/` | Imported Start2 submissions, uploaded files, editor uploads, discarded versions |
| Editor Upload | `/submissions/editor-upload/` | Add email-provided replacement versions |
| Organized List | `/submissions/organized/` | Publication checklist by Paper Master record |
| Process PDFs | `/processing/pdfs/` | Page count, hash, thumbnails, and publication PDF debug copies |
| Verify Paper IDs | `/reviews/paper-ids/` | Correct author-entered IDs and verify mapping |
| Title/Author Review | `/reviews/title-authors/` | Extract and review title, authors, image evidence, and title comparison together |
| Formatting Review | `/reviews/formatting/` | Review first-page title/author formatting and upload corrected files |
| Not Publishing List | `/reviews/not-publishing/` | Track paid/published scope exclusions |
| Exceptions | `/reviews/exceptions/` | Approve rare page/author/plagiarism exceptions |
| Error Report | `/reports/errors/` | Critical, Medium, and Info readiness issues |
| Author Count | `/reports/author-count/` | Per-author publication paper counts |
| Audit Log | `/reports/audit-log/` | Searchable record of important actions and file/state changes |
| Export Reports | `/reports/` | Excel exports and publication package ZIPs |
| Compact candidates | `/submissions/organized/?view=compact` | Compact read-only roster inside the same Organized List publication scope |
| Plagiarism / CrossCheck | `/integrations/crosscheck/` | Prepare publication PDFs and import scores/reports |
| System Backup / Restore | `/integrations/system-state/` | Download or preview/apply a complete system snapshot |

Organized List keeps the main table compact. Open a paper's `Details` to review
its publication metadata, the complete extracted author list and extraction
status, current publication PDF/source files, optional debug copy, and editorial
notes in one publication-record view. Routine pages show file actions and source
labels rather than machine-specific absolute paths.

Its summary is split into `Publication blockers` and `Tracked information`. Blocker cards link to focused filters and only appear when the current view contains that issue. Tracked information remains visible without competing with work that can stop final export.

Use the `Checklist / Compact candidates` control to switch views. Both use the same active Paper Master publication rows and publication-facing Corrected-to-Original file helpers. The old `/reports/active-versions/` link redirects to Compact candidates.

## Dashboard And Readiness

Dashboard uses the same blocking rows as Final Publication Package export. Its top panel therefore answers whether a final package can be created now; it is not a separate approximate status calculation.

- `blocking papers` counts unique affected Paper IDs.
- `blocking checks` counts individual readiness findings, so one paper can contribute more than one check.
- `Next actions` lists only workflows that currently have blockers.
- `No current blockers` lists workflows whose checks are clear instead of repeating zero-value cards.
- `Tracked information` can include non-blocking editorial reminders, such as a verified Paper ID whose titles still differ.

Uploading a corrected PDF intentionally sends the paper back through PDF processing and Title/Author review. Dashboard should show those actions again until the current Corrected/Original publication PDF has matching page/hash data and reviewed extracted metadata.

`Review OK` completes the Title/Author check, including acceptance of the extracted-title comparison. A reviewed paper whose Final and extracted title wording still differs is tracked for reference, but it is not returned to Next actions and does not require a second Confirm Match action.

Final Submission Edit is intentionally limited to submission metadata, original files, and plagiarism score/report entry. Processing state, Title/Author Review, duplicate-author review, and Not Publishing decisions are shown there for context but must be changed from their dedicated pages.

`Import / Re-upload` is collapsed on the Final Submissions page until selected so submission tabs and the version list remain the primary view. Expanding it exposes drag/drop file zones, selected-file counts, PDF/source summary, per-file removal, and the existing preview-before-apply workflow. Browser summaries are convenience only; server extension/hash checks decide actual file types. Final Submission Edit follows one sequence: Submission identity, Metadata, Current row files, Plagiarism data/report, read-only Workflow status summary, and Save. Destructive version actions are outside the normal edit form in the collapsed bottom `Version actions` danger zone and still require a reason. Not Publishing remains a separate workflow.

When Edit is opened from Organized List, Title/Author Review, Formatting Review, Not Publishing, Verify Paper IDs, or Exceptions, Save returns to the originating worklist with its view, filter, sort, search, tab, or single-paper selection. External return URLs are rejected. Worklist filters/search can update only that list area without a full refresh, but the same links/forms work as ordinary Django requests. No client-side code decides review state, exception validity, active versions, or publication files.

The workflow links inside Final Submission Edit are exact links, not prefilled
searches. Their focused banner names the Paper ID, Final ID, origin, and current
status. `Back to full worklist` returns to normal browsing. A focused page may
say that the selected version is outside its current workflow scope; this is
intentional and prevents an inactive or excluded version from being replaced on
screen by a similarly named active record. Search boxes remain broad matching
tools and should not be used as proof that a particular Final ID was selected.

Author Count supports author/Paper ID search, attention/over-limit/duplicate/allowed filters, and paper-count/name sorting. Exceptions supports search plus status and exception-type filters. Title/Author keeps Workflow and Tracked views separate. Verify Paper IDs preserves filter/search URLs. State-changing buttons still perform full audited server requests.

Status colors are consistent across pages: red means a blocker or dangerous action, amber needs manual attention, blue is tracked information, green means the named review is complete, and gray is inactive/history. Primary text uses deep ink on muted work surfaces; labels and supporting text use a darker blue-gray instead of low-contrast gray. Compact pill-shaped labels report status, file origin, counts, or categories and are not controls. Action buttons are taller rectangular controls with stronger borders and visible hover states. Every label also includes text, so color is never the only status signal.

Tables use one uniform row surface with clear horizontal separators. Zebra striping is intentionally disabled across the application; hovering a row provides the only temporary row highlight. Details, exception, note, and discard panels therefore cannot disrupt row coloring. Organized List keeps routine counts and file-origin information neutral, reserves muted green for completed editorial reviews, and marks blocking rows with a red left edge instead of replacing the entire row background.

Typography is centralized for long editorial sessions. Normal page and table text uses 15px type with increased line height, supporting text has a fixed 13px minimum, and status pills use 12px type. Nested `small` elements do not shrink further. Muted text is reserved for supporting metadata and remains dark enough to read against the work surfaces.

Large worklists default to 100 rows and provide `50 / 100 / 200 / All`.
Filters and sorting apply to the complete result before pagination. Use `All`
when every matching record must be compared together; routine numbered pages
respond faster because file checks, previews, suggestions, and text diffs are
prepared only for visible rows. Dashboard readiness and global workflow alerts
load just after the page shell, but remain server-calculated from the same
rules used by publication export.

Technical values such as paths, action names, and expanded Audit Log JSON use dark monospace text on a muted light surface. If any expanded detail shows light text on a light background, treat it as a display defect rather than an indication that the log data is missing.

## Import Workflow

1. Download templates from the app.
2. Import the Paper Master List first.
   The page header shows the total publication-scope paper count; while searching, it
   shows the number of matching rows alongside the unchanged total.
3. Review the Paper Master Import Preview. Rows needing attention are sorted above unchanged rows.
4. For existing Paper Master notes, choose whether to preserve existing system notes or apply imported notes. The default is preserve.
5. Import Final Submission metadata and upload all PDF/source files together.
6. Review the Final Submission Import Preview. Mapping, reset, file, and new-row issues are sorted above unchanged rows.
7. Apply only after the preview matches the intended import.

Final Submission file upload supports large PDF/source batches up to 5000 files per request. This is a Django request-parsing limit, not a CSV row limit. If a conference upload set exceeds that number of files, split the file upload into multiple batches.

Paper Master notes are internal editorial notes. They appear in review workbooks and Note Summary, but they do not go into the final publication package manifest.

## Version Decisions

Final Submissions can be Start2 imports or Editor Uploads.

- Start2 imports are the normal author-uploaded records.
- `Add Final Submission` creates a normal Start2-origin record through a dedicated create workflow. It evaluates the entered Paper ID against Paper Master, initializes PDF/title-author/format checks as Pending, stores uploaded PDF/source paths, recalculates active and replaced versions, and writes a `final_submission_manual_create` audit event.
- Editor Uploads are email-provided replacement versions created by the editorial team.
- Before an Editor Upload is created, the PDF title is extracted in dry-run mode.
  The title safety check shows the uploaded title first and each applicable reference
  below it. Identical Paper Master and Final titles are combined instead of shown
  twice. Review the word-level highlight first; use the expandable character diff
  only when necessary. You can open the temporary PDF, choose another PDF, or cancel
  without creating a submission. Confirming a real mismatch creates an unverified
  Editor Upload that still requires Paper ID review.
- Editor Uploads are prioritized when both sources exist for the same Paper ID.
- If Start2 and Editor Upload both exist and neither is discarded, the system shows a Start2/Editor conflict and blocks final export.

Use Discard when a specific version should not be used. Discard keeps the record and note for traceability.

Use Not Publishing when the paper should not be published at all, such as unpaid, withdrawn, or intentionally excluded.

Old Versions is version history. Not Publishing is a publication decision.

## Final Publication Version Rules

The Paper Master List is the publication scope. A final submission is publication-relevant only when its Paper ID is in Paper Master List, it is not discarded, and it is not marked Not Publishing.
When the active final is marked Not Publishing, Organized List omits that Paper Master
row instead of reporting a missing final; inactive replaced versions remain history and
are never restored as publication candidates.

Active version selection is per Paper ID:

1. Discarded versions are ignored.
2. Undiscarded Editor Uploads have priority over Start2/imported submissions.
3. If Editor Uploads exist, the newest Editor Upload becomes active.
4. If no Editor Upload exists, the newest Start2/imported submission becomes active.
5. Newest follows the active-version rule in Settings: Final ID order or upload date, with Final ID as tie-breaker.
6. If Start2 and Editor Upload are both undiscarded, the Editor Upload is temporarily active, but the conflict blocks final export until one side is discarded with a note.

Publication-facing PDF priority is:

1. Corrected PDF.
2. Original PDF for the active submission.

Publication-facing source priority is:

1. Corrected source.
2. Original source for the active submission.

`data/publication_pdf_debug/` is a generated inspection folder. It is useful for quickly checking renamed PDFs, but it is not the source of truth. Publication links, CrossCheck export, duplicate checks, and publication package export use the active submission's Corrected PDF or Original PDF directly.

Legacy `current_file_path`, `active_final_folder`, and `old_versions_folder` data can still exist after restoring older state archives, but those values no longer decide final publication output.

## PDF And Source Workflow

Process PDFs does all of the following:

- Calculates page count.
- Calculates PDF hash.
- Generates page thumbnails.
- Resets page-limit exceptions if the page count changed.
- Recalculates active versions.
- Rebuilds author cache.
- Syncs `data/publication_pdf_debug/` from the same Corrected/Original PDF source used by publication export.

Process PDFs does not scan folders and does not silently create submissions. It does not intentionally rewrite original uploaded PDFs, corrected PDFs, original source files, corrected source files, extracted title/authors, plagiarism scores, or review status.

Run Process PDFs whenever Dashboard or the global alert says it is needed. Corrected PDFs require Process PDFs again so page count, hash, thumbnails, and debug copies match the current publication PDF source.

The page-preview area defaults to `All` and keeps the complete thumbnail strip for every matching publication candidate expanded. This is intentional: editors can scan first, middle, and last pages without opening each record. Use `Needs processing`, `Page issues`, `Processed`, or search to narrow papers; use `Jump to paper` for long runs. Paper headers remain visible while their strip is near the top, page tiles keep a fixed size while loading, and selecting a thumbnail opens a larger preview. These display tools do not alter processing or publication selection.

The Process PDFs status area uses the full page width when only one issue type is
present. It splits into two columns only when both unprocessed PDFs and missing-PDF
issues need to be shown at the same time.

In Organized List, checks that require a Final Submission show `--` when a Paper
Master record has no Final Submission. Empty status badges are never used as a
placeholder.

Formatting Review queue mode keeps one paper expanded at a time. Its compact row identifies Paper ID, status, edited state, PDF/source origin, and processing warning before you open the full preview/upload workspace. Single Paper Mode remains the safer sequential workflow; Save stays on the current paper and Go next remains a separate action with unsaved-change protection.

## Paper ID Review

Use `/reviews/paper-ids/` to compare author-entered IDs and titles against the Paper Master List.

- IDs not in Paper Master cannot be verified.
- If a paper is intentionally not publishing, mark it in the Not Publishing workflow instead of verifying an invalid ID.
- Verified hard title differences remain visible but are lower priority than unverified mappings.

## Title/Author Review

Use `/reviews/title-authors/` to extract title/authors from active publication PDFs.

Review statuses:

- Pending: needs review.
- Red Flag: extraction looks wrong or formatting likely needs correction.
- Review OK: title/authors have been checked.

The page also shows extracted title vs Final Submission title while you review the card. Missing metadata or a Pending/Red Flag review can block final export. Marking the card Review OK records that the displayed title difference was accepted; the difference remains visible and tracked but does not create a second blocker.

The built-in extractor is the default path. Settings can enable an optional GROBID fallback for local/internal GROBID services; the Settings page shows a green/red API health indicator beside the GROBID API URL and refreshes it while you edit the URL. The Title/Author page checks GROBID health before any GROBID action. If the API is unavailable, GROBID buttons are disabled and batch extraction is not started, so rows are not incorrectly turned into extraction errors. During a suspicious-row batch, rows are processed one at a time; if the GROBID service becomes unavailable mid-run, the batch stops, successful rows remain saved, and unprocessed rows are skipped rather than marked as paper-level errors. Use `Try GROBID` on individual rows, or `Try GROBID for suspicious rows` for rows with extraction errors or Red Flag status. If a row has missing/truncated authors but is not an extraction error, mark it Red Flag first or use the single-row button. A successful GROBID extraction overwrites extracted title/authors, creates a verification image, resets Title/Author Review back to Pending, and recalculates Extracted Title Match the same way the built-in extractor does. A failed GROBID attempt does not overwrite the current extraction.

Manual override is an exception path for cases where extracted title/authors must be corrected without editing the PDF/source. Use the row-level `Manual override` action on the Title/Author page, enter the corrected extracted title/authors, and record a required reason. Manual override resets Title/Author Review to Pending, recalculates extracted-title match, writes an audit event, and appears as an Info item in Error Report. Final Submission edit does not silently edit extracted title/authors; use the Title/Author workflow so the reason and review reset are recorded.

## Formatting Review

Use `/reviews/formatting/` to review title/author formatting visually.

- List mode is a compact queue; select `Review paper` to expand one full workspace.
- Single Paper Mode shows one paper at a time to reduce wrong-file uploads.
- Corrected PDF upload performs the same responsive title safety check in dry-run
  mode before the file is saved. It compares with the Final Submission title without
  replacing stored extracted metadata.
- Source file buttons show type labels such as Word, ZIP, or TeX.
- Review OK means the current publication version's format is acceptable.
- Edited means corrected PDF/source files exist.

If corrected files are uploaded, related review flags reset as needed and Process PDFs may be required.

Single Paper Mode remains the sequential review workspace. Save and Go next are separate, and the page warns before leaving with unsaved changes.

## Plagiarism / CrossCheck Workflow

Go to `/integrations/crosscheck/`.

1. Enter a token and export the plagiarism upload ZIP.
2. Upload the PDFs to the outside plagiarism tool.
3. Import the result CSV with `filename`, `plagiarism_percent`, and `single_percent`.
4. Upload optional report PDFs separately. Reports are matched by filename.

CrossCheck ZIP exports are limited to active, undiscarded, not-Not-Publishing submissions whose Paper ID exists in the Paper Master List. The PDFs use the same Corrected/Original publication source priority as the publication package.

Scores are displayed as whole percentages. Reports are opened through app links, not by manually browsing paths.

## Exceptions

Exceptions are rare approvals for:

- Page count below/above configured limits.
- Too many authors on one paper.
- One author appearing on too many publication papers.
- Plagiarism % above the configured threshold.
- Single % above the configured threshold.

Default status is Not allowed. Only Allowed exception with a required reason note can stop the issue from blocking final export. Plagiarism % and Single % are approved separately. If the underlying count or score changes, the exception becomes stale and must be re-approved.

For paper-level exceptions, start from Organized List. Rows with page, per-paper author-count, plagiarism score, or duplicate-author review items show a `Manage exceptions` panel. The panel only shows relevant sections for that paper and includes publication PDF/report links where useful. Use Exceptions for centralized review and for author paper-count exceptions, which are author-level decisions across multiple papers.

Exceptions also supports Paper/Final ID text search and exception-type filtering. Author Count supports focused views for over-limit authors, duplicate names inside a paper, allowed exceptions, and all authors. These filters are review aids only and do not change exception validity.

`Manage exception` from Author Count opens the exact author exception. Row-level
exception actions from Organized List continue to use the exact active Final
Submission. Dashboard issue actions open the matching workflow subset rather
than the full Error Report or a broad Needs Attention list.

## Export Workflow

Use `/reports/` for exports.

- Active Publishable Versions and Editorial Review Workbook are internal review outputs.
- Final Publication Package ZIP is strict and should be used only when readiness is clean.
- Draft Publication Package ZIP can be downloaded after warnings. It may skip missing files and includes a warnings CSV.
- Final package manifest contains ID, extracted title, extracted authors, author number, page number, Plagiarism %, and Single %. It does not include editorial notes.
- Final and draft package PDFs use the publication-facing priority above: Corrected PDF, then Original PDF. They do not read the publication debug folder or legacy active-final/current-file paths.

## Backup, Cleanup, And Clear Database

Download a System State ZIP before moving machines, archiving work, or clearing data. The snapshot includes settings, conference name, database workflow state, managed PDFs/source files, plagiarism reports, title/author verification images, page thumbnails, and format previews. Temporary import/restore/upload preview tokens are not included.

System State ZIPs include `data/logs/audit.log` and archived audit logs, so restored systems keep the same action trail.

Use Storage Management in Settings for preview-first cleanup:

- Conservative cleanup keeps referenced thumbnails/previews and publication debug or legacy output folders. It only selects unreferenced generated cache.
- Generated reports/exports cleanup removes regenerated Excel/ZIP download artifacts.

Clear Database wipes records and managed files so the app can start a new conference. Use it only after downloading a System State ZIP if the current work must be preserved.

Clear Database preserves the current audit log by default. Check `Also archive and clear audit log` only when you intentionally want a fresh log for a new environment. When checked, the app moves the current file into `data/logs/archive/` and starts a new `audit.log`.

## Audit Log

Use `/reports/audit-log/` when you need to trace a mistake or confirm what the system did. Search by Paper ID, Final ID, action name, status, or message.

The log is append-only JSON Lines stored at `data/logs/audit.log`. It records key actions such as import previews/applies, manual edits, uploads, Editor Uploads, discard/undo, Not Publishing, verification, title/author review, formatting, Process PDFs, CrossCheck export/import/report uploads, exception approvals/removals, settings changes, publication export, System State backup/restore, storage cleanup, and Clear Database.

The log records metadata, reset flags, counts, file names, hashes, and portable paths. It does not store PDF/source/report binary content.
