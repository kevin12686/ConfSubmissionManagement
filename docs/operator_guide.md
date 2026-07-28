# Operator Guide

This guide is for editors running the local system to prepare final submissions for publication.

It explains what to do and what result to expect. For the authoritative rules
that determine publication scope, active versions, selected files, readiness,
and export safety, see [Publication Rules](publication_rules.md). For setup or
failure recovery, see [Troubleshooting](troubleshooting.md). Return to the
[Documentation Home](README.md) to choose a different guide.

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

Ordinary navigation belongs in the Navbar rather than being repeated at the
upper-right of every page. Page-header buttons are therefore limited to local
commands such as Add, Import, extraction, view switching, Note Summary, or Back.
Cross-workflow links appear beside the state that requires them: blocked
Dashboard readiness opens Error Report, clear readiness opens the anchored
publication-package section, Process PDFs exposes PDF Issues only when they
exist, and plagiarism review appears after score/report data is updated.

## Page Map

| Page | URL | Main use |
| --- | --- | --- |
| Dashboard | `/` | Final-package readiness and current editorial actions |
| Paper Master List | `/papers/` | Official paper scope, publication decisions, titles, authors, acceptance status, editorial notes |
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

`Source File Issues` identifies a missing source or a source-integrity problem
after Review OK. An available Original or Corrected source with Formatting still
Pending/Needs edit is shown only under `Format Not OK`; both categories appear
together only when both the file and the Formatting workflow need attention.

Use the `Checklist / Compact candidates` control to switch views. Both use the same active Paper Master publication rows and publication-facing Corrected-to-Original file helpers. The old `/reports/active-versions/` link redirects to Compact candidates.

## Dashboard And Readiness

Dashboard uses the same blocking rows as Final Publication Package export. Its
top panel answers whether the final package can be created now; it is not an
approximate status calculation.

- **Blocking papers** counts unique affected Paper IDs.
- **Blocking checks** counts individual findings, so one paper can contribute
  more than one check.
- **Next actions** lists only workflows with current blockers.
- **No current blockers** lists workflow groups whose checks are clear.
- **Tracked information** shows non-blocking editorial context.

Uploading a corrected PDF intentionally returns the paper to PDF processing and
Title/Author Review. `Review OK` is the single Title/Author completion
decision; a reviewed title wording difference remains tracked information
instead of becoming a second blocker.

Tracked-information links open the matching subset rather than the broader
workflow tab: verified Paper Master title differences, reviewed extracted-title
differences, and allowed Plagiarism/Single exceptions each have a focused list.

### Worklists And Navigation

Large worklists default to 25 rows and support `25 / 50 / 100 / 200 / All`.
Filtering and sorting apply before pagination. Use `All` only when the complete
filtered set must be compared together.

Search is broad matching. Links created by the application target an exact
Paper ID, Final Submission, or exception and show a focused banner. An
out-of-scope target is explained instead of being replaced by a similar active
record.

Opening Final Submission Edit from a worklist preserves a safe local return
URL. Save returns to the originating view, filter, sort, search, tab, page, or
single-paper context. External return URLs are rejected.

Final Submissions opens with its tabs and version list first; `Import /
Re-upload` is collapsed until requested. Destructive version actions are kept
in the separate bottom danger zone. Not Publishing remains a dedicated
paper-level workflow.

Status colors are consistent: red is blocking or dangerous, amber needs manual
attention, blue is tracked information, green is complete, and gray is inactive
or historical. Color is never the only status signal.

Error Report filters severity, workflow area, and categories on the server
before pagination. Multiple categories use OR; area, severity, and category
dimensions combine with AND. Duplicate groups use a read-only detail view.
Exception-capable findings show `Manage exception` directly below the finding.
The full-width panel shows the current value, configured limit, recorded
approval, publication PDF/report links, and the same allow, re-approve, or
remove actions as Exceptions Center. After an action, the complete filtered
worklist refreshes: a valid approval moves the item to Info, while removing an
approval restores the blocker. Author paper-count findings can also be handled
there even though they are author-level rather than tied to one Final ID.

