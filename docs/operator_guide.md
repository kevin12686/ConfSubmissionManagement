# Operator Guide

Use this guide to prepare a conference from import to final delivery.
For installation see [Deployment](deployment.md); for exact safety rules see
[Publication Rules](publication_rules.md).

## Start With One Conference

Set the Conference name, timezone and editorial limits in Settings. Restore an
existing conference through System State if needed; do not re-import CSVs as a
replacement for a full restore.

Dashboard answers "what still blocks publication?" Organized List is the
per-paper workspace. Next actions are unresolved work; Tracked information
includes reviewed differences and permitted exceptions, not more required work.

## Page Map

| Page | URL | Use |
| --- | --- | --- |
| Dashboard | `/` | Readiness and next actions |
| Paper Master List | `/papers/` | Official roster, titles, notes and publication scope |
| Final Submissions | `/submissions/` | All submission-version records |
| Editor Upload | `/submissions/editor-upload/` | A new replacement submission |
| Organized List | `/submissions/organized/` | Per-paper publication checklist and Details |
| Process PDFs | `/processing/pdfs/` | Pages, hashes, thumbnails and debug copies |
| Paper ID Review | `/reviews/paper-ids/` | Resolve and verify the Official Paper ID |
| Title/Author Review | `/reviews/title-authors/` | Extract, inspect and review metadata |
| Formatting Review | `/reviews/formatting/` | Review formatting, upload corrections, compare PDFs |
| Not Publishing | `/reviews/not-publishing/` | Publication decisions, missing Finals and orphan Finals |
| Exceptions | `/reviews/exceptions/` | Central approvals for supported limits |
| Error Report | `/reports/errors/` | Blocking and informational findings |
| Author Count | `/reports/author-count/` | Authors across publication papers |
| Old Versions | `/reports/old-versions/` | Inactive submission history |
| Export Reports | `/reports/` | Final/draft ZIPs and internal Excel |
| Plagiarism / CrossCheck | `/integrations/crosscheck/` | Publication PDF batches and result imports |
| System State | `/integrations/system-state/` | Portable backup and restore |
| Audit Log | `/reports/audit-log/` | Search actions and before/after details |
| Settings | `/settings/` | Conference configuration and Storage Management |

Organized List Details includes extracted authors, source labels, current
publication PDF/source, workflow links and notes. Compact candidates is a
read-only alternate view of the same scope; `/reports/active-versions/`
redirects there.

## 1. Import The Roster And Files

1. Download the templates in the application.
2. Import Paper Master first; inspect its preview.
3. Preserve existing system notes unless intentionally replacing them from the
   file. This is the default for existing records; new papers use imported notes.
4. Import Final metadata with matching PDFs/source files; inspect its preview.
5. Confirm Apply only after reviewing changed IDs, files and reset effects.

Previews sort attention-needed records before unchanged ones. Imports update
or create by ID; a record missing from the file is not a request to delete it.
Combined Mapping Table workbooks are not supported. The request limit is
5000 uploaded files, not 5000 CSV rows; split larger upload batches.

Re-importing the same Final ID preserves its Official Paper ID while the
Author-entered ID is unchanged and the Official ID is nonblank. A changed
Author-entered ID re-resolves the mapping and resets its review. Final Title
changes reset review, not the Official ID. An unresolved ID needs human
resolution; it is not automatically Not Publishing.

Paper Master notes are internal. Note Summary on Master and Organized List
shows complete notes; internal workbooks include them, the publication manifest
does not. Stored notes trim outer/per-line whitespace and repeated blank lines.

### Editing A Record

Paper Master and Final Submission edits use **Review changes**, then
**Confirm and save changes**. Changed badges mean values differ from the loaded
form, not merely that a control was touched. Inspect server-calculated old/new
values and resets before confirming. If evidence changed meanwhile, reopen
the record; stale previews do not overwrite it.

## 2. Resolve Identity, Versions And Scope

| Situation | Action |
| --- | --- |
| Incorrect/unresolved Official Paper ID | Choose the real Master record in Paper ID Review and verify |
| Final outside Master | Resolve Paper ID or explicitly mark Not Publishing; neither happens automatically |
| Master has no Final and will not publish | Mark the **Master paper** Not Publishing with reason, optional note and impact confirmation |
| Decision Required | Explicitly choose Publishing or Not Publishing before export |
| Mixed Start2 and Editor Uploads | Inspect versions; discard the unwanted side with a required reason |
| A particular version should not be used | Discard that version; it remains in history |
| A paper is withdrawn/unpaid/excluded | Use Not Publishing, not Discard |

