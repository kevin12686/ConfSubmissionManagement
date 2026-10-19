# Editorial Acceptance Runbook

## Shared PDF Reader Acceptance

Use disposable data, never mutate the operator's live conference for browser QA.

1. Open PDFs from Organized List (both views/details), Final Submissions, edit,
   Formatting (List/Single/Focus), Title/Author, Paper ID, Author Count,
   exceptions/Error Report, Old Versions, history and Editor Upload preview.
   Confirm the common reader opens in the requested tab and leaves forms alone.
2. Confirm each reader's source URL is the original endpoint. Original,
   Corrected, report, debug, inactive/discarded row, exact revision and comparison
   Before/After must not be confused or replaced by the active paper's PDF.
3. Confirm the reader fills the viewport without site Navbar/footer or outer
   scrolling, while ordinary work pages keep their normal layout. Test actual
   nonblank canvas rendering, text selection/search, next/previous, page input,
   fit/zoom, rotation, password entry, 320px/desktop and short/landscape windows.
   Resize after loading and verify the PDF fills the space beneath the wrapping
   toolbar. Runtime/data/font requests must stay local; no editing/save UI may appear.
4. Test missing Corrected, corrupt PDF, missing/corrupt history, stale comparison
   and missing static modules. Show a readable error and raw direct/download
   links; never silently pick another file. Without JavaScript the raw actions
   must remain available.
5. Compare DB signatures, review flags, readiness and SHA256 bytes before/after
   reading. Publication ZIP and CrossCheck ZIP must still use the current
   Corrected/Original helper bytes, not rendered, debug or historical output.
6. Confirm source/ZIP downloads, thumbnails, verification images, magnifiers,
   comparison highlights and lazy/HTMX fragments retain their prior behavior.
7. For Docker, validate Nginx configuration and module/worker `.mjs` Content-Type
   through the static location; ordinary CSS/WASM MIME types must remain intact.
8. Reopen a multi-page PDF and confirm Fit page + Single page defaults, one
   displayed page, and wheel navigation that turns only one page per gesture.
   Verify a long stream of small trackpad deltas cannot keep flipping, then a
   new gesture can turn again. Zoom in: native scrolling must reach the edge,
   and only a fresh gesture there may flip. Going back enters the prior page's
   bottom; first/last-page bounds, horizontal/modified wheel and toolbar input
   must remain intact. Switch to Continuous
   scrolling and back at a middle/last page without losing page or zoom. Test
   next/previous, page input, cross-page search, internal PDF destinations and
   panning after zooming in; do not clip off-page content to simulate no scrolling.
   On mixed portrait/landscape documents, Fit page/Fit width must refit the
   newly selected page, while an explicit numeric zoom remains unchanged.
9. In the PDF area, test Left/Right, Up/Down, Page Up/Down and Home/End in both
   layouts. Zoomed pages must pan first; a held vertical key reaching the edge
   must not turn until released and pressed again. Held page-turn keys cannot
   keep skipping pages. Verify backward-bottom/forward-top entry and first/last
   limits. Mix wheel, key, toolbar, page input, search and internal links; pending
   positioning must not move a different page or layout. Inputs, selects, buttons,
   password entry, IME, modified keys and text selection must remain native.
   Focus loss/window blur must release held keys; loading must not steal an input
   the user focused before the PDF became ready.

10. Open a long, mixed-orientation PDF. Desktop starts with the page sidebar open;
    narrow screens start closed. Check all page numbers, lazy nonblank thumbnail
    canvases, independent sidebar scrolling, same-document jumps and current-page
    markers following wheel, key, page input and search navigation. Rotate and
    reopen quickly: no stale thumbnail may survive canceled rendering. Test
    sidebar keyboard focus/Enter/Space/Escape and mobile selection auto-close.
    Main PDF width, zoom preservation, normal site pages and source/download
    URLs must remain unchanged. A thumbnail failure must not block the main PDF;
    closing/reopening retries. Compare DB signatures and selected/revision file
    bytes before and after; thumbnails must not create managed media files.

This runbook is for manual end-to-end validation before using the system for a real publication handoff. The standard is strict: final publication export should be clean only when readiness issues are resolved or explicitly allowed as exceptions.

Use a disposable conference data folder. Complete the automated command
checklist first, then work through each stage in order. Expected publication
behavior is defined in [Publication Rules](publication_rules.md); shared
responsive and worklist behavior is defined in
[UI Conventions](ui_conventions.md). Docker procedures are centralized in the
[Docker Guide](docker_guide.md).

