# Editorial Acceptance Runbook

This runbook is for manual end-to-end validation before a real publication handoff.
The standard is strict: the publication package must fail while any readiness issue
exists, and it may succeed only after the error report is clean.

## Dummy Conference Dataset

Create six Paper Master records:

| Paper ID | Title | Authors | Expected outcome |
| --- | --- | --- | --- |
| P001 | Ready Paper | Ada Lovelace; Alan Turing | Publishes successfully |
| P002 | Revised Version Paper | Grace Hopper | Newer final version publishes |
| P003 | Mapping Problem Paper | Katherine Johnson | Blocks until Paper ID is fixed |
| P004 | Unpaid Paper | Barbara Liskov | Mark Not Publishing |
| P005 | Missing Source Paper | Donald Knuth | Blocks until source is uploaded |
| P006 | Similarity Review Paper | Edsger Dijkstra | Blocks until plagiarism is resolved |

Create eight Final Submission records:

| Final ID | Author-entered ID | Intended Paper ID | Scenario |
| --- | --- | --- | --- |
| 10 | P001 | P001 | Clean PDF/source, all reviews complete |
| 20 | P002 | P002 | Older version, should become replaced |
| 21 | P002 | P002 | Newer version, should become active |
| 30 | WRONG | P003 | Mapping error until editor fixes it |
| 40 | P004 | P004 | Unpaid or withdrawn; mark Not Publishing |
| 50 | P005 | P005 | PDF exists, source missing |
| 60 | P006 | P006 | Plagiarism over threshold |
| 61 | P006 | P006 | Duplicate or replacement edge case |

## Manual Workflow

1. Import the Paper Master list.
2. Import Final Submission metadata and attach PDF/source files.
3. Open Organized List and confirm each issue appears under the expected category.
4. Fix P003 by verifying/correcting the Paper ID.
5. Mark P004 as Not Publishing and confirm it no longer blocks publication readiness.
6. Upload the missing source for P005.
7. Upload a corrected PDF/source for one active paper and confirm reviews reset.
8. Run Process PDFs after any corrected PDF upload.
9. Import or enter extracted title/authors.
10. Verify Paper ID, title/author extraction, and extracted-title match.
11. Import plagiarism results and resolve any over-threshold P/S scores.
12. Open Error Report and confirm it is empty before exporting.
13. Export the publication package.

## Acceptance Checks

- Before all blockers are fixed, Download Publication Package ZIP must redirect back
  to Export Reports and show an error.
- After all blockers are fixed, the ZIP must include:
  - exactly one manifest CSV,
  - one PDF per publishable active paper,
  - one source file per publishable active paper,
  - no files for Not Publishing papers,
  - no replaced final versions.
- Manifest rows must match the database values for Paper ID, extracted title,
  author count, page count, plagiarism percent, and single percent.
- Each ZIP PDF/source file must match the file currently shown by the system as the
  publication PDF/source.
- Re-uploading a PDF/source after review must reset dependent reviews and block
  publication until the editor reviews it again.

## Automated Command Checklist

Run these before manual acceptance:

```bash
.venv/bin/python manage.py test
.venv/bin/python manage.py check
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```