Implementation and presentation contracts for partial navigation, exact
targets, pagination, accessibility, and shared components live in
[UI Conventions](ui_conventions.md).

## Import Workflow

1. Download templates from the app.
2. Import the Paper Master List first.
   The page header shows the total publication-scope paper count; while searching, it
   shows the number of matching rows alongside the unchanged total.
3. Review the Paper Master Import Preview. Rows needing attention are sorted above unchanged rows.
4. For existing Paper Master notes, choose whether to preserve existing system notes or apply imported notes. The default is preserve.
5. Import Final Submission metadata and upload all PDF/source files together.
6. Review the Final Submission Import Preview. ID/reset risks, file changes,
   new rows, metadata-only changes, and unchanged rows appear in that order.
7. Apply only after the preview matches the intended import.

Paper Master and Final Submission imports are intentionally separate. Combined
`Mapping Table` workbooks are rejected because they bypass the normal source
and confirmation boundaries.

For an existing Final ID:

- the same Author-entered ID preserves the current Official Paper ID;
- a changed Author-entered ID re-resolves the Official Paper ID and resets
  Paper ID review;
- a blank current Official Paper ID is resolved from the imported
  Author-entered ID;
- a changed Final Title preserves the Official Paper ID but resets Paper ID
  review and extracted-title comparison;
- changed Final Authors return Title/Author Review to Pending without replacing
  the current extracted title/authors.

An unresolved Official Paper ID is not automatically marked Not Publishing. It
remains an ID problem for manual correction/verification, or an editor may use
the explicit Not Publishing workflow when that is the real publication
decision.

Not Publishing List places `Final Submissions Outside Paper Master` before
Missing Final and tracked decisions. For each row:

- choose `Resolve Paper ID`, search Paper Master by ID, title, or author, review
  the selected Master metadata, then verify the selection; or
- choose `Mark Not Publishing`, explicitly select a reason, and confirm the
  decision.

The compact row never edits Official Paper ID directly. `Open full Paper ID
Review` remains available when title/author diffs need closer inspection. A
successful resolution removes the orphan row because it now belongs to a valid
Paper Master ID.

Final Submission file upload supports large PDF/source batches up to 5000 files per request. This is a Django request-parsing limit, not a CSV row limit. If a conference upload set exceeds that number of files, split the file upload into multiple batches.

Paper Master notes are internal editorial notes. They appear in review workbooks and Note Summary, but they do not go into the final publication package manifest.

## Version Decisions

Final Submissions can be Start2 imports or Editor Uploads.

- Start2 imports are the normal author-uploaded records.
- `Add Final Submission` creates a normal Start2-origin record through a dedicated create workflow. It evaluates the entered Paper ID against Paper Master, initializes PDF/title-author/format checks as Pending, stores uploaded PDF/source paths, recalculates active and replaced versions, and writes a `final_submission_create` audit event.
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
The Paper Master record owns that decision, including when no Final Submission
has been received. Open Not Publishing List, focus the Paper Master record,
choose a reason, enter a note, review the publication-scope impact, and confirm.
The paper remains traceable but is removed from publication processing,
reviews, CrossCheck, readiness, and final or draft packages.

Existing and future Final Submission versions inherit the Paper Master
decision. Their exclusion fields are compatibility mirrors and do not override
the Master decision. Undo returns the paper to publication scope; if it still
has no Final, Missing Final Submission becomes a blocker again. Neither action
clears Paper ID verification or review work because no reviewed evidence
changed.

`Decision Required` means migrated state or a new Master/orphan transition was
ambiguous. For example, importing a Master ID that matches an orphan Final
already marked Not Publishing does not silently revive that Final. Import
Preview identifies the affected Final IDs, and the Master remains outside
publication scope until an editor explicitly keeps the paper in publication
scope or marks it Not Publishing. Decision Required and any Master/Final
decision integrity conflict block final, draft, and CrossCheck exports.
Old Versions uses the same tab treatment and active-count styling as the other
editorial worklists.

