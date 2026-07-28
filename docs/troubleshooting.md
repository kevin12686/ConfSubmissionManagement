# Troubleshooting

Use this guide when the local app behaves unexpectedly.

For normal task order, use the [Operator Guide](operator_guide.md). For the
rules behind publication blockers and selected files, use
[Publication Rules](publication_rules.md). This guide focuses on symptoms,
diagnosis, and recovery. Docker procedures are centralized in the
[Docker Guide](docker_guide.md); the complete guide map is on the
[Documentation Home](README.md).

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

If Apply says a preview file changed, do not reuse the token. Upload and preview
the metadata/files again; the stored size or SHA-256 no longer matches what was
reviewed.

Editor Upload and Formatting title-guard previews also expire after two hours.
An expired, missing, or changed preview is intentionally not recoverable; start
a new preview so the confirmation is bound to the exact current bytes.

### Paper Master notes would be overwritten

Paper Master Import Preview offers a notes choice. Keep the default Preserve existing system notes unless the import file intentionally contains the updated internal notes.

### Final Submission files did not match

File names should include the Final Submission ID, for example:

- `34_file_Submit_PDF.pdf`
- `34_file_Submit_Source.zip`

The app uses file extension to identify PDFs and source files, so misplaced upload slots can still be corrected if the extension is clear.

## Processing And Review

### Dashboard looks clear but final export is blocked

Dashboard and final package export use the same readiness service. Refresh the page after an import or review action. If they still disagree, do not force the final package; record the categories shown by Export Reports and inspect Audit Log. A disagreement is a system defect, not an editorial condition to work around.

The Dashboard header distinguishes affected papers from blocking checks. One paper can have several checks, so the two numbers do not always match.

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

### Process PDFs page is long

Every page thumbnail for each paper on the current worklist page stays expanded
so blank middle/end pages remain visible. Use the paper status filters, the
Paper ID / Final ID / title search, and page-size control. Filtering occurs
before pagination, so an exact ID search can locate a paper that was previously
on another page. Select `All` only when the complete filtered set must be
inspected together. Images use fixed-size lazy-loaded tiles, so rapid scrolling
should not change the layout.

### I found a formatting problem while scanning Process PDFs

Use `Record formatting issue` on that paper card. If the issue belongs to a
specific page, open its enlarged thumbnail and choose
`Record issue for this page`. The note is stored in the existing Formatting
Review notes and the paper becomes `Needs edit`; no PDF or processing metadata
is changed. Open Formatting Review from the same card to correct files or
complete Review OK. If a previous Review OK existed, recording the issue
intentionally invalidates that approval until the paper is reviewed again.

### Formatting queue filter did not update

Formatting, Process PDFs, Organized List, Final Submissions, Author Count,
Exceptions, Title/Author Review, and Verify Paper IDs use the locally bundled
HTMX asset for GET-only worklist updates. Dashboard readiness and global alerts
also load asynchronously. If a partial request fails, the page shows a red
alert and no workflow state is changed. Retry or refresh. Worklists remain
normal Django GET URLs. If assets are missing after deployment, confirm
`htmx-2.0.10.min.js`, `tabler-1.4.0.min.css`, and
`tabler-1.4.0.min.js` exist under static files. For Docker/Gunicorn, also check
that `web` completed `collectstatic`, became healthy, and `proxy` mounts the
same `sms_static` volume read-only. Nginx serves the resulting `STATIC_ROOT` in
Docker; WhiteNoise is only the non-Docker Gunicorn fallback.

### First readiness load after restart is slower

Dashboard readiness, Error Report, and publication duplicate checks must read
the current publication files. The first request after restarting the Python
process computes content hashes; later requests can reuse a hash only while
the file's device, inode, size, modification time, and change time are
unchanged. Worklist filters and normal pagination still inspect only the
necessary scope and hydrate only visible rows.

If repeated requests remain slow, check whether media is on a high-latency
filesystem or Docker bind mount and confirm the files are not being rewritten
continuously. Do not work around the delay by forcing a publication package:
final export performs a strict fresh validation.

