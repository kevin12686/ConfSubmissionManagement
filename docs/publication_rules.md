# Publication Rules

This document is the canonical description of publication-facing behavior.
Operator and developer documentation may summarize these rules, but changes to
publication scope, active versions, file resolution, readiness, or export must
be reflected here.

## Rule Ownership

| Concern | Source of truth |
| --- | --- |
| Publication scope | Paper Master List |
| Active Final Submission | Active-version services and configured ordering rule |
| Publication PDF | `publication_pdf_info()` |
| Publication source | `publication_source_info()` |
| Readiness blockers | `publication_readiness_rows()` |
| Final and draft package assembly | Central publication export service |
| State-change history | Audit Log |

Generated copies, legacy path fields, browser state, and compatibility caches
must not replace these sources of truth.

## Publication Scope

Paper Master defines which Paper IDs belong to the publication.

A Final Submission can participate only when:

1. Its official Paper ID exists in Paper Master.
2. It is not discarded.
3. Its Paper ID group is not marked Not Publishing.
4. Its active-version state is unambiguous.

Not Publishing is a paper-level decision. Mark and undo apply to every Final
Submission with the same official Paper ID. Mixed included/excluded versions
are a Critical `Mixed Not Publishing Decision` blocker; export does not guess
which version should win.

Discard is version-level. It removes one version from consideration while
preserving the record and reason for traceability.

## Active Version Selection

Active selection is evaluated separately for each Paper ID:

1. Ignore discarded versions.
2. If undiscarded Editor Uploads exist, select the newest Editor Upload.
3. Otherwise select the newest Start2/imported submission.
4. Determine newest using the Settings rule: Final ID order or upload date,
   with Final ID as the tie-breaker.

When both Start2 and Editor Upload versions remain undiscarded, the Editor
Upload is temporarily active, but the conflict blocks final export until one
side is discarded with a reason. A clearly marked draft may use that
deterministic Editor Upload selection and records the conflict in its warnings
CSV.

Inactive, discarded, Not Publishing, and invalid-ID records remain available
for history or correction but do not become publication candidates.

## Publication File Resolution

PDF and source files resolve independently:

| Kind | First choice | Fallback |
| --- | --- | --- |
| PDF | Corrected PDF | Original PDF on the active submission |
| Source | Corrected source | Original source on the active submission |

Fallback applies only when no Corrected file is selected. If a selected
Corrected file is missing, unreadable, changed after validation, or fails its
review binding, publication is blocked. The application must not silently use
Original instead.

`data/publication_pdf_debug/` is generated inspection output. It is never an
input to publication links, duplicate checks, CrossCheck export, or final
package export.

Legacy fields such as `current_file_path`, `source_current_file_path`,
`active_final_folder`, and `old_versions_folder` may remain for restored-state
compatibility or diagnostics. They do not select publication files.

## Process PDFs Contract

Process PDFs recalculates active versions and processes only current Paper
Master publication candidates. For those candidates it:

- calculates page count and PDF hash;
- generates operation-unique page thumbnails;
- resets page-limit exceptions when the page count changes;
- refreshes the compatibility author cache;
- synchronizes publication PDF debug copies.

It must not:

- scan folders and create submissions;
- rewrite Original or Corrected uploads;
- change extracted title/authors or plagiarism scores;
- reset unrelated review state;
- treat discarded, Not Publishing, invalid-ID, or historical versions as
  processing errors.

The compatibility author cache is not publication authority. Author counts and
their blockers are derived from the current active Paper Master snapshot.

## Review And Reset Dependencies

Review state resets only when its evidence changes.

| Changed evidence | Required effect |
| --- | --- |
| Publication PDF bytes or selected PDF | PDF processing, page exception, Title/Author, duplicate-author, plagiarism, and Formatting state become stale |
| Publication source bytes or selected source | Title/Author, duplicate-author, and Formatting state become stale |
| Page count | Page-limit exception becomes stale |
| Extracted title/authors | Title/Author Review and dependent author checks become stale |
| Plagiarism or Single score | Matching approved score exception becomes stale |
| Official Paper ID | Paper ID verification and active grouping are recalculated |
| Paper Master notes only | No review state resets |
| Active-version ordering rule | Preview and apply selection changes; do not reset unrelated review fields |