## Final Publication Version Rules

The complete rules are maintained in
[Publication Rules](publication_rules.md). Operationally:

- Paper Master defines the conference paper set and owns each paper's
  Publishing / Not Publishing decision.
- Discarded and Not Publishing records do not enter publication output.
- Editor Upload wins active selection over Start2, but a mixed undiscarded
  source conflict blocks final export until one side is discarded with a reason.
- Corrected PDF/source wins over Original. If a selected Corrected file is
  missing, publication is blocked rather than falling back.
- Generated debug copies and legacy path fields never select publication input.

Use Organized List and Error Report to resolve these conditions. Do not decide
the publication version by browsing files under `data/`.

## PDF And Source Workflow

Process PDFs does all of the following:

- Calculates page count.
- Calculates PDF hash.
- Generates page thumbnails.
- Resets page-limit exceptions if the page count changed.
- Recalculates active versions.
- Refreshes the compatibility author cache; publication author counts are
  calculated from current active Paper Master submissions.
- Syncs `data/publication_pdf_debug/` from the same Corrected/Original PDF source used by publication export.

Process PDFs does not scan folders and does not silently create submissions. It does not intentionally rewrite original uploaded PDFs, corrected PDFs, original source files, corrected source files, extracted title/authors, plagiarism scores, or review status.

Run Process PDFs whenever Dashboard or the global alert says it is needed. Corrected PDFs require Process PDFs again so page count, hash, thumbnails, and debug copies match the current publication PDF source.

The page-preview area defaults to `All` and keeps the complete thumbnail strip for every matching publication candidate expanded. This is intentional: editors can scan first, middle, and last pages without opening each record. Use `Needs processing`, `Page issues`, `Processed`, or the single worklist search to narrow papers by Paper ID, Final ID, or title. Filtering happens before pagination, so an exact ID search locates the matching current publication candidate even when it was previously on another page. Paper headers remain visible while their strip is near the top, page tiles keep a fixed size while loading, and selecting a thumbnail opens a larger preview. These display tools do not alter processing or publication selection.

Each preview card also shows the current Formatting status. If you notice a
problem while scanning, use `Record formatting issue` on the card, or open a
page thumbnail and choose `Record issue for this page`. Entering a note:

- appends the note to that Final Submission's existing Formatting notes;
- records the selected page number when applicable;
- changes Formatting status to `Needs edit`;
- clears a previous Formatting Review OK source binding;
- does not alter the PDF, page count, hash, thumbnails, Title/Author review, or
  plagiarism results.

Use `Open Formatting Review` when you are ready to download files, upload a
corrected version, or complete Review OK. Process PDFs deliberately does not
offer corrected-file upload or Review OK actions.

The Process PDFs status area uses the full page width when only one issue type is
present. It splits into two columns only when both unprocessed PDFs and missing-PDF
issues need to be shown at the same time.

Organized List presents structural blockers once instead of repeating their
effects across every workflow column. Missing Final, Paper ID decision,
publication decision, and multiple-active-Final rows merge the workflow columns
into one explanation with an exact resolution action. `Details` retains the
underlying metadata for traceability.

For a normal publication candidate, each issue belongs to its root workflow:

- a missing PDF is a PDF issue; Pages shows `Requires PDF` rather than a second
  blocker;
- missing extracted title/authors is an Extraction issue; Title and Authors
  show `Awaiting extraction`;
- independent page, source, review, plagiarism, and formatting findings remain
  visible in their own columns.

This presentation does not relax Error Report or Publication Package readiness.
Those services continue to report every formal publication blocker.

Formatting Review queue mode keeps one paper expanded at a time. Its compact row identifies Paper ID, status, edited state, PDF/source origin, and processing warning before you open the full preview/upload workspace. Single Paper Mode remains the safer sequential workflow; Save stays on the current paper and Go next remains a separate action with unsaved-change protection.

## Paper ID Review

Use `/reviews/paper-ids/` to compare author-entered IDs and titles against the Paper Master List.