### Publication ZIP download stalls through a proxy or tunnel

Publication ZIP responses should include `Content-Type: application/zip` and
`Content-Length`, without `Content-Encoding: gzip`. The application deliberately
does not recompress ZIP, PDF, image, Office, or unknown binary responses. If the
headers still show gzip, rebuild/restart the deployment from the current code
and confirm the proxy or tunnel is not independently compressing the response.

Current Docker Nginx uses response buffering for dynamic downloads. Large
responses may temporarily consume proxy-container disk space, up to `10240m`
(10 GiB) per
request, while a slow browser receives the file. This is not a download cache:
the temporary response is not shared or reused and is removed when the request
finishes or is abandoned. Check Docker host free space if several large
downloads run concurrently.
Responses larger than `10240m` still download, but once the finite buffer is
full Gunicorn may again wait for the client to consume data.

Buffering lets Gunicorn finish its local handoff sooner, but it cannot repair a
failed export or a connection interrupted after ZIP bytes have already reached
the browser. Retry the export from the application and verify the downloaded
archive normally. The gateway fallback may replace an upstream `502`, `503`,
or `504` only before response delivery has begun; it never automatically
replays the export POST.

### Upload drop zone summary looks wrong

The upload summary is only a pre-submit convenience based on filename extensions. Remove/reselect the affected files and submit again. The server remains authoritative: Final Submission import still previews metadata/file matches and uses extension/hash checks before Apply. No file is stored merely by dropping it into the browser zone.

### Final import rejects a Mapping Table workbook

Combined Mapping Table workbooks are no longer supported. Download and use the
Paper Master template on the Paper Master page, apply that preview first, then
use the Final Submission template and file upload on Final Submissions.

For an existing Final ID, changing Final Title does not silently move the row
to another Official Paper ID. The current Official ID is preserved and Paper ID
review is reset. Change the Author-entered ID only when the submitted ID itself
was corrected. An invalid/unresolved ID remains in Paper ID Review; it is not
automatically classified as Not Publishing.

### Why did Settings show a missing legacy processed PDF path?

Settings Storage checks DB file references against files on disk. `Legacy processed PDF path` means the old `current_file_path` field. It is retained for older restored data and debug traces, but it is no longer used to choose publication files.

If a real publication source is missing, Error Report shows Missing PDF based on the active submission's Corrected PDF or Original PDF.

### Could publication debug or active-final affect the Publication ZIP?

No. Current publication ZIP generation uses the publication-facing PDF helper:

1. Corrected PDF.
2. Original active-submission PDF.

`data/publication_pdf_debug/`, legacy `data/active_final/`, and `current_file_path` are not read when building the final or draft publication package.

### Editor Upload asks for confirmation

Editor Upload performs a dry-run title extraction and compares the PDF title with the selected Paper Master title and Final Title. The uploaded title is shown once above vertically stacked comparisons so long titles do not overlap on short or narrow screens. If Paper Master and Final Title are exactly the same, they appear as one reference. Open the temporary PDF to inspect it, choose another PDF to replace the preview, or cancel without creating a record. Confirm a mismatch only after checking that the uploaded file is the intended paper.

If confirmation reports that the preview file or Paper Master changed, the
previous decision is stale. Reopen Editor Upload and review the current bytes
and current Master record; no submission was created from the stale preview.

### Formatting upload asks for confirmation

Corrected PDF upload uses the same title safety component. A mismatch does not forbid saving, but it prevents accidental wrong-file upload by requiring confirmation. The ordinary word-level difference is shown first; character-level differences are available in an expandable detail.

### Single Paper Mode loses Next or returns to the first paper

Current versions create a stable Single Paper queue when the mode starts.
Saving Review OK stays on the same paper; Go next follows the original queue
instead of recalculating the Pending/Review OK sort. If an old bookmark contains
only `mode=single&submission=...`, reopen Single Paper Mode from the current
Formatting list. A message that the queue expired means its two-hour temporary
session snapshot is gone; the fallback link preserves the original filter and
search so a new queue can be started.

