# Conference Final Manager

A local no-login Django + SQLite application for managing conference final submissions.

This project intentionally does not implement plagiarism checking, PDF title extraction, or PDF author extraction. It only stores manual or imported results from external tools.

The title/author extraction integration calls your existing `ExportTitleAuthor.py` script. The extraction logic remains in that script; Django only runs it, stores the extracted fields, saves the verification image, and tracks manual verification.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

## Main Folders

- `data/incoming/`: PDFs waiting to be scanned or processed.
- `data/active_final/`: active final PDFs copied with `[PaperID]-[TitleShortName].pdf`.
- `data/old_versions/`: inactive/replaced PDFs copied for retention.
- `data/reports/`: Excel exports.
- `data/extraction_results/`: suggested location for external extraction output files.
- `data/plagiarism_reports/`: suggested location for plagiarism reports from external tools.

Folder paths and limits can be changed from the Settings page.

## CSV/XLSX Imports

Templates are in `sample_data/`:

- `initial_papers_template.csv` for the Paper Master List
- `final_submissions_template.csv`
- `external_results_template.csv`

Use the Download Template buttons in the app to get CSV templates.

Paper master imports use these columns:

- `paper_id`
- `acceptance_status`
- `title`
- `authors`

Final submission imports use these columns:

- `final_submission_id`
- `author_entered_paper_id`
- `final_submission_title`
- `final_submission_authors`
- `upload_date`
- `uploaded_fields`

On the Final Submissions page, upload the metadata file together with the PDFs and source files. Files are matched by Final Submission ID using names like `34_file_Submit_PDF.pdf` and `34_file_Submit_Source.docx`. The file extension decides the actual type, so a PDF placed in the source slot is still handled as the PDF.

Use the Verify Paper IDs page to compare Final Submission Title against the title in the Paper Master List, correct author-entered Paper IDs, and mark records verified.

Use the Organized List page for the cleaned working list. It shows each Paper Master record matched to the active final version, including verification, page count, PDF/source status, extraction status, plagiarism status, similarity score, and report path.

External results are matched by `final_submission_id` first. If that is missing or unmatched, the importer falls back to matching the active final submission by `paper_id` or `paper_id_filled`.

## Title/Author Extraction

Open the Title/Author page to run extraction on active PDFs. The app calls the script path configured in Settings:

```text
/Users/kevin/Codes/UTDConferenceTools/PDF Title/ExportTitleAuthor.py
```

For each active PDF, the app stores:

- `extracted_title`
- `extracted_authors`
- extraction status and message
- the generated verification image under `data/media/title_author_verification/`
- manual verified/unverified state

Manual edits or external result imports mark title/author verification as unverified again, so the changed result can be checked.

The Title/Author page has two separate checks:

- extraction verification: confirm the marked image and extracted title/authors are correct
- title match verification: compare extracted title against Final Submission Title to confirm it is the same paper

Identical normalized titles are auto-verified. Non-identical titles show a character-level diff and can be manually verified or moved back to unverified.

## Formatting Corrections

Open the Formatting page to download original PDF/source files, edit them outside Django, and upload corrected files back into the system.

Corrected files are stored separately from author originals:

- corrected PDFs: `data/media/formatted_pdfs/`
- corrected source files: `data/media/formatted_sources/`

The app keeps the original upload, the corrected upload, a format status, and notes. If a corrected PDF is uploaded, Process PDFs will use the corrected PDF first and should be run again to refresh page count, hash, thumbnails, and active-final copies.

## PDF Processing Scope

The Process PDFs page does only these things:

- scan the incoming folder for PDFs
- calculate PDF page count
- render one thumbnail per PDF page for quick visual checking
- compute SHA-256 PDF hash
- determine active versions
- copy active PDFs to the active folder
- copy inactive/replaced PDFs to the old versions folder

It does not extract titles, extract authors, or run plagiarism checks.

## Reports

The app can export:

- active final versions
- old versions
- error report
- author count
- all reports in one workbook

Exports are saved under the configured reports folder.
