# Acceptance Runbook

Run only against disposable checkout/database/media, private test workers and
unique Docker project names. No real conference volume, container or external
publication folder may be reused. Never submit real CrossCheck uploads without
explicit permission for the target folder and papers.

The [developer gate](developer_guide.md#regression-commands) is mandatory for code
changes. This procedure adds operational/browser proof; it is not a claim that
a release passed. Record results in [Validation Record](deployment_validation.md).

## Fixtures And Evidence

Create a small synthetic conference with:
- A fully ready Publishing paper.
- Two Start2 versions and a mixed Start2/Editor Upload paper.
- Wrong/unresolved ID, orphan Final and missing-Final Master.
- Missing PDF/source and selected-but-missing Corrected files.
- Page/author/P/S limit cases, stale/allowed exceptions and duplicate authors.
- Notes, manual override, review image, revision history and CrossCheck history.

Record app/method/archive versions, source revision, hardware/backend/model
settings, input hashes, commands, test counts/skips and unresolved limitations.
Keep pre/post DB/review/readiness snapshots and exact publication file hashes.
Use actual rendered PDF/OCR evidence when claiming recognition behavior.

## Warning And Worklist Freshness

With disposable data, process a pending PDF and discard/undo one side of a
Start2/Editor conflict in under five seconds. Confirm banners disappear/reappear
without repeated reloads. Repeat across two tabs, Back/Forward and hidden/visible
transitions. Fail a status request: last-known warnings must be labelled stale;
Retry must recover, and delayed old responses must not overwrite current data.

In Organized List, approve/remove an exception with a filter and pagination
active. Check summary, membership, ordering and page refill/clamping together.
Keep another panel open with an unsaved reason and confirm it survives the
replacement. Exercise stale signed evidence and validation errors. Compare
publication identities, selected bytes and review/reset state before/after
advisory reads; run the full developer gate.

Delay a picker's initial lookup, select a different Master, then release or fail
the original response: the new selection must remain. Repeat after HTMX removal.
In Single/Focus, restoring Notes/status must remove the dirty warning; cancelling
departure retains protection and accepting it asks only once, including history
return. Resolve an exception at the bottom of a filter and verify a visible,
Paper/Final-labelled Toast without losing other drafts. At 1366px, action buttons
must fit the Organized List; at 390px the collapsed Navbar has no empty strip.

## Editorial Workflow

| Test | Required outcome |
| --- | --- |
| Master import | Attention-first preview; notes preserve/apply choices; no notes-only review reset |
| Final import/re-import | ID-keyed updates; unchanged Author-entered ID preserves populated Official ID; missing rows not deleted |
| Record edit | Loaded-value Changed state, server preview/reset list, explicit confirm; no-op/file-identical changes preserve reviews |
| Stale/concurrent preview | Reject; no partial writes or unseen merge |
| ID review | Exact picker, correct Master metadata; invalid ID never auto-excluded |
| Orphan decision | Resolve via shared verifier or explicitly exclude; valid reason required |
| Master no Final | Can mark Not Publishing; Undo immediately restores missing-Final blocker |
| New Master over excluded orphan | Decision Required, not silent publication |
| Version rule change | Preview before Apply, correct old/new IDs, no review reset |
| Editor Upload | Match/mismatch/error guard, pending processing, expandable note |
| Discard/Undo | Version selection recalculates; history preserved; no whole-paper exclusion |
| Process PDFs | Current scope only; page/hash/thumbnails/debug refreshed; manuscript/review invariants |
| Processing triage | Issue appended to Formatting notes; correct page; stale evidence rejected |
| Extraction | Built-in/GROBID/override share renderer and exact current PDF; review reset dependencies |
| Manual override | Reason required, visible source, new evidence, audit, pending review |
| Review OK | Completes metadata review; reviewed title difference tracked, not second blocker |
| Formatting | Title guard before commit; source-only handling; stale signed evidence rejected |
| Single/Focus | Correct filtered queue, Save stays, explicit navigation/dirty warning |
| Exceptions | Separate limit types, reason required, approve/reapprove/remove, stale detection |
| Author counts | Live candidate scope, exact current PDF links; no cache-derived publication authority |

## Formatting History

Test consecutive uploads, same-name/different bytes, PDF-only/source-only pairs,
baseline capture, missing history and original-file invalidation.
Every revision must retain exact paired bytes/provenance. Status/notes-only
save creates no revision. Baseline is idempotent and labels capture time honestly.

Preview/cancel, stale evidence, concurrent save and file/DB/audit failures must
leave neither false revision nor partial pointer changes. History download checks
parent ownership/path/hash; corrupted history never supplies current files.
Check inactive/discarded records, lazy loading, over 25 revisions and narrow UI.

## PDF Comparison

### Engine

Test actual PDFs plus frozen evidence for:
- Insertions, deletions, changed words/numbers and broken references.
- Pure shift, reflow, repagination, initial-page deletion and metadata-only edits.
- Repeated words/table values, real swaps and field ownership.
- Missing/stale/contradictory native text; visible OCR remains authority.
- Minus/decimal deletion, numeric ranges, formulas and mixed tolerated edits.
- Direct visual groups versus invalid transitive composition, accents and
  known real-change tolerance blind spots.
- Inline/split/wrapped hyphens versus variables, adjacent math, attached digits,
  changed letters, missing context and anchor-cut boundaries.
- Blank/rotated/image pages, unequal lengths, empty OCR and work bounds.
- Corrupt/expired/runtime-mismatched cache; cold/pair/document reuse;
  failed pairs never reused as success.

Freeze raw OCR, locations, scores, geometry, accounting and render hashes.
Reduced findings alone is not accuracy. Large unpairable rewrites must explicitly
handoff. Missing Corrected/baseline or stale upload revision cannot fallback.

### Browser

At desktop, short window and 390 px:
- Both documents/crops nonblank; no page overflow; top dock fully visible.
- Neutral Info, red arrows and amber unlocated distinct; shared Hyphens filter.
- Auto match and connection use the same pair; reverse hover, edges, multi-region,
  hidden/offscreen connections and absent counterpart pages.
- Pointer magnification stays available entering Evidence/dock; long evidence
  can scroll without dismissing inspection.
- Next/Previous, explicit page changes, close/reopen, keyboard/touch and late loads.
- Retina DPR 1/2/3, min crop scale, cap/rotation/aspect, high-detail debounce/failure.
- Exact-snapshot detail pixels and PDF links; no result/hash/state modification.
- Automatic comparison stays on its owning card through saves, filters and HTMX.

## CrossCheck

Use mocked service/evidence for normal regressions. Real service tests require
approved target folders, minimal uploads and retained result evidence.

Validate service login/folder association, all/missing previews, exact publication
PDF bytes, submit uncertainty/reconciliation, 15-second visible polling, partial
results and explicit Apply. Retry-download must not upload again, overwrite an
execution ZIP or replay an applied result. Change active Final/PDF/manual scores
between preview/apply and verify rejection. Same-byte new Final is still a new
identity. Check restore preserves history/timestamps and applied-result locks.
Compare batch history/detail for running, partial, all-retrieved awaiting Apply,
and all-applied states. Missing reports/attention remain visible when some results
are ready. Retry must keep earlier applied counts. Preview/cancelled/empty must
not appear successful. Missing matching report history cannot count as ready.
Summary GETs must not contact the service, write DB/files or change signed evidence.

## Exports And State

| Test | Required outcome |
| --- | --- |
| Final ZIP | Exact current Corrected/Original bytes; scoped candidates only |
| Draft | Clearly labelled warnings; structural ambiguity still rejected |
| Filename collision | Fail before overwrite, case-insensitive sanitized names |
| Mid-export file/DB/settings change | Abort/remove partial output |
| Audit/output/ZIP validation failure | No promoted partial deliverable |
| Manifest | Extracted metadata/counts/P/S plus Exceptions; no Master notes/override reasons |
| Workbook | Selected sheets only; stable read-only snapshot, formulas inert, readable styles |
| System State | Current-version round-trip retains timestamps/reviews/images/revisions/audit/CrossCheck evidence |
| Legacy archives | Validate original v5/v6/v7 format before adapting; reject incomplete newer archives |
| Restore failure | DB/files roll back together; no stale cross-install paths |
| Cleanup | Protect references/revisions/debug/legacy outputs, including overlapping roots and post-preview changes |
| Clear Database | App-owned data cleared, external roots protected, audit preserved/archive option, failure rollback |

## Docker And Worker Lifecycle

Use [Deployment](deployment.md) commands in an isolated setup.

1. First CFM creates local CPU/models/tokens. First GPU uses explicit init.
2. Update via both updater entrypoints; preserve env bytes, explicit settings,
   tokens, DB/reviews and selected source hashes.
3. Remove env/auth, introduce mismatched identity/foreign project/mount, or
   concurrent env changes: reject without recreating identity.
4. Fail image build/preflight: old service retained. Fail cutover: report actual
   state without claiming automatic rollback.
5. Check proxy Host/port, CSRF-protected POST, static assets, large ZIP/download
   buffering, maintenance UI and fresh GET recovery without POST replay.
6. Worker health/version/settings: no secrets; ready/busy/not-ready/cold states,
   old/malformed metadata, safe gateway errors and custom-header round-trip.
7. Auth/namespace isolation, duplicate requests, queue bounds, retry limit,
   restarts, lost replies, stale jobs, runtime mismatch and timeout cleanup.
8. Real CPU/GPU inference: compare exact evidence on each platform, not mocked
   provider labels. Test GPU residency, idle unload, cold/warm cache, actor
   failure/reaping, finite memory and unchanged CPU path.
9. PDF.js reader: actual modules/thumbnails/search, wheel/keyboard/input focus,
   direct/download fallback, rejected/expired same-snapshot sources.

Do not claim a successful cold full-document run from a cache hit or a small
fixture. State what hardware/quality/time budget was tested. Clean only the
test-created containers/volumes/models/workspace afterward.