- IDs not in Paper Master cannot be verified.
- Not Publishing List uses the same Paper Master picker, signed review
  evidence, and verification command for its orphan Final resolution panel.
- An orphan Paper ID group marked Not Publishing cannot be remapped or verified
  until that decision is explicitly undone.
- If a paper is intentionally not publishing, mark it in the Not Publishing workflow instead of verifying an invalid ID.
- Verified hard title differences remain visible but are lower priority than unverified mappings.
- After Verify, Unverify, or publication-decision actions, the worklist returns
  to the same card or continues with the next visible card when the current
  filter no longer includes it.

## Title/Author Review

Use `/reviews/title-authors/` to extract title/authors from active publication PDFs.

Review statuses:

- Pending: needs review.
- Red Flag: extraction looks wrong or formatting likely needs correction.
- Review OK: title/authors have been checked.

The page also shows extracted title vs Final Submission title while you review the card. Missing metadata or a Pending/Red Flag review can block final export. Marking the card Review OK records that the displayed title difference was accepted; the difference remains visible and tracked but does not create a second blocker.

Single-row extraction, GROBID, Manual override, and review-status actions return
to the same card. When a status change moves the paper out of the selected view,
the worklist continues at the next visible card rather than jumping to the page
header. If the Manual override panel was open, it reopens with the editable form
loaded; the loading placeholder is not the restored state.

The built-in extractor is the default path. Settings can enable an optional GROBID fallback for local/internal GROBID services; the Settings page shows a green/red API health indicator beside the GROBID API URL and refreshes it while you edit the URL. The Title/Author page checks GROBID health before any GROBID action. If the API is unavailable, GROBID buttons are disabled and batch extraction is not started, so rows are not incorrectly turned into extraction errors. During a suspicious-row batch, rows are processed one at a time; if the GROBID service becomes unavailable mid-run, the batch stops, successful rows remain saved, and unprocessed rows are skipped rather than marked as paper-level errors. Use `Try GROBID` on individual rows, or `Try GROBID for suspicious rows` for rows with extraction errors or Red Flag status. If a row has missing/truncated authors but is not an extraction error, mark it Red Flag first or use the single-row button. A successful GROBID extraction overwrites extracted title/authors, creates a verification image, resets Title/Author Review back to Pending, and recalculates Extracted Title Match the same way the built-in extractor does. A failed GROBID attempt does not overwrite the current extraction.

Manual override is an exception path for cases where extracted title/authors must be corrected without editing the PDF/source. Use the row-level `Manual override` action on the Title/Author page, enter the corrected extracted title/authors, and record a required reason. Manual override resets Title/Author Review to Pending, recalculates extracted-title match, writes an audit event, and appears as an Info item in Error Report. Final Submission edit does not silently edit extracted title/authors; use the Title/Author workflow so the reason and review reset are recorded.

All three result sources use one verification-image renderer. The image has a
header for the review-sample label, extraction source, filename, wrapped
extracted title, and numbered author legend. The renderer first confirms how
much visibly blank space exists above the PDF title, places the header in that
space, and adds only the extra height still required. Small top/bottom padding
and a safety gap before the first PDF content are always retained. If a logo,
line, image, or text occupies the top area, the renderer expands rather than
covering it. The PDF evidence marks title text in yellow with a blue underline
and gives every parsed author an independent green box and underline. Compare
the `A1`, `A2`, and later legend entries with those boundaries; two adjacent
boxes can reveal that one person's name was incorrectly parsed as two authors.
Author evidence is case-sensitive, and internal extracted words and punctuation
must appear in the PDF. The final extracted word may match the beginning of a
longer PDF word, but the renderer outlines only extracted characters. Attached
list markers, affiliations, ORCIDs, and symbols therefore stay outside a green
boundary. If the remaining characters begin with a letter, the boundary is
orange: for example, `John Smith` is visibly outlined only through `Smith` in
`John Smithson`. A different internal hyphen still does not match. If reliable
PDF character coordinates are unavailable, the renderer leaves the author
unmarked rather than drawing a misleading whole-word box.
When the same author has both a complete match and a partial match elsewhere on
the page, only the complete green evidence is shown. The numbered author legend
uses the same state colors as the PDF: green for complete/attached metadata,
orange when only partial evidence exists, and red when no evidence was found.
This does not change extracted authors, the PDF, or title comparison.
Hold `Ctrl` while pointing at the verification image to inspect the title
underline and individual author boundaries with the same magnifier used by
Formatting Review. Its `3:2` landscape shape preserves horizontal title and
author context. A normal click still opens the complete verification image.
The magnifier remains available after changing a review filter, page, or page
size; those actions replace the worklist without requiring a browser refresh.