Editor Upload creates a separate submission, not a Formatting correction.
Its dry-run title guard compares the PDF title to both Master and effective
Final Title. Blank Final Title uses Master Title. A mismatch/error requires
confirmation and leaves ID review pending; a match can verify the ID.
The new PDF still needs processing and the normal reviews. Expand Editor note
to read the full upload note.

Old Versions classifies inactive versions as Discarded, Replaced or Other
inactive. Origin is separate. A Not publishing flag is a secondary decision,
not a type of old version.

## 3. Process PDFs And Review Metadata

Run **Process Publication PDFs** after new/corrected PDFs. It refreshes
page counts, PDF hashes, thumbnails and inspection debug copies. It does not
change source manuscripts, extracted metadata, plagiarism scores or unrelated
reviews. Only current Publishing Master candidates are processed.

All page thumbnails remain expanded for blank-page scanning. If a page has a
formatting problem, record it there with an optional page number: this appends
Formatting notes and marks Needs Edit through the existing workflow.

In **Title/Author Review**:

1. Extract using the built-in extractor.
2. Inspect the verification image and extracted metadata against the actual PDF.
3. Use Red Flag if formatting needs correction and re-extraction.
4. Mark Review OK when the metadata/differences have been reviewed.

Review OK is the completion decision. A reviewed title difference is Tracked
information, not a second mandatory Confirm match task.

| Extraction option | Use / consequence |
| --- | --- |
| Built-in | Default local extractor |
| GROBID | Optional trusted service configured in Settings; availability checked before use |
| GROBID suspicious batch | Extraction error or Red Flag records; stops on service loss rather than failing the remaining batch blindly |
| Manual override | Exceptional correction of extracted metadata only; requires a reason, new evidence image and renewed Review OK |

All three use the shared verification renderer. Manual override is visibly
marked and audited; it does not edit Final Title or the PDF. Re-extraction
clears its override metadata. Filters include manual overrides and title-match
states. Re-extraction resets dependent reviews even if the new text looks good.

## 4. Formatting And Revision History

Open a paper's review panel, inspect its files, choose status and save.
Source buttons identify Word/ZIP/TeX/etc. Upload Corrected files here, not through
a general extracted-metadata edit.

A Corrected PDF first undergoes a dry-run title check against Final Title.
Mismatch/extraction failure requires confirmation before files are saved. This
guard never overwrites official extracted metadata. Source-only uploads do not
run a PDF title guard.

Changing files invalidates dependent evidence; follow the resulting processing
and review tasks. If an already Review OK record receives new files, it returns
to Pending. Saving Review OK binds the selected source bytes.

### Single Paper Mode

Start from the desired filter/search; the queue captures that worklist.
Save stays on the current paper, including when it leaves that filter.
Use Go next when ready. Previous/Next/Back preserve the queue context;
unsaved changes trigger a leave confirmation. Ineligible or expired queue
members cannot authorize stale saves. Single mode is not an auto-save workflow.

### Revision History

Expand Revision history to view/download complete captured PDF/source pairs.
A PDF-only or source-only upload captures the other file as it was used then.
Status/notes-only saves create audit events, not revisions.

Existing files captured is a baseline, not a reconstructed upload history.
Current files means matching current row files/bytes, not publication approval.
History has no restore, delete or "make current" operation; missing history
never causes another version to be selected. History also appears on Final edit
for inactive/discarded records.

### PDF Comparison

A saved PDF upload starts a separate comparison with Original, bound to that
exact saved revision. Failure does not undo or replace saved files.

| Result | How to review |
| --- | --- |
| Red text difference with arrow | Inspect added/removed/changed OCR text |
| Amber unlocated range | Manually locate the passage; not proof of a deletion or a match |
| Neutral Info | Inspectable visual, spacing or hyphen tolerance; not equality proof |
| Unavailable | Comparison failed; never means no changes |
| Limited / complete coverage | Describes engine execution, not publication approval |

