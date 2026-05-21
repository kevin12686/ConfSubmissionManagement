# Troubleshooting

Use this guide when the local app behaves unexpectedly.

## Startup

### Python was not found

Install Python 3.12 or newer.

On Windows, enable `Add python.exe to PATH` during installation. The Windows startup script tries `py -3` first, then `python`.

On macOS, install Python 3 and run:

```bash
python3 --version
```

### macOS says the script is not executable

Run:

```bash
chmod +x start.command scripts/start_local.sh
```

### Port 8000 is already in use

Stop the other server or choose another port:

```bash
DJANGO_PORT=8001 ./scripts/start_local.sh
```

Then open <http://127.0.0.1:8001/>.

### Packages fail to install

The first run needs internet access. Retry after checking network access:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## Imports

### CSV has encoding errors

Use the app's template download when possible. If a CSV came from Excel, save it as UTF-8 CSV or upload XLSX instead.

### Preview shows changes but Apply has not run

Preview pages do not mutate records or files. Review the differences and click Apply Confirmed Changes when the preview matches the intended import.

### Paper Master notes would be overwritten

Paper Master Import Preview offers a notes choice. Keep the default Preserve existing system notes unless the import file intentionally contains the updated internal notes.

### Final Submission files did not match

File names should include the Final Submission ID, for example:

- `34_file_Submit_PDF.pdf`
- `34_file_Submit_Source.zip`

The app uses file extension to identify PDFs and source files, so misplaced upload slots can still be corrected if the extension is clear.

## Processing And Review

### Dashboard shows Process PDFs needed

Run `/processing/pdfs/`. This alert means an active publication PDF source exists but page count, hash, thumbnails, or debug-copy refresh is missing or stale.

Missing PDFs are separate issues and do not count as process-needed.

### What does Process PDFs change?

Process PDFs is not only a page-count button. It:

- Calculates page count and PDF hash.
- Generates page thumbnails.
- Clears page-limit exceptions if page count changed.
- Recalculates active versions.
- Rebuilds author cache.
- Syncs `data/publication_pdf_debug/` from the same Corrected/Original PDF source used by publication export.

It does not scan folders, create submissions, modify original uploads, modify corrected uploads, change source files, change extracted title/authors, change plagiarism scores, or change review statuses. Publication ZIP and CrossCheck ZIP do not read the debug folder.

### Corrected PDF was uploaded but pages look old

Run Process PDFs again. Corrected PDFs are the first publication PDF priority, but thumbnails, page count, hash, and debug copies must be refreshed.

### Why did Settings show a missing legacy processed PDF path?

Settings Storage checks DB file references against files on disk. `Legacy processed PDF path` means the old `current_file_path` field. It is retained for older restored data and debug traces, but it is no longer used to choose publication files.

If a real publication source is missing, Error Report shows Missing PDF based on the active submission's Corrected PDF or Original PDF.

### Could publication debug or active-final affect the Publication ZIP?

No. Current publication ZIP generation uses the publication-facing PDF helper:

1. Corrected PDF.
2. Original active-submission PDF.

`data/publication_pdf_debug/`, legacy `data/active_final/`, and `current_file_path` are not read when building the final or draft publication package.

### Editor Upload asks for confirmation

Editor Upload performs a dry-run title extraction and compares the PDF title with the selected Paper Master title and Final Title. If either comparison differs, confirm only after checking that the uploaded file is the intended paper.

### Formatting upload asks for confirmation

Corrected PDF upload also runs a title guard. A mismatch does not forbid saving, but it prevents accidental wrong-file upload by requiring confirmation.

### Title/Author extraction is wrong

Use the Title/Author page:

- Mark Red Flag if the PDF formatting likely needs correction.
- Correct formatting and upload a corrected PDF/source if needed.
- Re-extract only the needed records when possible.
- Review OK only after extracted title/authors and verification image are acceptable.

### A paper disappeared from a filtered page

Check the page filter. Organized List defaults to All, but review pages can be filtered by status. Use All if you need to confirm whether the record still exists.

## Version And Publication Decisions

### Start2/Editor version decision needed

The same Paper ID has both an undiscarded Start2 version and an undiscarded Editor Upload. The editor upload is temporarily prioritized, but final export is blocked until one side is discarded with a note.

### Discard vs Not Publishing

Discard excludes one Final Submission version from active selection.

Not Publishing excludes the paper from publication output because of an editorial decision such as unpaid, withdrawn, or not in the final publication scope.

Old Versions shows version history. Not Publishing List shows publication decisions.

### ID cannot be verified

A Paper ID must exist in the Paper Master List before it can be verified. Correct the ID or mark the record as Not Publishing if it should not be published.

## Plagiarism / CrossCheck

### CrossCheck result import has no report

Reports are optional. The CSV imports `filename`, `plagiarism_percent`, and `single_percent`. Upload report PDFs separately if they exist.

### Percent values include `<1%`

The app treats `<1%` as 1 for score storage/display.

### Report link is missing

Upload report PDFs through the report upload workflow. The app matches reports by filename and then shows an open-report link.

## Exports

### Final publication package is blocked

Open Error Report and resolve Critical blockers, or approve valid exceptions in Exceptions.

Common blockers:

- Missing active final for a Paper Master record.
- Invalid or unverified Paper ID.
- Missing PDF or source.
- PDF not processed.
- Page count outside limits without allowed exception.
- Title/author review not OK.
- Formatting not Review OK.
- Missing or over-threshold plagiarism scores.
- Duplicate publication title/PDF/source.
- Unresolved duplicate author.
- Start2/Editor conflict.

### Need an intermediate package anyway

Use Download Draft Package Anyway from Export Reports after reviewing the warning. The draft ZIP may skip missing files and includes a warnings CSV. Do not treat it as final-ready.

### Excel export fails

Run:

```bash
.venv/bin/python manage.py check
```

If the error mentions dates, inspect uploaded `upload_date` values and re-import with valid dates. If the export still fails, use the Error Report text and recent import preview to identify the row with invalid data.

## Storage And Backup

### Generated reports are taking space

Use Settings > Storage Management > generated reports/exports cleanup. It removes regenerated Excel/ZIP downloads and external upload packages.

It does not remove original uploads, corrected files, plagiarism report PDFs, System State backups, or thumbnails/previews still referenced by the database.

Conservative cleanup can select unreferenced generated cache and orphan active/old output files. Review the preview before Apply. Do not apply cleanup if the candidate list includes publication outputs that you still need for audit or final checks.

### Thumbnails or previews are missing

Run Process PDFs for page thumbnails. Run Title/Author Extraction for verification images. Referenced thumbnails and previews are not removed by conservative cleanup.

If they are missing after a System State restore, the ZIP may have been created by an older app version that did not include all review artifacts. Use a fresh System State ZIP from the original machine when possible; otherwise regenerate the missing artifacts and re-check the affected reviews before final export.

### System State restore says unsupported version

The ZIP was created by a different state archive version. Use the matching app version shown in the footer and in the ZIP manifest.

### Restored paths point to the wrong computer

System State restore should remap managed files into this project's `data/` tree. If a path still points to another machine or a temp folder, do not continue publishing from that state. Export a fresh System State ZIP from the original machine and restore again.

### Need a completely clean conference

Download a System State ZIP first if the current work must be preserved. Then use Settings > Clear Database. This wipes database records and managed files so the app starts a new conference environment.