## Formatting Review

Use `/reviews/formatting/` to review title/author formatting visually.

- List mode is a compact worklist; select `Review paper` to expand one full workspace.
- Single Paper Mode shows one paper at a time to reduce wrong-file uploads.
  Its entry is part of the current worklist toolbar, so changing a tab or search
  also updates which list will be reviewed. When it starts, the system snapshots
  the papers matching that filter and search in natural Paper ID order. That
  sequence remains stable even after a paper changes from Pending/Needs edit to
  Review OK.
- Corrected PDF upload performs the same responsive title safety check in dry-run
  mode before the file is saved. It compares with the Final Submission title without
  replacing stored extracted metadata.
- Source file buttons show type labels such as Word, ZIP, or TeX.
- On a desktop or laptop, place the pointer over the first-page preview and hold
  `Ctrl` to magnify a wide title/author area in place. The landscape lens follows the
  pointer, stays inside the image, hides its hover hint while active, and closes
  as soon as `Ctrl` is released.
  Touch devices show the normal preview; use `Open Publication PDF` when closer
  inspection is needed.
- Review OK means the current publication version's format is acceptable.
- Edited means corrected PDF/source files exist.
- In List mode, Save and title-guard confirm/cancel return to the same expanded
  paper workspace. If the saved status removes that paper from the current tab,
  the next visible paper is opened instead. The first-page preview and its
  magnifier load automatically in the restored workspace.

If corrected files are uploaded, related review flags reset as needed and Process PDFs may be required.

Single Paper Mode remains the sequential review workspace. Save and Go next are
separate: Save returns to the same paper for inspection, while Go next follows
the queue snapshot. Previous, Next, Go next, and Back to list warn before leaving
with unsaved changes. The queue keeps its original filter/search and expires
after two hours; start it again if the expiration message appears. If a queued
submission is discarded, excluded, or replaced while the queue is open, it is
shown as outside scope and Continue moves to the next valid queued paper.

An `Open Formatting Review` link from a specific Final Submission uses Focused
Formatting Review. Focus mode shows that exact active publication candidate
without adding neighboring papers or altering the sequential queue. Use `Start
Single Paper Mode here` when a sequential review should begin at that record.

Every formatting Save is bound to the publication PDF/source displayed when the
page opened. If either file or the submission changes before Save or before a
corrected-PDF title warning is confirmed, the system rejects the stale action.
Reload and review the current files rather than confirming an older page.

## Plagiarism / CrossCheck Workflow

Go to `/integrations/crosscheck/`.

1. Enter a token and export the plagiarism upload ZIP.
2. Upload the PDFs to the outside plagiarism tool.
3. Import the result CSV with `filename`, `plagiarism_percent`, and `single_percent`.
4. Upload optional report PDFs separately. Reports are matched by filename.

CrossCheck ZIP exports are limited to active, undiscarded, not-Not-Publishing submissions whose Paper ID exists in the Paper Master List. The PDFs use the same Corrected/Original publication source priority as the publication package.
Each token/scope export writes a manifest that binds Paper ID, exact Final ID,
and publication PDF SHA-256. A token/scope cannot be overwritten. Result rows
and report PDFs are accepted only while that exact Final and PDF remain the
current publication candidate; replacement-version and changed-PDF results are
reported as stale and are not applied.