The paired documents are primary. Hover to magnify; the upper dock keeps both
crops visible. Auto match uses the same selected region pair as its connection,
following the pointer even beyond the box. Without a pair it uses relative
position. Click a mark for persistent Evidence; entering the dock freezes
crops. Linked pages without a counterpart show an empty state, not page one.
Technical details contains evidence counts, policy and file identities.

Formatting shows remote/local version, CPU/GPU, availability and settings.
Status refreshes every ten seconds while visible. A configured unavailable
remote pauses new work; confirm local CPU only if willing to wait.
Complete verified cache results display without new-compute confirmation.
A result's execution label records who actually computed it, not current health.

Comparison cannot certify figures, tables, layout or formulas. Review those
visually. [PDF Comparison](pdf_comparison.md) lists accepted blind spots.

## 5. Plagiarism / CrossCheck

### Automatic Service

1. In Settings, enter service URL and optional login, test/load folders, select
   the exact folder and confirm its conference association.
2. Preview All publication PDFs or Missing results only; check scope, folder,
   Final IDs and PDF sources, then confirm upload.
3. Monitor progress or return later. Visible pages poll every 15 seconds; hidden
   pages pause, and failed checks wait for Check progress.
4. Inspect per-paper retrieved scores/reports and explicitly confirm Apply.
5. For confirmed uploads still lacking reports, use Retry remaining downloads.
   It starts another download execution, not another PDF upload.
6. Use Execution history for immutable ZIPs and report evidence.

Job completion does not imply every report exists. Retry results need the same
preview/apply confirmation. Already applied papers cannot be replayed or
overwritten; changed active Final/PDF makes an old result stale, even if another
version has identical bytes. On a lost response, Check progress before retrying;
do not blindly submit again.

New service URL is blank by default. Optional credentials are saved locally
for that URL, not in System State. Environment overrides take precedence.
Confirm the folder again after changing conference name, service URL or folder.

### Manual Workflow

Enter a batch token; prepare All PDFs or Missing Results Only ZIP, upload to the
external tool, then import its CSV and optional report PDFs. Both exports use
current publication PDFs and retain exact Final/hash provenance.
Missing results means either P% or S% is missing, not a missing report or stale
report flag. All and Missing ZIPs do not overwrite one another.

## 6. Resolve Exceptions And Export

Exceptions support page limits, authors per paper, papers per author and
separate P%/S% thresholds. Approve only with a reason; changed values make the
approval stale. Report replacement alone does not clear score exceptions.
Organized List shows only relevant per-paper sections; author paper-count
decisions belong to Author Count/Exceptions. Error Report provides exact
exception actions too.

| Export | Purpose |
| --- | --- |
| Final Publication Package ZIP | Strict checked deliverable; Not Publishing excluded |
| Draft Package ZIP | Intermediate output with warnings; structural ambiguity still blocks |
| Editorial Publication Workbook | Internal Publication Detail plus selected supporting sheets |
| Advanced / Debug Excel | Raw active/old-version data, not the deliverable or a backup |

Publication Detail is always included. Optional sheets: Exception Detail,
Readiness Issues, Paper Master, Not Publishing and Author Count. Thus a workbook
may intentionally include excluded records in its supporting sheets.
The manifest contains publication metadata plus Exceptions and reasons, but not
Master editorial notes or internal manual-override reasons.

Before handoff: review Error Report, inspect actual publication PDFs/source,
export the final ZIP, and save a System State ZIP.
[Operations](operations.md) covers backup, cleanup and audit recovery.

## PDF Reader

Open PDF actions use the same full-window PDF.js reader and originating endpoint.
Default is Fit page / Single page; Continuous is optional. Wheel turns pages;
when zoomed it pans first, then a fresh edge gesture turns the page. Keyboard:
Left/Right change pages, Up/Down and Page Up/Down pan before turning, Home/End
jump to ends. Inputs/toolbars retain their native keys. Thumbnail clicks jump
to pages. Open directly and Download use the same selected file endpoint.

Ctrl-hover magnification is shared by Formatting first-page previews and
Title/Author images; the hint hides while magnifying.

## Trace A Change

Audit Log supports category/action/status/search and readable edit summaries,
with expanded formatted/raw JSON. Events include before/after values, resets
and file metadata, not PDF/source binaries. Old action names are normalized for
display without rewriting the log. If something seems stale, inspect the exact
Paper/Final ID and audit trail before re-uploading or re-approving.
