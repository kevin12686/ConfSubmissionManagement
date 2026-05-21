# AGENTS.md

This file applies to the entire repository.

## System Purpose And Risk

Conference Final Manager is a local Django + SQLite system for managing conference
final-submission versions, editorial checks, publication readiness, CrossCheck data,
and final publication exports.

Treat publication-facing behavior as high risk. A wrong active version, wrong PDF or
source file, missing blocker, stale review state, or incorrect export can create an
irreversible IEEE publication error. After publication, correction may not be allowed,
and the conference may carry the consequences.

## Required Documentation Discipline

Before every change:

- Read the relevant current docs before editing. Start with `README.md`, then use the
  applicable files in `docs/`:
  - `docs/operator_guide.md` for user-facing workflows.
  - `docs/developer_guide.md` for service boundaries, tests, reset rules, and versions.
  - `docs/architecture.md` for source-of-truth and workflow invariants.
  - `docs/troubleshooting.md` for operator-facing failure modes.
  - `docs/editorial_acceptance_runbook.md` for end-to-end acceptance expectations.
- Inspect the current implementation before assuming a documented rule is still true.

After every change:

- Re-read the affected docs and update them when behavior, workflow, UI text, routes,
  exports, schemas, settings, backup/restore behavior, cleanup behavior, or operator
  expectations changed.
- Do not treat documentation updates as optional follow-up work. If code and docs
  describe the same behavior, they must be handed off together.
- Check whether the version should advance. Increment `APP_VERSION` in
  `conference_final_manager/settings.py` for user-visible behavior, workflow, docs,
  UI, schema, or export changes. Increment `STATE_ARCHIVE_VERSION` only when System
  State ZIP structure or restore compatibility changes.

## Core Publication Invariants

- The Paper Master List is the publication scope.
- A Final Submission is publication-relevant only when its Paper ID is in the Paper
  Master List, it is not discarded, and it is not marked Not Publishing.
- Discard is version-level. It excludes one Final Submission version but does not mean
  the paper is excluded from publication.
- Not Publishing is paper/publication-decision-level. It preserves traceability while
  excluding the paper from readiness and final package output.
- Editor Uploads outrank Start2/imported submissions for active-version selection.
- If undiscarded Start2 and Editor Upload versions both exist for the same Paper ID,
  the Editor Upload may be temporarily active, but final publication export must remain
  blocked until one side is discarded with a note.
- Active-version rule changes must be previewed before apply and must not reset review
  flags merely because the rule changed.

## Publication File Sources

Use app-managed helpers rather than ad hoc path logic.

- `source_pdf_path()` is processing/extraction input: Corrected PDF, then Original PDF.
- `publication_pdf_info()` is publication-facing output: Corrected PDF, then Original
  PDF from the active submission.
- `publication_source_info()` is publication-facing output: Corrected source, then
  Original source from the active submission.
- Publication package export, draft package export, CrossCheck export, duplicate
  checks, Organized List publication links, Active Versions, and publication-facing
  links must use publication-facing helpers.
- `data/publication_pdf_debug/` is an inspection copy only. It must never become the
  source of truth for publication package export, CrossCheck export, duplicate checks,
  or publication links.
- Legacy fields and folders such as `current_file_path`, `source_current_file_path`,
  `active_final_folder`, and `old_versions_folder` may exist for restored data and
  traceability. They must not decide final publication PDF/source selection.
- Do not delete old uploads for traceability unless a documented cleanup workflow
  explicitly selects regenerated, unreferenced artifacts.

## Processing, Review, And Reset Rules

- Process PDFs is not a read-only page-count operation. It recalculates active
  versions, page counts, PDF hashes, thumbnails, author cache, and publication debug
  copies.
- Process PDFs must not scan incoming folders, create submissions, rewrite original or
  corrected uploads, modify source files, change extracted title/authors, change
  plagiarism scores, or change review statuses.
- Reset only dependent review/check flags:
  - Changed PDF resets processing, title/author extraction, title match review,
    plagiarism scores, formatting review, and related file-derived exceptions.
  - Changed source resets formatting review.
  - Changed extracted authors resets author-number and duplicate-author review state.
  - Changed Paper ID resets Paper ID verification and active-version grouping.
  - Changed Paper Master notes must not reset review/check status.
- Corrected PDF uploads require Process PDFs again before final publication readiness
  can be trusted.
- Exceptions are valid only when explicitly allowed with required notes and when their
  approved value still matches the current value.

## Architecture Boundaries

- Keep controllers thin: forms, redirects, messages, downloads, and template rendering.
- Put reusable workflow and domain behavior in `submissions/services/`.
- Use `submissions/application/selectors.py` for read-only page/query contexts.
- Use `submissions/application/commands.py` for state-changing workflow wrappers where
  the existing pattern applies.
- Keep templates as simple Bootstrap pages. There is no React or frontend build.
- `FinalSubmission` remains the compatibility record and behavior source of truth while
  one-to-one state models mirror lifecycle domains. Writes must stay synchronized until
  legacy fields are intentionally retired.

## Audit, Backup, Restore, And Cleanup

- Any workflow that changes records, files, review status, publication readiness,
  settings, exports, cleanup, or backup/restore must write audit events through
  `submissions/services/audit.py`.
- Use the audit helper matching the outcome: `audit_preview()`, `audit_requested()`,
  `audit_success()`, `audit_failure()`, or `audit_blocked()`.
- Audit entries should include relevant Paper ID, Final Submission ID, changed fields,
  before/after values, reset flags, file changes, hashes, and result counts.
- Store portable project/media-relative paths in audit and System State data. Do not log
  machine-specific temp paths or binary PDF/source/report content.
- System State export must include settings, database state, managed files, referenced
  review artifacts, active audit log, and archived audit logs.
- System State restore must remap managed files into the current project's local
  `data/` tree and must reject unsupported or corrupt archives. Do not preserve old
  machine-specific absolute paths.
- Clear Database preserves `data/logs/audit.log` by default. Only the explicit
  audit-clear option may archive and start a fresh audit log.
- Storage cleanup is preview-first. It must preserve original uploads, corrected
  uploads, plagiarism report PDFs, System State backups, referenced thumbnails/previews,
  and audit logs according to current docs.

## Testing And Acceptance

For documentation-only changes, run at least:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

For code changes, run the full regression gate unless there is a documented reason not
to:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test submissions
.venv/bin/python -m compileall -q submissions conference_final_manager manage.py
```

Add or update acceptance coverage in `submissions/tests/test_acceptance.py` when
changing active-version selection, import preview/apply behavior, review resets,
publication readiness or export blocking, file priority, publication package output,
System State export/restore, storage cleanup, audit logging, Editor Upload, discard, or
Not Publishing behavior.

If publication export behavior changes, test both strict final package and draft package
paths and verify package bytes come from the current publication-facing PDF/source
priority.

## Working Style

- Prefer existing services, helpers, models, tests, and docs over new abstractions.
- Do not bypass preview-before-apply workflows for imports, re-uploads, restore, or
  settings that materially alter publication candidates.
- Do not manually copy files from `data/` for normal exports; use app download/export
  workflows.
- Do not change publication source-of-truth behavior casually. If it must change, update
  operator docs, architecture notes, troubleshooting, acceptance tests, and version
  metadata together.