If the current queued submission was discarded, marked Not Publishing,
replaced, or otherwise removed from active publication scope, the page reports
that transition and offers the next valid queue item. It must not display
`Paper 0 of N`.

### Formatting Save says the record or publication file changed

The review page is stale. Formatting Save and corrected-PDF title confirmation
are bound to the exact publication PDF/source shown when the page was rendered.
Reload that paper, inspect the current preview/files, and save again. Do not
work around the warning by copying an old review status into the database.

The same stale-page rule applies to Final Submission Edit, Paper Master Edit,
Title/Author Review, Exceptions, and Process PDF formatting triage. Reload the
page and re-review the current values; do not retry with an old hidden token.

### Title/Author extraction is wrong

Use Title/Author Review:

- Mark Red Flag if the PDF formatting likely needs correction.
- Correct formatting and upload a corrected PDF/source if needed.
- Re-extract only the needed records when possible.
- If GROBID fallback is enabled in Settings, use single-row `Try GROBID` for rows where the built-in extractor misses authors or produces suspicious truncated output. The batch GROBID action only processes extraction errors and Red Flag rows. GROBID failures leave the existing extracted title/authors unchanged.
- If extraction remains wrong and the PDF/source should not be changed, use row-level `Manual override`. A reason is required, the override is audited, and Title/Author Review returns to Pending. Do not try to edit extracted title/authors from Final Submission edit.
- Review OK only after extracted title/authors and verification image are acceptable.

Verification image URLs include a file-modification cache buster so a newly generated built-in or GROBID image should replace the previous browser image immediately.

Built-in, GROBID, and Manual Override now use the same verification layout. The
header reuses only confirmed blank space above the PDF title and expands
upward for the remainder. It always keeps a small safety gap from the first
visible PDF content; a top logo, line, image, or text forces additional
expansion rather than being covered. Numbered authors in the header correspond to separate green
outline/underline regions in the PDF. If a single person appears as two
numbered entries or two adjacent boxes, the extracted author list was split
incorrectly even when all text was found.

### GROBID fallback cannot connect

GROBID is optional and disabled by default. If enabled, confirm the API URL in Settings points to a trusted local/internal service such as `http://localhost:8070` or your lab server. Settings shows a green/red health indicator using the GROBID `/api/isalive` endpoint; the indicator refreshes as you edit the URL or timeout, before the settings are saved. The Title/Author page also checks API health before any GROBID action. If the API is unavailable, GROBID buttons are disabled and batch extraction aborts before processing rows, so unavailable service does not create extraction errors for every paper. If the service drops during a batch, the batch stops at that point; completed rows remain saved and the current/unprocessed rows are counted as skipped. The app calls `/api/processHeaderDocument` for extraction and does not send PDFs to a cloud service unless you configure a cloud URL yourself. Connection, timeout, HTTP, or TEI parsing failures are logged and shown as messages, but they do not overwrite existing extracted title/authors.

### A paper disappeared from a filtered page

Check the page filter. Organized List defaults to All, but review pages can be filtered by status. Use All if you need to confirm whether the record still exists.

## Version And Publication Decisions

### Start2/Editor version decision needed

The same Paper ID has both an undiscarded Start2 version and an undiscarded Editor Upload. The editor upload is temporarily prioritized, but final export is blocked until one side is discarded with a note.

### Discard vs Not Publishing

Discard excludes one Final Submission version from active selection.

In Final Submission Edit, Discard is intentionally under the collapsed bottom `Version actions` danger zone and uses a separate form from `Save Final Submission`. If you mean to exclude the entire paper, leave Edit and use Not Publishing instead.

### Edit returned to the wrong page

Open Edit from the worklist button rather than manually rewriting the URL. Organized List (Checklist or Compact candidates), Formatting Review, Title/Author Review, Not Publishing, Verify Paper IDs, and Exceptions include a safe local return URL. External `next` URLs are rejected and fall back to Final Submissions.

