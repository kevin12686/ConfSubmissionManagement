# Publication Rules

This is the canonical business-rule reference. For tasks, use the
[Operator Guide](operator_guide.md); for implementation, use
[Architecture](architecture.md). A documentation rewrite must not change these rules.

## Authority

| Concern | Authority |
| --- | --- |
| Paper scope and decision | Paper Master / `InitialPaper.publication_decision_status` |
| Active version | Active-version service and configured ordering |
| PDF / source selection | `publication_pdf_info()` / `publication_source_info()` |
| Readiness | `publication_readiness_rows()` on a publication read snapshot |
| Package assembly | Publication export service |
| State transitions | Audited workflow services, not templates or generated folders |

## Scope And Publication Decisions

Master decisions are Publishing, Not Publishing or Decision Required.
A candidate must have a Master Paper ID, be undiscarded, belong to a Publishing
Master and have unambiguous active state.

Not Publishing is a paper decision, even before a Final exists. It excludes that
paper from processing/review candidates, CrossCheck, publication author/duplicate
counts and both final/draft packages. The record remains for traceability.
Marking requires an explicit reason, signed evidence and scope confirmation;
the editorial note is optional.
Undo restores Publishing without resetting reviews; a still-missing Final
immediately blocks again.

Final exclusion fields mirror the Master decision where a Master exists.
Imports and Editor Uploads inherit it. Disagreement is a structural integrity
blocker, not permission to choose whichever field is convenient.

An orphan Final outside Master needs explicit ID resolution or Not Publishing.
No invalid ID automatically implies exclusion. Resolution selects an existing
Master and calls the same signed `verify_submission()` command as ID Review;
it must not silently assign Official ID through a general edit.

Creating/importing a Master whose orphan Final has an exclusion enters
**Decision Required**, never silent Publishing. Ambiguous/migrated decisions
must be resolved before either final or draft export.

Discard is different: it removes one submission version from selection and
retains its reason/history, not the whole paper's publication decision.

## Active Versions

For each Paper ID:

1. Exclude discarded versions.
2. Prefer undiscarded Editor Uploads; otherwise use Start2.
3. Select newest by configured Final ID natural order or upload date, with
   Final ID tie-breaker.

Undiscarded Start2 and Editor Uploads together are a version conflict. Editor
Upload is provisionally active but final export blocks until the editor resolves
the conflict by discarding unwanted versions. A labelled draft can use that
deterministic candidate and record the conflict in warnings.

Changing the ordering rule requires preview and confirmation. It recalculates
active/duplicate state but preserves both old/new rows' existing reviews,
including Paper ID verification. Newly active rows must independently satisfy
readiness; selection itself approves nothing.

## Publication Files

| File | Selection |
| --- | --- |
| PDF | Selected Corrected PDF, otherwise active submission's Original PDF |
| Source | Selected Corrected source, otherwise active submission's Original source |

Selection is independent for PDF and source. **Fallback means no Corrected
selection, not a missing/broken Corrected file.** Missing, unreadable, changed
or unbound selected evidence blocks publication; never silently substitute Original.

`current_file_path`, `source_current_file_path`, legacy active/old folders
and `data/publication_pdf_debug/` do not select publication input.
Debug copies are generated from the same publication helper for inspection;
their absence does not block export. Final Submissions displays files belonging
to that row, not a different active version for the same paper.

## Process PDFs

Only current Publishing Master candidates are processed. The operation
recalculates active versions, computes pages/hash, generates unique thumbnails,
resets page exceptions when counts change, refreshes the compatibility author
cache and synchronizes debug PDF copies.

It does not scan incoming folders, create submissions, rewrite Original or
Corrected files, change extracted metadata/plagiarism scores, or reset unrelated
reviews. Global alerts, Dashboard and the processing page share this candidate
scope; pending orphan/Not Publishing PDFs are not actionable processing work.

The author cache is compatibility data, not publication authority. Publication
author counts derive from the current active Master snapshot.

## Review And Reset Dependencies

| Evidence change | Required dependency effect |
| --- | --- |
| Master Title | Reset affected Paper ID review |
| Master notes only | No review resets |
| Author-entered ID | Re-resolve Official ID; reset Paper ID review |
| Final Title | Preserve Official ID; reset Paper ID review and extracted-title comparison |
| Final Authors | Preserve Official ID/extracted values; Title/Author Review Pending |
| Official ID | Re-evaluate ID review and active grouping |
| PDF replacement | Reset processing/page evidence, Title/Author and duplicate-author review, author-number exception, plagiarism scores/report association and P/S exceptions, Formatting binding |
| Source replacement | Reset Formatting binding, Title/Author and duplicate-author review, extracted-title comparison and manual override metadata |
| Page count change | Invalidate page exception |
| Extraction/manual override | Pending Title/Author Review; reset dependent author reviews/exceptions and recalculate title match |
| P% / S% change | Approval becomes stale if approved score no longer matches |
| Report upload alone | Does not clear score exceptions |
| Master publication decision | Recalculate scope/mirrors; no unrelated review resets |
| Active ordering rule | Recalculate selection/duplicates; preserve review evidence |

