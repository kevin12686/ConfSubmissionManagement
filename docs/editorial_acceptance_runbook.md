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

1. Configure Settings, including conference name, page limits, author limits, plagiarism thresholds, timezone, folders, and active-version rule.
2. Import the Paper Master List and verify preview sorting places changed/new rows above unchanged rows.
3. Import Final Submission metadata and upload matching PDF/source files. Confirm preview sorting places mapping/file/reset issues above unchanged rows.
3a. Use `Add Final Submission` to create one record manually. Confirm matching Paper Master title evaluation, uploaded PDF/source paths, Pending processing/review state, active/replaced version recalculation, and a `final_submission_manual_create` audit event. Submit an invalid form and confirm no record is created.
4. Open Dashboard and confirm its blocking check count matches the current final-package readiness rows. Only workflows with blockers should appear under Next actions; clear workflows should appear under No current blockers.
5. Open Verify Paper IDs. Correct P003 and verify only after it maps to a valid Paper Master record.
6. Mark P004 as Not Publishing and confirm it moves out of publication blockers while remaining visible in the Not Publishing List.
7. Create the Editor Upload for P007. Confirm the title guard compares PDF title against Paper Master title and Final Title, then verify the Start2/Editor conflict warning appears.
8. Discard either the Start2 or Editor Upload version for P007 with a required note. Confirm the conflict clears.
9. Run Process PDFs. Confirm only current Paper Master publication candidates are processed; discarded, Not Publishing, invalid-ID, and historical versions must not create processing errors. Confirm page counts, hashes, thumbnails, and publication PDF debug copies are generated. Confirm all matching thumbnail strips remain expanded. Exercise `Needs processing`, `Page issues`, `Processed`, and `All`; use paper jump; verify sticky paper identity, fixed-size lazy thumbnails, page labels, enlarged preview, and failure tile without changing any record.
10. Run Title/Author Review for needs-review records. Review extracted title/authors, title differences, red flags, and verification images together. Confirm that Review OK is the only completion action and no second title-match confirmation appears. If GROBID fallback is enabled, test it only on suspicious rows and confirm successful GROBID output still returns to Pending review. For one difficult paper, test Manual override with a reason and confirm it is visibly marked, audited, and still requires Review OK.
11. Open Formatting Review. Confirm list mode is a compact queue and only one selected paper remains expanded. Change tabs and search, verify the worklist updates and URL history changes without losing the page shell, then disable JavaScript or open the filter URL directly to confirm the ordinary GET fallback. Use Single Paper Mode, upload a corrected PDF/source for one paper, confirm the corrected PDF title guard, Save stays on that paper, and then re-run Process PDFs.
12. Export CrossCheck/plagiarism PDFs with a token, import result CSV with Plagiarism % and Single %, and upload optional report PDFs.
13. Open Author Count. Confirm publication paper count is per normalized author, duplicate-author warnings are reviewable, and name/Paper ID search plus attention filters do not change counts.
14. Open Exceptions. Search/filter to P008, approve it only with a note, and confirm allowed page/author/plagiarism exceptions move to Info and do not block final export. Change an approved plagiarism score and confirm the exception becomes stale and blocks export again.
15. Confirm Final Submissions opens with submission tabs/table first and `Import / Re-upload` collapsed. Expand it, drop/select metadata plus PDF/source files, verify counts/type summary/removal, and confirm preview-before-apply still controls storage. Open Final Submission Edit from Organized List, Title/Author Review, Formatting Review, Not Publishing, Verify Paper IDs, and Exceptions. Confirm Save returns to the same worklist/view/filter/search/tab. Confirm the edit order is Submission identity, Metadata, Current row files, Plagiarism data/report, Workflow status summary, and Save. Confirm version discard is a separate form under the collapsed bottom `Version actions` danger zone and still requires a reason. Then open Organized List, switch Checklist/Compact candidates, and confirm both show the same active publication scope.
16. Exercise Author Count search/filter/sort, Exceptions status/type/search, Title/Author grouped views, Verify Paper ID filters, Final Submission tabs, and Process PDF filters. Confirm URLs can be refreshed/shared and normal GET navigation still works without JavaScript.
17. Export a draft publication package while blockers exist and confirm the warnings CSV lists skipped and risky items.
18. Resolve all blockers and export the final publication package.
19. Open Audit Log and confirm recent import, Process PDFs, review, CrossCheck, exception, and export actions are searchable by Paper ID or Final ID.
20. Download a System State ZIP, clear the database/files in a test environment, restore the ZIP, and confirm state/files and audit logs return.
21. At 390px and desktop width, confirm the page itself does not overflow, tables scroll inside their containers, 15px table/body text and 12px badges remain readable, buttons fit their labels, focus is visible, modal/collapse controls remain keyboard-operable, and repeated POST clicks do not submit twice. Confirm the two-level application header keeps the current conference visible, the workflow navigation collapses below 1200px, and active/hover/dropdown states remain readable.

## Acceptance Checks

- Import preview never mutates records or files before Apply.
- Dashboard and Final Publication Package export use the same readiness blockers; Dashboard must never show Ready when strict final export is blocked.
- Dashboard paper counts use active publication papers and do not count inactive old versions or count one paper twice merely because it has multiple findings.
- Re-uploaded PDFs/sources reset only dependent review/check flags.
- Corrected PDFs are first priority for publication-facing links, CrossCheck export, duplicate checks, and publication packages.
- If no corrected PDF exists, the original active-submission PDF is the publication-facing PDF source.
- Process PDFs recalculates active versions, page counts, hashes, thumbnails, author cache, and debug copies, but it must not rewrite original uploads, corrected uploads, extracted data, plagiarism scores, or review flags.
- Editor Uploads are active over Start2 until the conflict is resolved, but unresolved conflicts block final publication export.
- Discarded versions remain traceable and appear as old versions, not current publication candidates.
- Not Publishing records remain traceable but are excluded from publication readiness and final packages.
- Not Publishing and invalid-ID records are excluded from Title/Author and Formatting review queues; invalid-ID records remain visible in mapping/readiness workflows.
- Final Submission Edit cannot directly change processing, Title/Author Review, duplicate-author review, or Not Publishing state; those states are owned by their dedicated workflows.
- UI navigation, partial GET updates, display filters, thumbnail previews, and layout changes must not alter publication PDF/source priority, active-version selection, readiness categories, review reset behavior, publication ZIP contents, or audit requirements.
- Expand an Audit Log JSON record and inspect inline path/action code on Settings and Integration pages; monospace content must use dark text on a muted light surface and remain readable.
- UI-only GET requests must leave publication ZIP entry names, PDF/source SHA256 values, manifest rows, and readiness blocker categories byte-for-byte/logically unchanged.
- Old Versions classifies inactive records as Replaced, Discarded, or Other inactive; Not Publishing appears only as a secondary flag.
- Error Report separates Critical, Medium, and Info items.
- Allowed exceptions do not block final export while their approved value still matches the current value.
- Draft publication package is clearly marked and contains a warnings CSV.
- Final publication package contains one PDF/source pair per publishable Paper Master record and no replaced, discarded, or Not Publishing records.
- Final publication package file bytes match the current publication-facing PDF/source priority for each active publishable Paper Master record: Corrected PDF/source first, then Original PDF/source.
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