## A Workflow Link Shows Several Similar Papers

Normal search fields use partial matching, so a search such as `58` may match
Final ID `58` and Paper ID `R058`. System-generated links from Final Submission
Edit and worklist details should instead open a `Focused ...` banner and one
exact record. If a current link still includes a prefilled `q=` solely to locate
one record, treat it as stale UI and report the source and destination pages.

A focused page may show `Outside review scope` for an inactive, discarded, Not
Publishing, or non-Master version. That message is safer than showing another
active record. Use the Edit link or return to the full worklist to resolve the
version state; do not change the URL to force an out-of-scope record into a
publication workflow.

Not Publishing excludes the paper from publication output because of an editorial decision such as unpaid, withdrawn, or not in the final publication scope.

Old Versions shows version history. Not Publishing List shows publication decisions.

### A Paper Master record has no Final and will not be published

Do not delete the Paper Master record and do not create a placeholder Final.
Open Not Publishing List, focus the Paper Master record, choose the editorial
reason, enter a note, review the scope-impact warning, and confirm. The decision
is stored on Paper Master, so it works before a Final exists and excludes the
paper from both final and draft packages.

If the paper later receives a Final Submission, the imported or Editor Upload
version inherits the Master decision and remains outside publication scope.
Undo Not Publishing only when the paper should return to the proceedings. If no
Final exists at that point, Missing Final Submission correctly returns as a
blocker.

### Publication Decision Required

This state means older data, manually inconsistent data, or a newly imported
Master ID matching an orphan Not Publishing Final could not be safely
classified. It blocks final, draft, and CrossCheck exports. Open Not Publishing
List and explicitly choose either Keep in publication scope or Mark Not
Publishing; the system must not guess.

### Publication Decision Integrity Conflict

Paper Master is authoritative, but all mapped Final decision mirrors must agree
with it. This Critical error means an import, restore, or legacy record left
them inconsistent. Final and draft force export cannot bypass it. Open Not
Publishing for the Paper ID and explicitly apply Keep Publishing or Not
Publishing so every mapped version is synchronized. Do not edit the Final
exclusion fields directly.

### ID cannot be verified

A Paper ID must exist in the Paper Master List and that Master paper must be in
Publishing state before it can be verified. Correct the ID, undo the Master
Not Publishing decision, or classify an orphan Final as Not Publishing when it
should not be published.

## Plagiarism / CrossCheck

### CrossCheck result import has no report

Reports are optional. The CSV imports `filename`, `plagiarism_percent`, and `single_percent`. Upload report PDFs separately if they exist.

### CrossCheck import reports stale batch/version

The token manifest points to a different Final ID or publication PDF SHA-256
than the current candidate. Do not rename or reuse the old result. Prepare a new
CrossCheck token for the current publication PDF, then import that result and
report.

### Percent values include `<1%`

The app treats `<1%` as 1 for score storage/display.

### Report link is missing

Upload report PDFs through the report upload workflow. The app matches reports by filename and then shows an open-report link.

### Plagiarism score is over threshold but allowed

Open Exceptions and approve `Plagiarism %` or `Single %` separately with a reason. A valid allowed exception moves the issue to Info and does not block final export. If the imported score changes later, the exception becomes stale and blocks export until it is re-approved or removed.

### Review image magnifier does not appear

The in-place magnifier is intentionally enabled only for a mouse or trackpad
that reports hover and a fine pointer. It does not appear on touch/coarse-pointer
devices. In Formatting list mode, expand the paper first. Wait for the
Formatting preview or Title/Author verification image to load, place the pointer
over it, and hold `Ctrl`. The lens closes when `Ctrl` is released or the browser
loses focus. The in-image `Hold Ctrl to magnify` hint should disappear
immediately while the lens is active; it is not a browser-native tooltip. Use
`Open Publication PDF` or click the full verification-image
link when the browser or device does not support the modifier-controlled lens.