Scores are displayed as whole percentages. Reports are opened through app links, not by manually browsing paths.

## Exceptions

Exceptions are rare approvals for:

- Page count below/above configured limits.
- Too many authors on one paper.
- One author appearing on too many publication papers.
- Plagiarism % above the configured threshold.
- Single % above the configured threshold.

Default status is Not allowed. Only Allowed exception with a required reason note can stop the issue from blocking final export. Plagiarism % and Single % are approved separately. If the underlying count or score changes, the exception becomes stale and must be re-approved.

For paper-level exceptions, start from Organized List when reviewing one paper
as a whole. Rows with page, per-paper author-count, plagiarism score, or
duplicate-author review items show an `Exceptions` panel. Use Error Report when
working from a blocker or tracked Info finding; its `Manage exception` panel
opens the exact exception represented by that finding. Use Exceptions Center
for centralized review. Author paper-count exceptions remain author-level
decisions across multiple papers, but their Error Report finding also opens the
correct author-level exception rather than guessing from a Paper ID.

Saving or removing one Organized List exception refreshes that paper's complete
row from the system without reloading the full page. Other reason fields that
you have typed in the same panel remain visible if they do not yet have stored
data. Stored reasons and approval state always replace browser drafts, and only
the action you clicked is written to the database. Validation errors stay
beside the affected section so you can correct them without finding the paper
again. A normal page refresh recalculates list summaries, filtering, and
sorting from the latest state.

Exceptions also supports Paper/Final ID text search and exception-type
filtering. Its summary metrics and status-tab counts recalculate within the
selected type/search scope, while the selected status tab controls which of
those scoped records are listed. Author Count supports focused views for
over-limit authors, duplicate names inside a paper, allowed exceptions, and all
authors. These filters are review aids only and do not change exception
validity.

`Manage exception` from Author Count opens the exact author exception. Row-level
exception actions from Organized List continue to use the exact active Final
Submission. Dashboard issue actions open the matching workflow subset rather
than the full Error Report or a broad Needs Attention list.

## Export Workflow

Use `/reports/` for exports.

- Final Publication Package ZIP is strict and should be used only when readiness is clean.
- Draft Publication Package ZIP can be downloaded after warnings. It may skip missing files and includes a warnings CSV.
- Final and draft package manifests contain ID, extracted title, extracted
  authors, author number, page number, Plagiarism %, Single %, and the same
  per-paper Exceptions summary shown in the Editorial Publication Workbook.
  Exception summaries include their recorded reasons. The manifest does not
  include Paper Master editorial notes.
- Editorial Publication Workbook is the primary internal Excel output. Its
  Publication Detail sheet uses Extracted Title as the primary publication
  title, keeps Master and Final titles as references, and lists each paper's
  current exceptions and readiness findings. Publication Detail is always
  included. Select only the supporting sheets needed for the current task:
  Exception Detail, Readiness Issues, Paper Master, Not Publishing, or Author
  Count. Unchecked supporting sheets are omitted.
- Raw Active Submission Data and Raw Old Version Data are technical/debug
  exports. They are collapsed under Advanced / Debug Excel and are not
  included in the Editorial Publication Workbook. They are not publication
  deliverables or complete backups.
- Generated XLSX files share frozen/filterable headers, bounded column widths,
  wrapped long text, whole-number percentage display, and consistent readable
  styling. CSV files remain unformatted machine-readable data.
- Final and draft package PDFs use the publication-facing priority above: Corrected PDF, then Original PDF. They do not read the publication debug folder or legacy active-final/current-file paths.
- Draft export can carry ordinary readiness warnings, but it still blocks when one Paper ID has multiple active finals or when sanitized ZIP filenames collide; those conditions cannot select an unambiguous file.
- Final export blocks if two records would receive the same sanitized ZIP
  filename, or if a selected PDF/source changes after the readiness inspection.
  Resolve the Error Report item or reprocess the changed PDF before retrying.