The central reset helpers clear fields, rather than merely changing labels.
Formatting corrected-PDF save retains prior extracted strings for reference but
not their approval; re-extraction is still required. Replacing original files
archives/unlinks invalidated corrected files only after history protection.
Existing revisions remain.

Final re-import uses Final ID. Unchanged Author-entered ID plus nonblank
Official ID preserves the mapping. A changed Author-entered ID or blank Official
ID invokes resolution. Failure remains manual ID work, never automatic exclusion.
Records absent from an import are not deleted.

Title/Author **Review OK** is the completion decision; reviewed title differences
are tracked information, not a second mandatory title-match blocker.
Manual override requires a reason/new evidence and renewed Review OK.
Built-in/GROBID re-extraction clears override reason/time.

Formatting Review OK binds the selected publication source SHA256.
Pending/Needs Edit blocks via status; Source Review Hash Missing is reserved
for already Review OK records without their required integrity binding.
An already reviewed record receiving corrected files returns to Pending.

## Readiness And Exceptions

Dashboard, Organized List, Error Report and final export use the shared readiness
rules. Presentation may group derivative warnings under one cause or count
unique papers; it cannot remove export blockers or invent approval.

Page, authors-in-paper, papers-per-author and separate P%/S% exceptions require
reasons and matching current evidence. Approved values becoming different make
exceptions stale. Valid approvals waive only their corresponding limit finding,
not underlying values or unrelated blockers. Duplicate-author review is distinct.
Cross-paper author-count decisions remain author-level.

Structural blockers cannot be bypassed by draft: unresolved publication
decisions/orphans, decision-mirror conflicts, multiple active candidates and
sanitized case-insensitive filename collisions. Ordinary readiness warnings may
appear in a clearly labelled draft's warnings CSV.

## Export Integrity

A publication export:

1. Builds one scope/settings/active/readiness/file snapshot.
2. Rejects blockers appropriate to final/draft and structural ambiguity.
3. Strictly reads/hashes exact selected files and rechecks filesystem identity.
4. Rejects sanitized case-insensitive ZIP filename collisions.
5. Builds immutable entry/manifest bytes and verifies ZIP/CRCs.
6. Rechecks publication-critical DB state and atomically promotes output.

Changed records, settings, waiver state or selected files abort and remove partial
output. Missing history/debug/cache cannot authorize fallback.

Manifest fields include extracted metadata, counts, P/S and the workbook's
per-paper Exceptions summary **including reasons**. Master editorial notes and
manual-override reasons are excluded.

Internal Excel exports use stable read-only publication snapshots, not a
PaperAuthor rebuild. They validate workbook structure, concurrent state and
audit writes before atomic promotion. Formula-like text is non-executable in
XLSX; CSV string fields receive text protection where needed. Excel style/sheet
choices do not change scope or publication input. Not Publishing may appear in
internal supporting sheets, never in the final package.

## Preview, Concurrency And External Results

State-changing forms validate signed evidence for what the editor saw.
Stale records/files reject instead of merging. Edit/import/settings/Editor
Upload confirmations are not permission to bypass later identity checks.

CrossCheck binds exact active Final, PDF hash and batch provenance before
submission and apply. Only explicit confirmed uploads with downloaded reports,
valid scores and matching archive evidence can apply after human preview.
Partial/missing/stale results do not clear prior scores or reviews.
A new Final cannot inherit old-version results merely because bytes match.

Retry-download uses original confirmed uploads only: no PDF re-upload or batch
renaming. Executions retain their own ZIPs/results. Apply rechecks manuscript
identity where known, exact Final/hash and current plagiarism evidence.
Applied results cannot be replayed or overwritten, including after restore.

## Advisory Artifacts

| Artifact | Allowed role | Never allowed |
| --- | --- | --- |
| Formatting revisions | Immutable full-pair history/download | Restore pointers, substitute a missing current file or approve review |
| PDF comparison | Inspect exact active After versus same-row Original/verified revision | Approve/reset review, waive a blocker or choose a publication file |
| Comparison worker/cache | Byte/runtime-bound evidence computation/reuse | Read conference DB, choose files or change editorial state |
| PDF.js reader | Display exact originating endpoint | Select another version or serialize edited output into publication |
| Debug PDF copy | Human inspection of generated output | Publication/CrossCheck/duplicate-check authority |

Comparison revalidates live scope, current files, revision binding and snapshot
hashes on build/read. Automatic comparison runs after successful file save in a
separate operation; failure leaves saved files intact. Missing counterpart pages
and unlocated OCR are explicit, never equal text or a replacement page.
Complete cache hits still require current scope/file/revision checks.

OCR can miss text and deliberately tolerates listed similar characters,
diacritics, whitespace and word hyphens. Those tolerances are **not editorial
exceptions**. Figures, tables, layout and mathematical structure need human
inspection. Zero findings or successful CPU/GPU execution is not publication
approval. See [PDF Comparison](pdf_comparison.md).

## Audit And Change Control

Audit state-changing operations and export outcomes with before/after values,
resets and file hashes as applicable; no secrets or binaries. History GETs and
revision downloads use their defined traceability events.

Before changing a rule: inspect its service/callers, update its tests and this
document, run the [developer gate](developer_guide.md#regression-commands), and
evaluate app/archive versions independently.