## Exports

### Final publication package is blocked

Open Error Report and resolve Critical blockers, or approve valid exceptions in Exceptions.

Common blockers:

- Missing active final for a Paper Master record.
- Invalid or unverified Paper ID.
- Missing PDF/source, including a selected Corrected file that is no longer on disk.
- Formatting review Pending/Needs Edit.
- Source review hash missing after Review OK, or source bytes changed after
  Formatting Review.
- PDF not processed.
- Page count outside limits without allowed exception.
- Title/author review not OK.
- Formatting not Review OK.
- Missing plagiarism scores, or over-threshold plagiarism scores without valid allowed exceptions.
- Duplicate publication title/PDF/source.
- Duplicate publication filename after Paper ID/title sanitization.
- Unresolved duplicate author.
- Start2/Editor conflict.

If export reports that a file changed during inspection, do not use an older
generated package. Confirm that no external synchronization or editor is
replacing files, run Process PDFs again, clear all resulting blockers, and
export a new final package.

Before Formatting Review is complete, Error Report should show only
`Formatting Not Review OK`; an empty source review hash is expected at that
stage. For `Source Review Hash Missing` on a record already marked Review OK, or
for `Source Changed After Review`, inspect the current source in Formatting
Review and save `Review OK` again. Do not copy a hash from another record or
restore an older status directly in the database.

### Error Report count does not match the visible page

Select the relevant `Critical`, `Medium`, or `Info` severity tab. Severity is a
server-side filter and pagination applies to that filtered result, so the
worklist should show `1-25 of N` for the selected severity. `All` intentionally
sorts all issues by severity. Workflow-area links can be combined with severity
tabs. Category pills are also server-side filters: several selected categories
match with OR, then combine with the current workflow area and severity using
AND. Their counts reflect the current severity before category filtering. If
the result is unexpectedly empty, inspect the selected category pills or use
`Clear categories`. If the selected severity/category result reports a nonzero
count but an empty first page, record the URL and app version; that indicates a
pagination regression rather than missing readiness data.

### Need an intermediate package anyway

Use Download Draft Package Anyway from Export Reports after reviewing the warning. The draft ZIP may skip missing files and includes a warnings CSV. Do not treat it as final-ready.

### Excel export fails

Run:

```bash
.venv/bin/python manage.py check
```

If the error mentions dates, inspect uploaded `upload_date` values and re-import with valid dates. If the export still fails, use the Error Report text and recent import preview to identify the row with invalid data.

## Storage And Backup

### Need to trace who changed what

Open `/reports/audit-log/`. Search by Paper ID, Final ID, action, status, or message. The raw file is `data/logs/audit.log`, and the page can download it.

Useful actions to filter or search for include `paper_master_import_apply`,
`final_submission_import_apply`, `final_submission_edit`, `pdf_process`,
`formatting_review_update`, `editor_upload_apply`,
`final_submission_discard`, `paper_id_verify`, `crosscheck_result_import`,
`publication_package_export`, `system_state_export`,
`system_state_restore_apply`, `storage_cleanup_apply`, and
`database_clear_complete`.

### Audit log is missing or empty

The log file is created the next time an audited action runs. If Clear Database was run with `Also archive and clear audit log`, check `data/logs/archive/` for the previous log.

If the whole `data/logs/` folder is missing after moving machines, restore from a System State ZIP made with a current app version. Current snapshots include both active and archived logs.

### Audit log is large

The Audit Log page shows the latest events and search results without loading the whole file into the table. Download the raw log if you need a full external review. Do not manually edit `audit.log`; archive it through Clear Database only when starting a fresh environment.

### Generated reports are taking space

Use Settings > Storage Management > generated reports/exports cleanup. It removes regenerated Excel/ZIP downloads and external upload packages.

It does not remove original uploads, corrected files, plagiarism report PDFs, System State backups, or thumbnails/previews still referenced by the database.