`Review OK` is the single Title/Author completion decision. A reviewed
Final-versus-extracted title difference remains tracked information, not a
second publication blocker.

Formatting `Review OK` binds the SHA-256 of the selected publication source.
Pending or Needs Edit is blocked by Formatting status. A missing review hash is
an integrity blocker only after Review OK, so the same unfinished review is not
reported twice.

## Readiness And Exceptions

Dashboard, Organized List, Error Report, and final export consume the same
publication-readiness rules. Dashboard must never show Ready when strict final
export would be blocked.

Exceptions are narrow, evidence-bound decisions:

- page, author-limit, Plagiarism %, and Single % exceptions require a reason;
- an exception is valid only while the approved value still matches current
  evidence;
- author paper-count exceptions remain author-level;
- allowed exceptions move the corresponding finding out of blocking state but
  do not modify the underlying measurement.

Structural ambiguity is not an ordinary warning and cannot be bypassed by draft
export. This includes multiple active candidates, mixed Not Publishing state,
and duplicate sanitized publication filenames.

## Export Integrity

Final export is a single validated operation:

1. Build one publication read snapshot for scope, settings, active submissions,
   readiness, and file status.
2. Reject readiness blockers and structural ambiguity.
3. Strictly hash and read the exact PDF/source bytes selected by that snapshot.
4. Reject changed filesystem identity or content.
5. Reject sanitized, case-insensitive ZIP filename collisions.
6. Build immutable manifest and ZIP entry bytes.
7. Verify ZIP entries and CRCs.
8. Recheck publication-critical database state.
9. Atomically promote the completed package.

If Paper Master, submissions, settings, review state, waiver state, or selected
files change during assembly, export fails and removes partial output.

A draft package is clearly marked and contains a warnings CSV, but it still
fails closed on structural ambiguity and unsafe file selection.

Binary downloads such as ZIP, PDF, image, and Office files are not dynamically
gzip-compressed. Publication ZIP responses retain their normal content type and
length.

## Concurrency And Preview Safety

Long-running processing and extraction capture semantic row/file evidence and
recheck it under lock before persistence. Stale results must not overwrite
newer files, review decisions, thumbnails, or database state.

Import, Editor Upload, Formatting Upload, restore, cleanup, and material
Settings changes use preview-before-apply. Confirmation revalidates the
server-owned preview token, stored file size/hash, and current database
evidence. Temporary previews expire and are not part of System State backup.

GET pages, filters, pagination, detail panels, browser caches, and HTMX
navigation are read-only. They cannot select publication files or mutate
review, active-version, or exception state.

## Audit Requirements

State-changing workflows, exports, cleanup, Settings changes, and System State
operations write audit events through `submissions/services/audit.py`.

Events include the relevant Paper ID, Final Submission ID, changed fields,
before/after values, reset flags, file changes and hashes, result counts, and
failure details where applicable. Paths are portable project/media-relative
references; binary file contents are never logged.

Clear Database preserves the active audit log unless the operator explicitly
selects the archive-and-clear option.

## Implementation Map

| Rule area | Primary implementation |
| --- | --- |
| Active versions and PDF processing | `submissions/services/pdf_processor.py` |
| Publication file selection | `submissions/services/file_manager.py` |
| Request-scoped publication snapshot | `submissions/services/publication_read.py` |
| Readiness and author checks | `submissions/services/checks.py` |
| Exceptions | `submissions/services/exceptions.py` |
| Publication and report exports | `submissions/services/reports.py` |
| Formatting source binding | `submissions/services/formatting.py` |
| Signed workflow evidence | `submissions/services/workflow_evidence.py` |
| System State backup/restore | `submissions/services/system_state.py` |
| Audit logging | `submissions/services/audit.py` |

## Change Checklist

When a publication-facing rule changes:

1. Update this document.
2. Update the affected Operator, Developer, Architecture, Troubleshooting, and
   Acceptance sections only where their audience needs the change.
3. Add or update acceptance coverage proving Dashboard and export agree.
4. Run the regression gate in `docs/developer_guide.md`.
5. Increment `APP_VERSION`.
6. Increment `STATE_ARCHIVE_VERSION` only if archive structure or restore
   compatibility changes.
