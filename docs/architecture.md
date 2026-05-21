# Architecture Notes

Conference Final Manager is a local Django application with SQLite storage and local file management. The application is intentionally no-login and single-machine.

## Application Boundaries

- Controllers handle HTTP forms, redirects, Django messages, downloads, and template rendering.
- Application selectors assemble read-only page contexts and query composition.
- Application commands wrap state-changing workflows and return result objects.
- Services contain domain logic for imports, verification, PDF processing, formatting, title/author extraction, CrossCheck/plagiarism integration, exceptions, reports, storage cleanup, and backup/restore.
- Templates stay simple Bootstrap pages; there is no React or separate frontend build.

Core route groups:

- `/papers/`: Paper Master List.
- `/submissions/`: Final Submissions and Editor Upload.
- `/reviews/`: Paper ID, title/author, formatting, exceptions, and Not Publishing workflows.
- `/processing/pdfs/`: page count, hashes, thumbnails, publication debug copies, and active-version recalculation.
- `/reports/`: readiness reports, author count, version history, and publication exports.
- `/integrations/crosscheck/`: CrossCheck/plagiarism package export/import and System State Backup/Restore.
- `/settings/`: app settings, active-version rule preview, storage management, and clear database.

## FinalSubmission State Split

`FinalSubmission` remains the compatibility record and behavior source of truth. Newer one-to-one state models mirror lifecycle domains:

- `FinalSubmissionIdentityState`
- `FinalSubmissionFileState`
- `FinalSubmissionReviewState`
- `FinalSubmissionPublicationState`
- `FinalSubmissionPlagiarismState`

The split supports gradual refactoring. Reads can move to state models one workflow at a time, but writes must stay synchronized until legacy fields are fully retired.

## Workflow Rules

- Paper Master List is the publication scope.
- Final Submissions can come from Start2 imports or Editor Uploads.
- Editor Uploads are prioritized over Start2 records, but unresolved Start2/Editor conflicts block final publication export until one side is discarded.
- Discard is version-level: it excludes one Final Submission version but does not mean the paper is not publishing.
- Not Publishing is paper/publication-decision-level: it keeps records for traceability but excludes the paper from publication readiness and package output.
- Publication PDF priority is corrected PDF, then original author PDF.
- Publication source priority is corrected source, then original source.
- Active version selection is previewed before changing the active-version rule in Settings.
- Import/re-upload workflows are preview-before-apply when they may change existing records or files.
- Review flags are reset only when dependent data changes.

## Current Publication Resolution

Current active-version selection is implemented in `submissions/services/pdf_processor.py`.

1. All `active_version` flags are cleared.
2. Discarded submissions are excluded.
3. Submissions are grouped by `paper_id_filled`.
4. If a group has undiscarded Editor Uploads, only Editor Uploads are candidates.
5. Otherwise all undiscarded submissions for that Paper ID are candidates.
6. The selected candidate is determined by Settings:
   - `final_id`: numeric/natural Final ID sort.
   - `upload_date`: upload date, with Final ID sort as tie-breaker.
7. State mirror tables and `PaperAuthor` cache are refreshed after active selection.

Publication file resolution is implemented in `submissions/services/file_manager.py`.

`source_pdf_path()` is used for processing/extraction input and resolves:

1. Corrected PDF.
2. Original uploaded PDF.

`publication_pdf_info()` is used for publication-facing links, CrossCheck export, duplicate checks, and publication package export. It currently resolves:

1. Corrected PDF.
2. Original uploaded PDF.

`publication_source_info()` resolves corrected source, then original uploaded source.

This distinction matters. Process PDFs calculates page/hash/thumbnails from the same Corrected/Original PDF source and may sync `data/publication_pdf_debug/` for inspection, but that debug folder is not read by publication package export, CrossCheck export, duplicate checks, or publication-facing links.

Legacy `current_file_path`, `source_current_file_path`, `active_final_folder`, and `old_versions_folder` values are retained for compatibility with older restored data and debug traces. They are not publication source-of-truth fields.

## File And Path Safety

Managed files live under the project `data/` tree by default. Database fields may store file paths, but System State export/restore must remap managed paths into the receiving project folder. The snapshot includes referenced review artifacts such as title/author verification images, PDF thumbnails, and format previews because they preserve manual review context.

Do not preserve machine-specific absolute paths in restored state. Snapshot manifests may include portable path references and hashes, but restore must reject corrupted or unsupported archives. Temporary preview token folders are excluded from snapshots.

Storage cleanup is split by risk:

- Conservative cleanup removes only unreferenced regenerated cache. It does not delete publication debug, legacy active-final, or old-version output folders.
- Generated reports/exports cleanup removes regenerated Excel/ZIP downloads and external upload packages.
- Original uploads, corrected uploads, plagiarism report PDFs, system state backups, and referenced thumbnails/previews are retained.

## Versioning

The app version is defined in `conference_final_manager/settings.py` as `APP_VERSION`.

The System State archive format is defined separately as `STATE_ARCHIVE_VERSION`. Increment the archive version only when backup/restore structure or compatibility changes. Increment the app version for user-visible behavior, workflow, docs, or schema changes.

The footer displays both values so a user can match a System State ZIP to the expected application version.

## Regression Gate

Run these checks before merging or handing off changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```

For documentation-only changes, `check` and `makemigrations --check --dry-run` are usually enough, plus a link/stale-term review.
