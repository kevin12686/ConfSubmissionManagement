# Editorial Acceptance Runbook

This runbook is for manual end-to-end validation before using the system for a real publication handoff. The standard is strict: final publication export should be clean only when readiness issues are resolved or explicitly allowed as exceptions.

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

## Manual Workflow

1. Configure Settings, including conference name, page limits, author limits, plagiarism thresholds, timezone, folders, and active-version rule. Confirm the editable form appears without waiting for Storage Management or GROBID, then confirm the storage panel and health status fill in separately. Refresh Storage Management and verify its counts remain stable without changing any records. Create a cleanup preview, replace one candidate at the same path, and confirm Apply skips it as changed; an overlapping Reports folder must not select System State or import/restore preview files.
2. Import the Paper Master List and verify preview sorting places changed/new rows above unchanged rows.
3. Import Final Submission metadata and upload matching PDF/source files. Confirm preview sorting places mapping/file/reset issues above unchanged rows.
3a. Use `Add Final Submission` to create one record manually. Confirm matching Paper Master title evaluation, uploaded PDF/source paths, Pending processing/review state, active/replaced version recalculation, and a `final_submission_manual_create` audit event. Submit an invalid form and confirm no record is created.
4. Open Dashboard and confirm its blocking check count matches the current final-package readiness rows. Only workflows with blockers should appear under Next actions; clear workflows should appear under No current blockers.
5. Open Verify Paper IDs. Correct P003 and verify only after it maps to a valid Paper Master record.
6. Mark P004 as Not Publishing and confirm it moves out of publication blockers while remaining visible in the Not Publishing List.
7. Create the Editor Upload for P007. Trigger a title mismatch and confirm the title safety check shows the uploaded PDF title above vertically stacked Paper Master and Final comparisons, combines identical references, and provides word-level plus expandable character differences. At 320, 768, 1024, and 1440 pixel widths, confirm long titles wrap without horizontal page overflow. Open the temporary PDF, then test replacing and canceling a preview without creating a submission. Finally confirm a mismatched upload and verify it remains unverified and the Start2/Editor conflict warning appears.
   In a disposable data folder, change the preview PDF bytes and separately edit
   the Paper Master after preview; both confirmations must fail without creating
   an Editor Upload.
8. Discard either the Start2 or Editor Upload version for P007 with a required note. Confirm the conflict clears.
9. Run Process PDFs. Confirm only current Paper Master publication candidates are processed; discarded, Not Publishing, invalid-ID, and historical versions must not create processing errors. Confirm page counts, hashes, thumbnails, and publication PDF debug copies are generated. Confirm all matching thumbnail strips remain expanded. Exercise `Needs processing`, `Page issues`, `Processed`, and `All`; use paper jump; verify sticky paper identity, fixed-size lazy thumbnails, page labels, enlarged preview, and failure tile. Record one general formatting issue and one page-specific issue from the enlarged preview. Confirm both append to the same Formatting notes, set Needs edit, clear a prior Review OK binding, leave PDF/processing/Title-Author/plagiarism state unchanged, and create audit events.
10. Run Title/Author Review for needs-review records. Review extracted title/authors, title differences, red flags, and verification images together. Generate one Built-in, one GROBID, and one Manual Override image and confirm all three use the same layout, differing only in their source label. Test one PDF with substantial white space above its title and confirm the header reuses that space, then test one with a top logo/text and confirm the image expands instead of covering it. Use a long title and a long filename to confirm the header wraps with small margins and never reaches the PDF title/authors. Use adjacent or intentionally split author names to confirm every parsed author has a separate numbered legend entry and green boundary. Test an author surname followed by both a Unicode superscript and a merged numeric affiliation marker; confirm the author remains highlighted, the green boundary excludes the marker, and a longer different surname does not false-match. Hold `Ctrl` over the verification image and confirm the shared magnifier exposes those boundaries without interfering with the normal full-image link. Change the filter and move to another page through the partial worklist navigation, then confirm every newly displayed verification image still opens the magnifier without a full browser refresh. Confirm that Review OK is the only completion action and no second title-match confirmation appears. If GROBID fallback is enabled, test it only on suspicious rows and confirm successful GROBID output still returns to Pending review. For one difficult paper, test Manual override with a reason and confirm it is visibly marked, audited, and still requires Review OK.
11. Open Formatting Review. Confirm list mode is a compact worklist and only one
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
12. Export CrossCheck/plagiarism PDFs with a token, import result CSV with Plagiarism % and Single %, and upload optional report PDFs.
13. Open Author Count. Confirm publication paper count is per normalized author, duplicate-author warnings are reviewable, and name/Paper ID search plus attention filters do not change counts.
14. Open Exceptions. Search/filter to P008, approve it only with a note, and confirm allowed page/author/plagiarism exceptions move to Info and do not block final export. Change an approved plagiarism score and confirm the exception becomes stale and blocks export again.
15. Confirm Final Submissions opens with submission tabs/table first and `Import / Re-upload` collapsed. Expand it, drop/select metadata plus PDF/source files, verify counts/type summary/removal, and confirm preview-before-apply still controls storage. Open Final Submission Edit from Organized List, Title/Author Review, Formatting Review, Not Publishing, Verify Paper IDs, and Exceptions. Confirm Save returns to the same worklist/view/filter/search/tab. Confirm the edit order is Submission identity, Metadata, Current row files, Plagiarism data/report, Workflow status summary, and Save. Confirm version discard is a separate form under the collapsed bottom `Version actions` danger zone and still requires a reason. Then open Organized List, switch Checklist/Compact candidates, and confirm both show the same active publication scope.
16. From one Final Submission Edit page, open Paper ID Review, Process PDFs,
    Title/Author Review, Formatting Review, Not Publishing, and Organized List.
    Confirm each destination shows the shared focused-record banner and only the
    intended Final/Paper record. Create a collision such as Final ID `58` plus
    Paper ID `R058`; exact links must not show both, while manually searching
    `58` may. Confirm an inactive or excluded target shows an outside-scope
    explanation and that opening any focused GET leaves all review and active
    flags unchanged.