## Run Order

1. Run the automated gate below.
2. Create the disposable dataset.
3. Complete Manual Stages 1-8 in order.
4. Confirm the grouped acceptance checks before handoff.

### Automated Gate

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py scripts
```

The Docker-specific manual checks use disposable instances and the procedures
in the [Docker Guide](docker_guide.md).

## Dummy Conference Dataset

Create these Paper Master records:

| Paper ID | Title | Authors | Expected outcome |
| --- | --- | --- | --- |
| P001 | Ready Paper | Ada Lovelace; Alan Turing | Publishes successfully |
| P002 | Revised Version Paper | Grace Hopper | Newer Start2 final becomes current |
| P003 | Mapping Problem Paper | Katherine Johnson | Blocks until Paper ID is corrected and verified |
| P004 | Unpaid Paper | Barbara Liskov | Mark Not Publishing |
| P005 | Missing Source Paper | Donald Knuth | Blocks until source file is uploaded |
| P006 | Similarity Review Paper | Edsger Dijkstra | Blocks until plagiarism scores are acceptable or handled |
| P007 | Email Replacement Paper | Frances Allen | Editor Upload creates a conflict until Start2 or editor version is discarded |
| P008 | Approved Exception Paper | Leslie Lamport | Page or author-limit issue is allowed with an exception note |
| P009 | Duplicate Author Paper | Chih-Wei Hsu; Chih Wei Hsu | Duplicate-author warning requires review |

Create Final Submission records and files:

| Final ID | Paper ID entered by author | Scenario |
| --- | --- | --- |
| 10 | P001 | Clean PDF/source, all reviews complete |
| 20 | P002 | Older version, should become replaced |
| 21 | P002 | Newer version, should become current |
| 30 | WRONG | Mapping error until editor fixes it |
| 40 | P004 | Unpaid or withdrawn; mark Not Publishing |
| 50 | P005 | PDF exists, source missing |
| 60 | P006 | Plagiarism score over threshold |
| 70 | P007 | Start2 version exists |
| EDITOR-P007-001 | P007 | Editor Upload from email, conflict until one version is discarded |
| 80 | P008 | Page count or author number outside configured limit |
| 90 | P009 | Same normalized author appears twice |

## Manual Workflow Checklist

### Stage 1: Setup And Import

1. Configure Settings, including conference name, page limits, author limits, plagiarism thresholds, timezone, folders, and active-version rule. Confirm the editable form appears without waiting for Storage Management or GROBID, then confirm the storage panel and health status fill in separately. Refresh Storage Management and verify its counts remain stable without changing any records. Create a cleanup preview, replace one candidate at the same path, and confirm Apply skips it as changed; an overlapping Reports folder must not select System State or import/restore preview files.
2. Import the Paper Master List and verify preview sorting places changed/new rows above unchanged rows.
3. Import Final Submission metadata and upload matching PDF/source files. Confirm preview sorting places mapping/file/reset issues above unchanged rows.
4. Use `Add Final Submission` to create one record manually. Confirm matching Paper Master title evaluation, uploaded PDF/source paths, Pending processing/review state, active/replaced version recalculation, and a `final_submission_create` audit event. Submit an invalid form and confirm no record is created.

### Stage 2: Scope And Version Decisions

5. Open Dashboard and confirm its blocking check count matches the current final-package readiness rows. Only workflows with blockers should appear under Next actions; clear workflows should appear under No current blockers. Confirm each action total and breakdown count unique affected papers from those rows, its link opens only the represented blocking categories, valid Allowed exceptions remain under Tracked information, and a stale exception returns to Next actions.
6. Open Verify Paper IDs. Correct P003 and verify only after it maps to a valid Paper Master record.
7. Mark P004 as Not Publishing and confirm it moves out of publication blockers while remaining visible in the Not Publishing List.
8. Create the Editor Upload for P007. Trigger a title mismatch and confirm the title safety check shows the uploaded PDF title above vertically stacked Paper Master and Final comparisons, combines identical references, and provides word-level plus expandable character differences. At 320, 768, 1024, and 1440 pixel widths, confirm long titles wrap without horizontal page overflow. Open the temporary PDF, then test replacing and canceling a preview without creating a submission. Finally confirm a mismatched upload and verify it remains unverified and the Start2/Editor conflict warning appears.
   In a disposable data folder, change the preview PDF bytes and separately edit
   the Paper Master after preview; both confirmations must fail without creating
   an Editor Upload.
9. Discard either the Start2 or Editor Upload version for P007 with a required note. Confirm the conflict clears.

### Stage 3: PDF, Metadata, And Formatting Review

10. Run Process PDFs. Confirm only current Paper Master publication candidates are processed; discarded, Not Publishing, invalid-ID, and historical versions must not create processing errors. Confirm page counts, hashes, thumbnails, and publication PDF debug copies are generated. Confirm all matching thumbnail strips remain expanded. Exercise `Needs processing`, `Page issues`, `Processed`, and `All`; use paper jump; verify sticky paper identity, fixed-size lazy thumbnails, page labels, enlarged preview, and failure tile. Record one general formatting issue and one page-specific issue from the enlarged preview. Confirm both append to the same Formatting notes, set Needs edit, clear a prior Review OK binding, leave PDF/processing/Title-Author/plagiarism state unchanged, and create audit events.
11. Run Title/Author Review for needs-review records. Review extracted title/authors, title differences, red flags, and verification images together. Generate one Built-in, one GROBID, and one Manual Override image and confirm all three use the same layout, differing only in their source label. Test one PDF with substantial white space above its title and confirm the header reuses that space, then test one with a top logo/text and confirm the image expands instead of covering it. Use a long title and a long filename to confirm the header wraps with small margins and never reaches the PDF title/authors. Include a title ending in mathematical star `⋆` and confirm the exact character appears through the bundled fallback without overlapping the preceding title text. Use adjacent or intentionally split author names to confirm every parsed author has a separate numbered legend entry and boundary. Confirm author evidence is case-sensitive and requires matching internal punctuation: `Jean Luc` must not mark `Jean-Luc`. Test a surname followed by `*`, a Unicode superscript, merged numeric affiliation, an ORCID ending in `X`, and list punctuation; when trailing content is absent from the extraction, confirm only the extracted name is highlighted and numeric/symbolic metadata remains outside the green boundary. Confirm `John Smith` in `John Smithson` outlines only `John Smith` in orange, including for a single-word prefix case such as `Li` in `Linear`. Put an exact `Li` elsewhere on the same page and confirm the exact green match suppresses the orange `Li` in `Linear`. Confirm each `A1...AN` legend entry is green, orange, or red to match its selected evidence state. Repeat with a marker present in the extracted name and confirm it is included. Hold `Ctrl` over the verification image and confirm the shared magnifier exposes those boundaries without interfering with the normal full-image link. Change the filter and move to another page through the partial worklist navigation, then confirm every newly displayed verification image still opens the magnifier without a full browser refresh. Confirm that Review OK is the only completion action and no second title-match confirmation appears. If GROBID fallback is enabled, test it only on suspicious rows and confirm successful GROBID output still returns to Pending review. For one difficult paper, test Manual override with a reason and confirm it is visibly marked, audited, and still requires Review OK.
    For exact title evidence, remove a trailing symbol such as `⋆` from the
    extracted title and confirm the PDF-only symbol remains outside the yellow
    boundary; restore the same symbol to the extraction and confirm it is
    included.
12. Open Formatting Review. Confirm list mode is a compact worklist and only one
    selected paper remains expanded. Change tabs and search, verify the worklist
    updates and URL history changes without losing the page shell, then disable
    JavaScript or open the filter URL directly to confirm the ordinary GET
    fallback. In List, Single Paper, and Focus modes, point near the center and
    all four edges of the first-page preview, then hold `Ctrl`. Confirm the
    `3:2` landscape lens follows the pointer, remains inside the image, closes
    immediately when `Ctrl` is released or the window loses focus, and does not
    retain the `Hold Ctrl to magnify` hint over the enlarged content. Confirm
    the shared in-image hint returns after closing without the delay of a native
    browser tooltip and does not alter any formatting or
    publication state. Change Formatting filters and pages before repeating the
    magnifier check; each newly swapped preview must work without expanding a
    different card or refreshing the browser. Confirm touch/coarse-pointer
    layouts retain the static preview and `Open Publication PDF`. Start Single
    Paper Mode from a filtered/search result and record
    its first two Paper IDs. Mark the first Review OK and Save: it must stay on
    the same paper, preserve the filter/search, and Go next must still point to
    the recorded second paper. Previous/Next must use natural Paper ID order.
    Confirm Single mode has no numbered worklist paginator. Open Formatting from
    Final Submission Edit and confirm the exact Focus mode shows only that paper,
    no queue navigation, and offers `Start Single Paper Mode here`.
    Upload a corrected PDF/source, confirm the vertically stacked title safety
    component, cancel once to verify the temporary upload is removed, then save.
    Replace the publication file from another request before one Save and before
    one title-guard confirmation; both stale actions must be rejected without
    changing formatting status/files. Invalid or duplicate-kind uploads must
    retain entered status/notes and require files to be selected again.

### Stage 4: CrossCheck, Counts, Exceptions, And Navigation

For automatic CrossCheck, use a mocked service or an explicitly authorized test
folder with one full-text paper. Confirm Corrected/Original input bytes match
manual/final export selection, `PaperID.pdf` filenames, separate conference
postfix, and sequential daily tokens. Default IEEE credentials must be omitted.
Exercise 409, uncertain POST recovery without re-upload, partial/unconfirmed
results, Unprocessed fallback, malformed/foreign manifest, changed active Final,
changed PDF and changed score after preview. Apply only after confirmation and
check P/S mirror, report bytes, exception staleness, unchanged review flags and
rollback on DB/audit failure. Save/restore a v7 archive and verify jobs/items,
reports and evidence remain usable with no secret in the ZIP. Clear Database
must remove jobs/items/evidence/local secrets. GET/filter actions are read-only.

Begin connection setup with a blank URL and verify no request is sent. Enter a
test service requiring WebLogin, supply unsaved credentials and load folders;
test wrong/missing credentials as well. Save credentials without leaving the
page or saving/enabling other Settings. Change the address/login while a check
is pending and confirm the obsolete response is ignored. Saved credentials must
not cross service URLs; environment overrides still win. Verify passwords are
absent from responses, audit and backups, and existing saved addresses survive
the blank-default migration.

13. Export CrossCheck/plagiarism PDFs with a token, import result CSV with Plagiarism % and Single %, and upload optional report PDFs.
14. Open Author Count. Confirm publication paper count is per normalized author, duplicate-author warnings are reviewable, and name/Paper ID search plus attention filters do not change counts.
15. Open Exceptions. Search/filter to P008, approve it only with a note, and confirm allowed page/author/plagiarism exceptions move to Info and do not block final export. Change an approved plagiarism score and confirm the exception becomes stale and blocks export again. In Organized List, type reasons into two exception sections for the same paper, submit only one section, and confirm the complete paper row refreshes while the other unsaved reason remains. Confirm only the clicked exception was stored, the panel stays open, and a stale evidence warning reloads the current backend value without applying the old decision.
16. Confirm Final Submissions opens with submission tabs/table first and `Import / Re-upload` collapsed. Expand it, drop/select metadata plus PDF/source files, verify counts/type summary/removal, and confirm preview-before-apply still controls storage. Open Final Submission Edit from Organized List, Title/Author Review, Formatting Review, Not Publishing, Verify Paper IDs, and Exceptions. Change one value, confirm only that field is marked, revert it and confirm the marker clears. Submit another change and verify the Review Changes page shows exact current/proposed values and reset impact without changing the database. Confirm and verify Save returns to the same worklist/view/filter/search/tab. Select a byte-identical PDF and confirm no review resets; select different bytes and confirm PDF-dependent resets are previewed before apply. Confirm the edit order is Submission identity, Metadata, Current row files, Plagiarism data/report, Workflow status summary, and Review changes. Confirm version discard is a separate form under the collapsed bottom `Version actions` danger zone and still requires a reason. Then open Organized List, switch Checklist/Compact candidates, and confirm both show the same active publication scope.
    Re-import an existing Final ID with the same Author-entered ID but a changed
    Final Title that resembles another Master paper. Confirm Preview preserves
    the current Official Paper ID and resets Paper ID review without remapping.
    Re-import again with a changed Author-entered ID and confirm Official ID is
    re-resolved. Change Final Authors only and confirm Title/Author Review
    returns to Pending while existing extracted metadata remains visible.
    Upload a legacy workbook containing `Mapping Table` and confirm Apply is
    blocked without creating or updating records.
17. From one Final Submission Edit page, open Paper ID Review, Process PDFs,
    Title/Author Review, Formatting Review, Not Publishing, and Organized List.
    Confirm each destination shows the shared focused-record banner and only the
    intended Final/Paper record. Create a collision such as Final ID `58` plus
    Paper ID `R058`; exact links must not show both, while manually searching
    `58` may. Confirm an inactive or excluded target shows an outside-scope
    explanation and that opening any focused GET leaves all review and active
    flags unchanged.
18. Exercise Author Count search/filter/sort, Exceptions status/type/search, Title/Author grouped views, Verify Paper ID filters, Final Submission tabs, and Process PDF filters. Confirm URLs can be refreshed/shared and normal GET navigation still works without JavaScript.
19. Exercise Paper Master List and Final Submission sorting before and after search. Confirm natural ID order (`P2` before `P10`, Final `2` before `10`), pagination preserves the selected sort, Final Submission tabs retain it, and Old Versions tabs match the shared worklist tab design.

### Formatting Revision Acceptance

In a disposable conference, upload a corrected PDF and then source only; open
Revision history in Formatting List/Single/Focus and Final Submission edit.
Verify exact independent file pairs, notes/time, newest-first order, local
top/bottom pagination, retry feedback, and narrow-screen wrapping. Confirm
history navigation leaves an unsaved Formatting form and its main URL intact.
Capture existing corrected files twice and confirm one baseline and unchanged
review state. Invalidate the originals and discard the row; history remains
viewable/downloadable without selecting it for publication. Remove a history
file and verify its download fails without falling back. Inject copy, DB, and
audit failures and verify no partial upload or revision is applied.

Compare final/CrossCheck PDF bytes before and after baseline/history reads.
Cleanup must retain history even when Reports overlaps media. Verify v6
backup/restore round-trips revision hashes, links, and review flags; validate
a v5 restore with empty history, reject a v6 missing its revision payload, and
verify Clear Database and failed restore rollback cover historical files.

Automated edge coverage in `submissions/tests/test_formatting_history.py` also
checks all four Original/Corrected PDF/source combinations against final ZIP
and both CrossCheck scopes. A missing selected corrected file must never fall
back to an original, history, legacy path, or debug copy. Draft skips the paper
with an incomplete pair; CrossCheck requires the selected PDF, not a source.
Exercise a real multi-page PDF re-import followed by Process PDFs and compare
the exported bytes with the new original, not the most recent revision.

Verify that a second editor's stale save cannot add a revision, and an upload
interleaved with export aborts the export without retaining a mixed package.
History-only baseline capture must not invalidate a stable publication read.
Inject a failure after copying the PDF but before copying the source, and a
same-size/same-mtime mutation during capture; both must roll back completely.
Confirm source-only and PDF replacement keep their existing distinct reset
rules. Editor/Start2 conflicts, discard, multiple-active ambiguity, and Not
Publishing must retain their existing export behavior even when history exists.

### Formatting PDF Comparison Acceptance

Upload a corrected PDF in List/Single/Focus and verify it is saved before a
separate automatic Original comparison POST starts. Confirm title guard warnings
first; preview/cancel/validation failure must create no comparison receipt.
Test PDF-in-source-field recognition, source-only and notes/status saves,
filtered-out saved rows, loading, Warning/Info counts, and explicit Retry.
Keep the saved card/Single queue position, including after HTMX filter changes.
Refresh a completed result: no second build. Upload A then B (also with identical
bytes): A's request/result must never open B as its After side. A later
source-only revision may reuse a comparison of the same PDF. Interrupt/duplicate
a request and test missing Original, parse, cache-write and audit failures:
saved files/revisions/flags must not be rolled back by comparison failure.
Verify responsive status strips, no auto-navigation/polling, and normal backup,
cleanup protection and Clear Database coverage of automatic request caches.

Use disposable real PDFs: a corrected Word reference error, removed/moved
paragraph, changed punctuation/numbers, textless document, rotated page, and
visually missing table. Open Compare PDF from List/Single/Focus while leaving
an unsaved Formatting note; the note/input must survive in the original tab.
Build Original and revision baselines, navigate/filter findings, switch text
and page views, browse all pages, and verify shared loading, Ctrl magnification,
responsive stacking, and exact snapshot PDF links. Insert a cover page to prove
page pairing follows text rather than page numbers. No automatic table/image
completeness verdict is permitted.

Reflow a two-page document while preserving its second-page header: expect
one visible same-text record at page 2/2, not separate removal/addition. A real
title deletion must remain a separate content finding. Then change/remove the
header, edit footer years/table numbers, remove one repeated sentence, change a
suffix/punctuation beside a moved passage, and introduce a footer reference
error; none may be excluded. Cover multi-page findings and capped highlights:
all actual pages remain selectable, and a missing counterpart says no
corresponding text while labeling its reference page separately. An older
method link requires explicit rebuild. Pairing-budget exhaustion preserves
unmatched findings with a notice rather than a complete/no-differences result.

Reject other submissions' revisions, corrupt/missing PDFs, invalid/expired
tokens, ambiguous active records, changed scope, and changed files during or
after build. Test processing limits, same-size file mutation, signed-payload
corruption, and failed-audit cleanup. Compare existing records, flags, readiness,
and final/CrossCheck PDF bytes before and after comparison; all must remain
unchanged. State ZIP excludes comparison staging while retaining history/review
artifacts. Ordinary cleanup preserves staging; Clear Database removes it.

### Stage 5: Publication Export

20. Export a draft publication package while blockers exist and confirm the warnings CSV lists skipped and risky items.
21. Resolve all blockers and export the final publication package. Request it
    with gzip accepted and confirm the response remains `application/zip`, has
    `Content-Length`, has no `Content-Encoding: gzip`, and opens normally.

### Stage 6: Audit, Backup, And Recovery

22. Open Audit Log and confirm recent import, Process PDFs, review, CrossCheck, exception, and export actions are searchable by Paper ID or Final ID.
23. Download a System State ZIP, clear the database/files in a test environment, restore the ZIP, and confirm state/files and audit logs return. Compare archived automatic creation/update times, including revision dates and CrossCheck batch times; an expired CrossCheck upload preview must remain expired after restore. Point Reports temporarily at a shared external test folder and confirm Clear Database preserves it. Simulate a database-reset failure and confirm staged publication files are restored with the records.

### Stage 7: Responsive And Accessibility Pass

24. At 390px and desktop width, confirm the page itself does not overflow, tables scroll inside their containers, 15px table/body text and 12px badges remain readable, buttons fit their labels, focus is visible, modal/collapse controls remain keyboard-operable, and repeated POST clicks do not submit twice. Confirm the two-level application header keeps the current conference visible, the workflow navigation collapses below 1200px, and active/hover/dropdown states remain readable.

### Stage 8: Docker Proxy And Recovery

25. In a disposable Compose project with `SMS_DEBUG=0`, confirm Dashboard CSS,
    local icons, one title/author verification image, one PDF thumbnail, one
    formatting preview, and one publication PDF all load through the published
    Nginx endpoint. Confirm `web` has no published host port, `proxy` owns the
    configured port, and proxy mounts `sms_data` and `sms_static` read-only.
    Stop only web and confirm Nginx displays the themed generic fallback, then
    start web and confirm two successful readiness checks automatically replace
    the fallback with the Dashboard. Confirm this navigation does not reload
    the failed URL or resubmit a POST. Publish fresh backup/update status and
    confirm the same shell changes its operation label and phase.
    On a non-default published port, submit a normal CSRF-protected POST such
    as a blocked Final Publication Package request and confirm it reaches the
    application instead of returning 403. Repeat with `SMS_DEBUG=1`; file
    availability and CSRF behavior must be unchanged.
26. Create two disposable conference env files with unique
    `COMPOSE_PROJECT_NAME`, port, and data folder values. Run the unified updater
    with `--dry-run`; confirm it reports env changes and does not create the
    missing project. Confirm duplicate project names, overlapping ports, shared
    data folders, and an existing project's changed `SMS_DATA_DIR` are rejected
    before any update. Use `--create-missing`, then change one web setting and
    one proxy setting and apply another update. Confirm it builds first,
    force-recreates web, recreates proxy only for the proxy change, retains the
    data mount type, waits for readiness, verifies the loaded Nginx Host
    directive, loads a collected static asset, and completes the non-mutating
    CSRF POST smoke check. Also run the legacy rebuild `--dry-run` against a
    single-service fixture and confirm it recovers the live port/settings rather
    than applying an env file. Run raw backup and verify `sms_data` is mirrored
    while rebuildable `sms_static` and `sms_gateway_state` are excluded.
27. Through the Nginx endpoint, download a large generated ZIP with a
    deliberately slow client. Confirm another browser can still load the
    Dashboard, the completed ZIP opens and matches the expected manifest/files,
    and no `proxy_cache` is configured. During transfer, confirm Nginx may use
    request-scoped proxy temporary storage and releases it afterward. Stop web
    before starting a request and confirm the gateway fallback still appears;
    do not expect fallback to replace a download after response bytes have
    already started.

## Acceptance Checks

### Data And State Safety

- Import preview never mutates records or files before Apply.
- Dashboard and Final Publication Package export use the same readiness blockers; Dashboard must never show Ready when strict final export is blocked.
- Dashboard paper counts use active publication papers and do not count inactive old versions or count one paper twice merely because it has multiple findings.
- Re-uploaded PDFs/sources reset only dependent review/check flags.

### Publication Selection And Processing

- Corrected PDFs are first priority for publication-facing links, CrossCheck export, duplicate checks, and publication packages.
- If no corrected PDF exists, the original active-submission PDF is the publication-facing PDF source.
- Process PDFs recalculates active versions, page counts, hashes, thumbnails,
  the compatibility author cache, and debug copies, but it must not rewrite
  original uploads, corrected uploads, extracted data, plagiarism scores, or
  review flags. A stale concurrent batch must not overwrite a newer thumbnail
  directory.
- Editor Uploads are active over Start2 until the conflict is resolved, but unresolved conflicts block final publication export.
- Discarded versions remain traceable and appear as old versions, not current publication candidates.
- Paper Master owns Not Publishing, including before a Final exists. Such
  records remain traceable but are excluded from publication processing,
  readiness, CrossCheck, and final or draft packages.
- Not Publishing and invalid-ID records are excluded from Title/Author and Formatting review queues; invalid-ID records remain visible in mapping/readiness workflows.
- Final Submission Edit cannot directly change processing, Title/Author Review, duplicate-author review, or Not Publishing state; those states are owned by their dedicated workflows.

### UI And Worklists

- UI navigation, partial GET updates, display filters, thumbnail previews, and layout changes must not alter publication PDF/source priority, active-version selection, readiness categories, review reset behavior, publication ZIP contents, or audit requirements.
- Expand an Audit Log JSON record and inspect inline path/action code on Settings and Integration pages; monospace content must use dark text on a muted light surface and remain readable.
- UI-only GET requests must leave publication ZIP entry names, PDF/source SHA256 values, manifest rows, and readiness blocker categories byte-for-byte/logically unchanged.
- Empty or deliberately stale `PaperAuthor` compatibility rows must not change
  Author Count, `Author Over Limit`, or final package blocking.
- Django admin must show publication-critical models as read-only.
- Old Versions classifies inactive records as Replaced, Discarded, or Other inactive; Not Publishing appears only as a secondary flag.
- Error Report separates Critical, Medium, and Info items. With more than 25
  issues in one severity, select that severity and confirm its first page shows
  `1-25` of the severity total rather than an empty client-side tab. Combine a
  workflow-area link with a severity tab and confirm both filters remain active.
- Select two Error Report category pills and confirm the result includes either
  category, excludes unselected categories, and paginates the complete filtered
  result. Change severity and confirm it combines with the category selection;
  use `Clear categories` and confirm area/severity context remains intact.
- Open an exception-capable Critical finding from Error Report and expand
  `Manage exception`. Confirm the panel uses the full table width, shows the
  current value/limit and relevant PDF/report links, and does not appear on an
  unrelated finding. Approve with a reason and confirm the complete worklist
  refreshes, the blocker is gone, and the record appears as an Info allowed
  exception. Remove it from Info and confirm the blocker returns. Repeat with
  JavaScript disabled to confirm the POST/redirect fallback works. Change the
  underlying value before submitting an old panel and confirm signed evidence
  rejects the stale action; also confirm a Final Submission-scoped key cannot
  be applied to another Final Submission.
- Every paginated worklist shows the same page-size/page controls above and
  below its rows. Use the bottom control to change page and confirm the next
  view returns to the top of that worklist with search/filter/sort state intact.
- Error Report duplicate rows show a compact matching-record count; opening
  `Show matching records` must return the complete duplicate group without
  changing any database or file state. Verify this on a numbered page and with
  Page size `All`.

### Readiness, Exceptions, And Export Integrity

- Allowed exceptions do not block final export while their approved value still matches the current value.
- After loading Dashboard readiness once, replace a publication PDF outside
  the app and confirm readiness and Final Publication Package both require PDF
  processing again. A warmed content-hash cache must never hide the change.
- Replace a publication PDF/source after readiness inspection but before ZIP
  entry reading and confirm final export fails, records a failed audit event,
  and leaves no partial ZIP/manifest.
- Create two distinct Paper IDs/titles that sanitize to the same publication
  base filename. Confirm Error Report and Organized List both flag the
  collision and final export does not create a ZIP.
- Draft publication package is clearly marked and contains a warnings CSV.
- Trigger a structural Draft blocker such as two active finals for one Paper ID.
  Confirm Draft export fails closed and shows the full blocker table instead of
  only a transient message.
- Final publication package contains one PDF/source pair per publishable Paper Master record and no replaced, discarded, or Not Publishing records.
- Final publication package file bytes match the current publication-facing PDF/source priority for each active publishable Paper Master record: Corrected PDF/source first, then Original PDF/source.
- Formatting Review `Review OK` stores the current publication source hash.
  A Pending/Needs Edit record with no source hash reports only `Formatting Not
  Review OK`; it must not also report `Source Review Hash Missing`.
  Confirm Organized List follows the same split: an available Original or
  Corrected source is not counted under `Source File Issues` merely because
  Formatting is not Review OK, while a genuinely missing source appears in both
  Source File Issues and Format Not OK when both conditions apply.
  Replacing that source externally, clearing the hash, or deleting a selected
  Corrected PDF/source must block final export; no Original fallback is allowed
  while a Corrected file is selected.
- Change Paper Master, active/Not Publishing state, review status, or settings
  from a second editor request while final export is assembling. Confirm the
  export fails and removes all partial outputs.
- Export each Excel report and confirm it does not create, delete, or rebuild
  any `PaperAuthor` rows. Change publication state while the Editorial Workbook
  is assembling and confirm the export fails, records a failed audit event, and
  leaves no final or `.part.xlsx` file.
- Enter disposable title/note text beginning with `=`, `+`, `-`, or `@`.
  Confirm XLSX cells contain the exact literal text and are not formulas; confirm
  generated manifest/warning CSV strings are neutralized while numeric values
  remain numeric.
- Preview generated reports/exports cleanup and confirm generated publication
  manifest/warning CSV files are selected while an unrelated editorial CSV in
  the Reports folder is retained.
- Open Final Submission Edit, Paper Master Edit, Title/Author Review,
  Exceptions, and Process PDF formatting triage in editor A. Change the same
  evidence in editor B, then submit editor A's stale form. Confirm every action
  is rejected and editor B's values remain unchanged.
- Export a CrossCheck batch, create a newer active Final or replace its
  publication PDF, then import the old result/report. Confirm it is counted as
  stale, neither score nor report is attached to the replacement, and final
  export remains blocked for missing current results.
- Create a Paper Master record with no Final and confirm Missing Final
  Submission blocks publication. Mark that Master paper Not Publishing with a
  reason and scope-impact confirmation; confirm the blocker disappears and
  both final and draft packages exclude it. Import a Final afterward and
  confirm it inherits the Master exclusion. Undo the decision and confirm
  review evidence is preserved while Missing Final or current-candidate
  readiness returns.
- Create a Paper Master `Decision Required` record in a disposable test
  database and confirm both final and draft packages stop until an editor
  explicitly keeps it in publication scope or marks it Not Publishing.
- Mark an orphan Final Not Publishing, then preview/import a new Paper Master
  record with the same ID. Confirm Preview names the affected Final, Apply
  creates Decision Required, and Final, Draft, CrossCheck, and Verify remain
  blocked until an explicit publication decision is made.
- Create an unclassified active Final outside Paper Master. Confirm its decision
  section appears before Missing Final, has no preselected exclusion reason,
  and does not offer a generic Edit Paper ID shortcut. Expand Resolve Paper ID,
  search by Master ID/title/author, confirm the selected title/authors summary,
  and verify it. Repeat with stale page evidence and confirm no ID changes.
  Submit Mark Not Publishing without a reason and confirm no state changes;
  then select a valid reason and confirm the orphan is retained only as tracked
  Not Publishing information.
- In a disposable database, create a Publishing Master with an inconsistent
  excluded Final mirror. Confirm Error Report shows Publication Decision
  Integrity Conflict and neither Draft nor Final package can bypass it.
- Duplicate CJK, Greek, and canonically equivalent accented publication titles
  must appear as duplicate-title blockers.
- Final publication manifest contains the publication fields plus the same
  per-paper Exceptions summary used by the Editorial Publication Workbook.
  Confirm allowed exception type, values, and reason are present, while Paper
  Master editorial notes remain excluded.

### Backup And Audit

- System State ZIP restore remaps managed files into local `data/` folders and does not leave old absolute paths.
- Audit Log records critical user/system actions as JSON Lines in `data/logs/audit.log`.
- Audit Log canonical action names use
  `<domain>_<operation>[_<phase>]`; category, action, status, and text filters
  return the same events, and legacy action names remain visible in expanded
  raw JSON.
- Clear Database preserves Audit Log by default, and the optional audit-clear checkbox archives the old log before starting a new one.
- System State ZIP includes active and archived audit logs.