Conservative cleanup selects only unreferenced generated cache. It does not select publication debug, legacy active-final, or old-version output folders. Review the preview before Apply and stop if any candidate looks like a real upload or review artifact.

### Storage Management keeps showing Loading

Settings loads Storage Management separately so a large or slow Docker bind
mount cannot block the editable form. Wait for the panel request to finish or
use Refresh. If it repeatedly fails, check the global request error shown by
the page, confirm the configured folders are readable inside the container,
and compare the container paths with the mounted data directory. The scan reads
metadata but does not change files. Cleanup still requires a preview and typed
confirmation.

### Thumbnails or previews are missing

Run Process PDFs for page thumbnails. Run Title/Author Review extraction for verification images. Referenced thumbnails and previews are not removed by conservative cleanup.

If they are missing after a System State restore, the ZIP may have been created by an older app version that did not include all review artifacts. Use a fresh System State ZIP from the original machine when possible; otherwise regenerate the missing artifacts and re-check the affected reviews before final export.

### System State restore says unsupported version

The ZIP was created by a different state archive version. Use the matching app version shown in the footer and in the ZIP manifest.

### Restored paths point to the wrong computer

System State restore should remap managed files into this project's `data/` tree. If a path still points to another machine or a temp folder, do not continue publishing from that state. Export a fresh System State ZIP from the original machine and restore again.

### Docker instance problem

Use the [Docker Guide](docker_guide.md) for the current commands and ownership
model. Start with these checks:

```bash
docker compose ps
python3 scripts/update_docker_instances.py --dry-run
```

Confirm that:

- the command is running from the checkout that owns the Compose project;
- Docker Desktop is running;
- `web` is healthy and `proxy` owns the public port;
- both services use the same `sms_data` source, with the proxy mount read-only;
- every maintained environment file has a unique
  `COMPOSE_PROJECT_NAME`, endpoint, and `SMS_DATA_DIR`.

For a maintained environment file, apply changes with
`scripts/update_docker_instances.py`. A new environment file is not started
without `--create-missing`. Use `scripts/rebuild_docker_instances.py` only
when recovering a running legacy instance whose maintained environment file is
unavailable.

If static or media files disappear with `SMS_DEBUG=0`, do not re-enable debug
as a workaround. Verify the proxy's `sms_static` and read-only `sms_data`
mounts and rebuild through the updater.

If POST actions return CSRF 403 while GET pages load, confirm Nginx forwards
`Host $http_host`, including the browser-visible non-default port. Do not use
`csrf_exempt`, broad trusted origins, or a changed `SMS_ALLOWED_HOSTS` value
as a substitute for correct proxy headers.

A fallback page with a named backup, migration, update, or restart phase should
be allowed to finish. A generic unavailable page means no fresh operation
status explains the outage. The fallback returns to a new Dashboard GET after
readiness succeeds; verify any interrupted POST, upload, import, or export
before repeating it.

Update, rebuild, migration, and raw backup share
`runtime/.docker-data-operation.lock`. Do not remove an active lock. Locks
older than 12 hours are treated as stale. Preserve both the host mirror and any
`.backup-swap` directory when both exist, then inspect
`.sms-docker-backup-history.jsonl` before retrying.

To run temporarily from the verified host mirror, use the bind-rollback
procedure in the [Docker Guide](docker_guide.md#roll-back-to-the-host-mirror).
Never add `-v` to `docker compose down`.

### Need a completely clean conference

Download a System State ZIP first if the current work must be preserved. Then use Settings > Clear Database. This wipes database records and managed files so the app starts a new conference environment.

Clear Database preserves `data/logs/audit.log` by default. Check `Also archive and clear audit log` only if the new environment should start with a fresh log; the old log will be moved to `data/logs/archive/`.

Configured folders outside the application `data` and media roots are reported
but preserved. This prevents a shared absolute Reports, extraction, plagiarism,
incoming, or output folder from being recursively erased. Remove an external
folder manually only after confirming that every file in it belongs to the
conference.
