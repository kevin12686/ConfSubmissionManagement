# Operator Guide

This guide is for editors running the local system to prepare final submissions for publication.

## Start And Restore

1. Start the app with `start.command`, `start_windows.bat`, or `./scripts/start_local.sh`.
2. Open <http://127.0.0.1:8000/>.
3. If continuing an existing conference on a new machine, go to `/integrations/crosscheck/` and restore a System State ZIP before importing new files.
4. Set the conference name and limits in `/settings/`.

The Conference Final Manager icon appears in the browser tab and beside the application name in the top navigation bar.

## Page Map

| Page | URL | Main use |
| --- | --- | --- |
| Dashboard | `/` | Current status and next warnings |
| Paper Master List | `/papers/` | Official publication scope, titles, authors, acceptance status, editorial notes |
| Final Submissions | `/submissions/` | Imported Start2 submissions, uploaded files, editor uploads, discarded versions |
| Editor Upload | `/submissions/editor-upload/` | Add email-provided replacement versions |
| Organized List | `/submissions/organized/` | Publication checklist by Paper Master record |
| Process PDFs | `/processing/pdfs/` | Page count, hash, thumbnails, and publication PDF debug copies |
| Verify Paper IDs | `/reviews/paper-ids/` | Correct author-entered IDs and verify mapping |
| Title/Author Extraction | `/reviews/title-authors/` | Run extraction and review extracted title/authors |
| Formatting Review | `/reviews/formatting/` | Review first-page title/author formatting and upload corrected files |
| Not Publishing List | `/reviews/not-publishing/` | Track paid/published scope exclusions |
| Exceptions | `/reviews/exceptions/` | Approve rare page/author/plagiarism exceptions |
| Error Report | `/reports/errors/` | Critical, Medium, and Info readiness issues |
| Author Count | `/reports/author-count/` | Per-author publication paper counts |
| Audit Log | `/reports/audit-log/` | Searchable record of important actions and file/state changes |
| Export Reports | `/reports/` | Excel exports and publication package ZIPs |
| CrossCheck / Backup | `/integrations/crosscheck/` | Plagiarism package export/import and System State backup/restore |

## Import Workflow

1. Download templates from the app.
2. Import the Paper Master List first.
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
- Editor Uploads are email-provided replacement versions created by the editorial team.
- Editor Uploads are prioritized when both sources exist for the same Paper ID.
- If Start2 and Editor Upload both exist and neither is discarded, the system shows a Start2/Editor conflict and blocks final export.

Use Discard when a specific version should not be used. Discard keeps the record and note for traceability.

Use Not Publishing when the paper should not be published at all, such as unpaid, withdrawn, or intentionally excluded.

Old Versions is version history. Not Publishing is a publication decision.

## Final Publication Version Rules

The Paper Master List is the publication scope. A final submission is publication-relevant only when its Paper ID is in Paper Master List, it is not discarded, and it is not marked Not Publishing.

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

The page also tracks extracted title vs Final Submission title. Missing or unverified matches can block final export. Soft title differences are shown for attention but do not block by themselves.

The built-in extractor is the default path. Settings can enable an optional GROBID fallback for local/internal GROBID services; the Settings page shows a green/red API health indicator beside the GROBID API URL and refreshes it while you edit the URL. The Title/Author page checks GROBID health before any GROBID action. If the API is unavailable, GROBID buttons are disabled and batch extraction is not started, so rows are not incorrectly turned into extraction errors. During a suspicious-row batch, rows are processed one at a time; if the GROBID service becomes unavailable mid-run, the batch stops, successful rows remain saved, and unprocessed rows are skipped rather than marked as paper-level errors. Use `Try GROBID` on individual rows, or `Try GROBID for suspicious rows` for rows with extraction errors or Red Flag status. If a row has missing/truncated authors but is not an extraction error, mark it Red Flag first or use the single-row button. A successful GROBID extraction overwrites extracted title/authors, creates a verification image, resets Title/Author Review back to Pending, and recalculates Extracted Title Match the same way the built-in extractor does. A failed GROBID attempt does not overwrite the current extraction.

Manual override is an exception path for cases where extracted title/authors must be corrected without editing the PDF/source. Use the row-level `Manual override` action on the Title/Author page, enter the corrected extracted title/authors, and record a required reason. Manual override resets Title/Author Review to Pending, recalculates extracted-title match, writes an audit event, and appears as an Info item in Error Report. Final Submission edit does not silently edit extracted title/authors; use the Title/Author workflow so the reason and review reset are recorded.

## Formatting Review

Use `/reviews/formatting/` to review title/author formatting visually.

- List mode shows many papers.
- Single Paper Mode shows one paper at a time to reduce wrong-file uploads.
- Corrected PDF upload performs a title guard: the PDF title is extracted in dry-run mode and compared with the Paper Master title and Final Title before the file is saved.
- Source file buttons show type labels such as Word, ZIP, or TeX.
- Review OK means the current publication version's format is acceptable.
- Edited means corrected PDF/source files exist.

If corrected files are uploaded, related review flags reset as needed and Process PDFs may be required.

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
