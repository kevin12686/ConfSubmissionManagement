# Conference Final Manager

A local no-login Django + SQLite application for managing conference final submissions.

This project intentionally does not run plagiarism checking. It stores plagiarism scores and reports imported from external tools.

Title/author extraction is built in to the Django system. The built-in extractor preserves the established PDF title/author extraction behavior, stores the extracted fields, saves the verification image, and tracks manual review.

## Quick Start On This Computer

```bash
./scripts/start_local.sh
```

Open <http://127.0.0.1:8000/>.

On macOS, you can also double-click `start.command` in Finder. It creates `.venv` if needed, installs requirements, applies migrations, creates local data folders, and starts the server.

On Windows, double-click `start_windows.bat` in File Explorer, or run it from Command Prompt. It performs the same setup/start steps as the macOS script.

If macOS says the script is not executable after copying the folder, run:

```bash
chmod +x start.command scripts/start_local.sh
```

## New Computer Setup

Use this process when installing the system on a new computer.

### 1. Install prerequisites

- Python 3.12 or newer
- Terminal, Command Prompt, or PowerShell access
- The full project folder
- Optional: a System State ZIP from the old computer if you want to restore an existing conference

No login, cloud service, or separate database server is required. The app uses local SQLite and local files under `data/`.

The first run needs internet access to install Python packages. After packages are installed, normal local use does not require internet access except for Bootstrap CDN assets in the browser UI.

### 2. Copy the project folder

Copy the whole `SubmissionManagementSystem` folder to the new computer. Do not copy only selected files; the app expects the Django project, templates, sample data, scripts, and requirements to stay together.

### 3. Start the app

From Terminal:

```bash
cd /path/to/SubmissionManagementSystem
./scripts/start_local.sh
```

Or on macOS, double-click:

```text
start.command
```

Or on Windows, double-click:

```text
start_windows.bat
```

The script will:

- create `.venv` if it does not exist
- install packages from `requirements.txt`
- create the local `data/` folders
- run `python manage.py migrate`
- start the local server at <http://127.0.0.1:8000/>

The script does not install Python itself. If `python3` is missing, install Python first and run the script again.

On Windows, install Python from <https://www.python.org/downloads/windows/> and enable `Add python.exe to PATH` during installation. The Windows script first tries the Python launcher `py -3`, then falls back to `python`.

### 4. Restore an existing conference state

If you exported a System State ZIP from another computer:

1. Open <http://127.0.0.1:8000/integrations/crosscheck/>.
2. Use `Restore System State`.
3. Upload the System State ZIP.
4. Review the preview.
5. Type `RESTORE SYSTEM STATE` to apply.

The archive is portable. It includes settings, conference name, database state, PDFs, source files, reports, previews, and managed files. File references are restored into the new computer's local `data/` folders, not the old computer's absolute paths.

### 5. Stop the app

Press `Ctrl+C` in the Terminal window running the server.

## Manual Setup

If you do not want to use the start script:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>.

## Architecture

The codebase is organized around domain-oriented controller and application layers:

- `submissions/controllers/`: thin Django controllers grouped by workflow.
- `submissions/application/selectors.py`: read/query composition for page contexts.
- `submissions/application/commands.py`: side-effectful workflow actions with result objects.
- `submissions/services/`: domain services for import, processing, verification, reporting, and integrations.
- `submissions/models.py`: legacy core records plus one-to-one state mirrors for identity, files, reviews, publication, and plagiarism.
- `submissions/tests/`: acceptance regression tests and shared factories.

The current model split is intentionally transitional. Existing `FinalSubmission` fields remain the behavior source of truth while the one-to-one state models are kept in sync. This keeps migrations safe and gives future changes a stable place to move reads and writes one domain at a time.

Primary routes now follow workflow domains:

- `/papers/`
- `/submissions/`
- `/reviews/paper-ids/`
- `/reviews/title-authors/`
- `/reviews/formatting/`
- `/reviews/exceptions/`
- `/processing/pdfs/`
- `/integrations/crosscheck/`
- `/integrations/system-state/download/`
- `/reports/`
- `/settings/`

## Main Folders

- `data/incoming/`: PDFs waiting to be scanned or processed.
- `data/active_final/`: active final PDFs copied with `[PaperID]-[TitleShortName].pdf`.
- `data/old_versions/`: inactive/replaced PDFs copied for retention.
- `data/reports/`: Excel exports.
- `data/extraction_results/`: suggested location for external extraction output files.
- `data/plagiarism_reports/`: suggested location for plagiarism reports from external tools.
- `data/media/final_submissions/`: canonical uploaded final PDFs.
- `data/media/source_submissions/`: canonical uploaded source files.
- `data/media/formatted_pdfs/`: corrected PDF uploads.
- `data/media/formatted_sources/`: corrected source uploads.
- `data/media/pdf_thumbnails/`, `data/media/format_previews/`, `data/media/title_author_verification/`: regenerated cache files.

Folder paths and limits can be changed from the Settings page.

The Settings page also includes Storage Management. It inventories managed files, shows missing database references, previews cleanup candidates, and repairs missing active/old publication paths. Cleanup is preview-first: a GET request never deletes files. Conservative cleanup only selects unreferenced regenerated cache plus orphaned active/old publication outputs, so thumbnails and previews still referenced by the database are kept. A separate generated reports/exports cleanup can remove regenerated Excel/ZIP downloads and external upload packages. Original uploads, corrected uploads, plagiarism report PDFs, and system state backups are retained by cleanup actions.

## CSV/XLSX Imports

Templates are in `sample_data/`:

- `paper_master_list_template.csv` for the Paper Master List
- `final_submissions_template.csv`
- `external_results_template.csv`

Use the Download Template buttons in the app to get CSV templates.

Paper master imports use these columns:

- `paper_id`
- `acceptance_status`
- `title`
- `authors`
- `notes`

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

Open the Title/Author page to run extraction on active PDFs. No external script path is required.

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