- Formatting Review binds `Review OK` to the exact publication source bytes.
  After upgrading to archive format 3, existing ready records without this hash
  must be opened in Formatting Review and saved as `Review OK` again. If a
  Corrected PDF/source is selected but missing, restore or replace it; the
  system will not silently publish the Original file.
- Pending or Needs Edit Formatting records do not have a completed source-review
  hash yet. Error Report tracks them as `Formatting Not Review OK`; `Source
  Review Hash Missing` is reserved for records already marked Review OK whose
  integrity binding is unexpectedly absent.

## Backup, Cleanup, And Clear Database

### System State

Download a System State ZIP before moving machines, archiving work, performing
major maintenance, or clearing data. It includes settings, conference records,
managed files, reports, review artifacts, and active and archived audit logs.
Temporary preview tokens are excluded.

Restore is preview-before-apply and remaps managed paths to the receiving
installation. Do not continue publishing from a restore that reports an
unsupported archive version, corrupt file, or unresolved old-machine path.

### Docker Data

Docker runtime backup, update, named-volume migration, and host-mirror rollback
are documented in the [Docker Guide](docker_guide.md). The raw
`SMS_DATA_DIR` mirror is for immediate operational rollback; the System State
ZIP is the portable, versioned application backup.

Never run `docker compose down -v` for a conference instance because `-v`
deletes its named data volume.

### Storage Cleanup

Use Settings > Storage Management and review every preview before Apply:

- Conservative cleanup selects only unreferenced regenerated cache.
- Generated reports/exports cleanup selects reproducible downloads and external
  upload packages.
- Original and corrected uploads, plagiarism reports, System State backups, and
  referenced thumbnails/previews remain protected.
- Apply skips files that changed, became referenced, or became protected after
  Preview.
- An unreadable managed folder blocks cleanup instead of being treated as
  empty.

The Settings form loads before Storage Management and GROBID health checks
finish. Their separate read-only requests do not change records.

### Clear Database

Clear Database wipes database records and app-owned managed files so the
application can start a new conference. Download a System State ZIP first when
the current work must be preserved.

Only application-owned `data` and media locations are staged for deletion.
Configured absolute folders outside those roots are preserved. If the database
reset fails, staged files are restored.

The active audit log is preserved by default. Select **Also archive and clear
audit log** only when intentionally starting a fresh action trail.

## Audit Log

Use `/reports/audit-log/` when you need to trace a mistake or confirm what the
system did. Filter by workflow category, canonical action, or status, and
search Paper ID, Final ID, action name, or message. Action names consistently
use `<domain>_<operation>[_<phase>]`; preview, apply, cancel, and undo remain in
the action name when they represent distinct workflow stages.

The log is append-only JSON Lines stored at `data/logs/audit.log`. It records key actions such as import previews/applies, manual edits, uploads, Editor Uploads, discard/undo, Not Publishing, verification, title/author review, formatting, Process PDFs, CrossCheck export/import/report uploads, exception approvals/removals, settings changes, publication export, System State backup/restore, storage cleanup, and Clear Database.

Historical logs are not rewritten. The Audit Log page maps legacy action names
to their canonical action for filtering and display, while expanded JSON keeps
the original stored event. Expanded events provide a syntax-highlighted
Formatted view, a Plain view, and a Copy JSON action; these presentation tools
do not modify the append-only log.

The log records metadata, reset flags, counts, file names, hashes, and portable paths. It does not store PDF/source/report binary content.

Multi-editor review forms carry signed evidence for the exact values shown on
the page. Final Submission Edit, Paper Master Edit, Title/Author Review,
Exceptions, Settings, and Process PDF formatting triage reject a submit if
another editor changed the relevant record first. Editor Upload confirmation
also rejects changed temporary file bytes or changed Paper Master data. Reload
and review the new values; stale forms never merge or overwrite
publication-critical state. Temporary Editor Upload and Formatting previews
expire automatically after two hours; a changed preview is deleted when the
system rejects it, so the files must be uploaded and reviewed again.

`/admin/` is a read-only diagnostic view for publication-critical records. Use
the normal editorial pages for all changes.