16. Exercise Author Count search/filter/sort, Exceptions status/type/search, Title/Author grouped views, Verify Paper ID filters, Final Submission tabs, and Process PDF filters. Confirm URLs can be refreshed/shared and normal GET navigation still works without JavaScript.
17. Exercise Paper Master List and Final Submission sorting before and after search. Confirm natural ID order (`P2` before `P10`, Final `2` before `10`), pagination preserves the selected sort, Final Submission tabs retain it, and Old Versions tabs match the shared worklist tab design.
18. Export a draft publication package while blockers exist and confirm the warnings CSV lists skipped and risky items.
19. Resolve all blockers and export the final publication package. Request it
    with gzip accepted and confirm the response remains `application/zip`, has
    `Content-Length`, has no `Content-Encoding: gzip`, and opens normally.
20. Open Audit Log and confirm recent import, Process PDFs, review, CrossCheck, exception, and export actions are searchable by Paper ID or Final ID.
21. Download a System State ZIP, clear the database/files in a test environment, restore the ZIP, and confirm state/files and audit logs return. Point Reports temporarily at a shared external test folder and confirm Clear Database preserves it. Simulate a database-reset failure and confirm staged publication files are restored with the records.
22. At 390px and desktop width, confirm the page itself does not overflow, tables scroll inside their containers, 15px table/body text and 12px badges remain readable, buttons fit their labels, focus is visible, modal/collapse controls remain keyboard-operable, and repeated POST clicks do not submit twice. Confirm the two-level application header keeps the current conference visible, the workflow navigation collapses below 1200px, and active/hover/dropdown states remain readable.

## Acceptance Checks

- Import preview never mutates records or files before Apply.
- Dashboard and Final Publication Package export use the same readiness blockers; Dashboard must never show Ready when strict final export is blocked.
- Dashboard paper counts use active publication papers and do not count inactive old versions or count one paper twice merely because it has multiple findings.
- Re-uploaded PDFs/sources reset only dependent review/check flags.
- Corrected PDFs are first priority for publication-facing links, CrossCheck export, duplicate checks, and publication packages.
- If no corrected PDF exists, the original active-submission PDF is the publication-facing PDF source.
- Process PDFs recalculates active versions, page counts, hashes, thumbnails,
  the compatibility author cache, and debug copies, but it must not rewrite
  original uploads, corrected uploads, extracted data, plagiarism scores, or
  review flags. A stale concurrent batch must not overwrite a newer thumbnail
  directory.
- Editor Uploads are active over Start2 until the conflict is resolved, but unresolved conflicts block final publication export.
- Discarded versions remain traceable and appear as old versions, not current publication candidates.
- Not Publishing records remain traceable but are excluded from publication readiness and final packages.
- Not Publishing and invalid-ID records are excluded from Title/Author and Formatting review queues; invalid-ID records remain visible in mapping/readiness workflows.
- Final Submission Edit cannot directly change processing, Title/Author Review, duplicate-author review, or Not Publishing state; those states are owned by their dedicated workflows.
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
- Every paginated worklist shows the same page-size/page controls above and
  below its rows. Use the bottom control to change page and confirm the next
  view returns to the top of that worklist with search/filter/sort state intact.
- Error Report duplicate rows show a compact matching-record count; opening
  `Show matching records` must return the complete duplicate group without
  changing any database or file state. Verify this on a numbered page and with
  Page size `All`.
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
- Final publication package contains one PDF/source pair per publishable Paper Master record and no replaced, discarded, or Not Publishing records.
- Final publication package file bytes match the current publication-facing PDF/source priority for each active publishable Paper Master record: Corrected PDF/source first, then Original PDF/source.
- Formatting Review `Review OK` stores the current publication source hash.
  A Pending/Needs Edit record with no source hash reports only `Formatting Not
  Review OK`; it must not also report `Source Review Hash Missing`.
  Replacing that source externally, clearing the hash, or deleting a selected
  Corrected PDF/source must block final export; no Original fallback is allowed
  while a Corrected file is selected.
- Change Paper Master, active/Not Publishing state, review status, or settings
  from a second editor request while final export is assembling. Confirm the
  export fails and removes all partial outputs.
- Open Final Submission Edit, Paper Master Edit, Title/Author Review,
  Exceptions, and Process PDF formatting triage in editor A. Change the same
  evidence in editor B, then submit editor A's stale form. Confirm every action
  is rejected and editor B's values remain unchanged.
- Export a CrossCheck batch, create a newer active Final or replace its
  publication PDF, then import the old result/report. Confirm it is counted as
  stale, neither score nor report is attached to the replacement, and final
  export remains blocked for missing current results.
- Mark an inactive version Not Publishing and confirm every version with that
  Official Paper ID is excluded. Manually create a mixed included/excluded
  state in a disposable test database and confirm both final and draft package
  exports stop with `Mixed Not Publishing Decision`.
- Duplicate CJK, Greek, and canonically equivalent accented publication titles
  must appear as duplicate-title blockers.
- Final publication manifest contains publication fields only; editorial notes are not included.
- System State ZIP restore remaps managed files into local `data/` folders and does not leave old absolute paths.
- Audit Log records critical user/system actions as JSON Lines in `data/logs/audit.log`.
- Clear Database preserves Audit Log by default, and the optional audit-clear checkbox archives the old log before starting a new one.
- System State ZIP includes active and archived audit logs.

## Automated Command Checklist

Run these before manual acceptance:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```
