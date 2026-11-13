# Architecture

CFM is one Django application with SQLite and managed files. It is a trusted
local editorial system, not a public multi-tenant service. Docker can run
separate conferences and a shared advisory compute worker.

## Mental Model

```text
Paper Master (scope + publication decision)
    -> Final Submission versions
        -> selected active submission
            -> selected Corrected or Original PDF/source
                -> evidence-bound editorial reviews
                    -> publication read snapshot
                        -> readiness -> strict final export

Formatting revisions / comparison images / debug copies
    -> inspection and traceability only
```

The browser, generated folders and compute workers never become publication
authority. [Publication Rules](publication_rules.md) is the business specification.

## Request And Domain Boundaries

```text
URL -> controller -> selector / command -> domain service -> models + managed files
                \-> Django templates -> shared HTMX/JS enhancement
```

Controllers own HTTP concerns. Selectors compose read-only contexts; commands
invoke state transitions. Services own rules, locking, signed evidence, reset
dependencies and audit. Templates render supplied state; JS enhances interaction,
not publication decisions.

The UI uses local Tabler/Bootstrap-compatible markup and HTMX. Normal URLs/forms
remain usable without partial navigation. There is no separate React service or
frontend build. See [UI Conventions](ui_conventions.md).

`workflow_alerts.py` reads advisory counts without cross-request caching.
`workflow_alerts.js` owns bounded, visible-only refresh, cross-tab invalidation
and last-known/error presentation. It does not write workflow state or feed
readiness. `worklist_state.js` preserves presentation state across opted-in
worklist POST replacements; the server still owns filtering and exception rules.
Shared `form_state.js` snapshots supply record-edit markers and the opt-in
Formatting `form_guard.js`; neither changes server validation. `toasts.js` and
one Django partial render full-page/HTMX feedback. `paper_picker.js` owns request
generation and teardown so asynchronous metadata cannot replace user intent.

## State Ownership

| State | Owner |
| --- | --- |
| Master decision | `InitialPaper`; Final exclusion fields are compatibility mirrors for mapped IDs |
| Version and editorial state | `FinalSubmission` compatibility record |
| Domain mirrors | Identity/File/Review/Publication/Plagiarism one-to-one state models |
| Historical Formatting pair | Immutable `FormattingRevision` and independent copies |
| Author cache | Derived compatibility data; live publication counts use read snapshots |
| CrossCheck execution/results | Conference-owned durable provenance/evidence records |
| Audit | Append-only file, separate from DB |
| Worker jobs/cache/models | Deployment-owned, outside conference storage |

`final_submission_state.py` maps compatibility fields into domain mirrors.
Normal saves synchronize affected domains; bulk workflows use its matching bulk
update/upsert service. Restore/repair performs full synchronization.
Do not introduce an unsynchronized second state authority.

## Publication Reads And Exports

`publication_read.py` builds one request/operation snapshot for settings, Master
scope, current submissions, authors and file inspection. Strict export hydrates
fresh filesystem hashes rather than trusting compatibility caches.

`file_manager.py` selects Corrected -> Original independently for PDF/source.
A selected but missing Corrected file fails closed. It never reads legacy
processed paths, revision history or debug copies as fallback.

Export stages exact byte entries, validates filenames/ZIP structure/CRCs,
rechecks publication-critical DB state and promotes atomically. XLSX exports
use the same read boundary and do not rebuild authors. Audit/validation failures
remove incomplete outputs. [Export integrity](publication_rules.md#export-integrity)
defines the full contract.

## Writes And Expensive Work

Signed form evidence binds what an editor reviewed. Apply locks/reloads the
record and checks evidence rather than overwriting unseen concurrent changes.
Record-edit/import/settings previews are separate from Apply.

Do not hold SQLite write locks across long PDF or remote calls. Capture identity
first, do expensive work outside the write transaction, then lock/revalidate
before applying. Files use staging, verified copies and rollback/promotion
coordination; a DB rollback alone cannot reverse filesystem changes.

Formatting writes own title-guard confirmation, evidence resets, current pointers
and immutable revision capture. Status-only writes do not create revisions.
Automatic comparison starts **after** a successful save, bound to that saved
revision, and cannot roll back or redirect the file save.

## Comparison Compute

```text
CFM: select + freeze PDFs + bind revision
    -> completed validated cache?
        yes -> new signed inspection snapshot
        no  -> preferred remote worker, or confirmed local CPU work
                    -> bounded OCR / alignment
                    -> CFM validates result + render hashes + current identity
                    -> signed inspection snapshot
```

Native defaults to embedded CPU. Docker uses a separate host-shared worker;
optional remote CPU/GPU service is preferred when configured.
`comparison_client.py` owns transport/idempotent routing, not file selection.
`comparison_execution_choice.py` binds local-compute consent.
Accepted/uncertain work stays on its original worker; disconnects must not cause
duplicate execution elsewhere.

Each worker has private namespace authorization, ledger, signing/cache storage,
one heavy job at a time and bounded fair queueing. It receives exact PDF bytes
and hashes, never a conference DB/media mount. Job children isolate settings and
namespace evidence. GPU residency adds one supervised model actor: it shares
weights, not namespace state. See [Worker Protocol](../comparison_slave/README.md).

`comparison_health.py` observes services without submitting jobs.
`comparison_metadata.py` allowlists packaged version and effective settings.
Formatting polls while visible; last-seen is labelled. These observations are
not readiness, route authorization, model-cache identity or evidence approval.

The [PDF Comparison contract](pdf_comparison.md) owns OCR ordering, tolerances,
limits, cache validation and inspection behavior.

## Optional Integrations

| Integration | Boundary |
| --- | --- |
| Built-in/GROBID extraction | Version-bound metadata extraction; success resets reviews and uses one verification renderer |
| Manual override | Reasoned metadata exception, new evidence, no manuscript edits |
| GROBID health | Bounded optional availability probe; no startup dependency |
| CrossCheck Service | Previewed publication PDFs -> confirmed uploads -> execution ZIPs -> explicit result preview/apply |
| CrossCheck retry-download | Original confirmed uploads only, immutable execution history; no PDF re-upload/replay |
| PDF.js | Same-origin allowlisted file endpoint only, read-only browser renderer |

GROBID uses header TEI without external consolidation. Failure preserves prior
extraction rather than replacing it with empty metadata.
CrossCheck list/detail presentation shares a read-only item classifier and batch
summary. The list prefetches bounded page items with an existence check for the
matching retained result; it does not contact the service or open archives.
Retrieval/application counts are historical evidence, not current publication
permission. Action flags, signed evidence and archive/hash/version validation
remain in the original workflow services.

CrossCheck optional login/IEEE overrides are machine-local secrets. They are
separate from workflow records and excluded from System State.
PDF.js disables scripting/editing/XFA and never writes back a manuscript.
See [Operator Guide](operator_guide.md) for interaction steps.

## Storage, Audit And Restore

Managed storage includes upload files, corrected files, revision copies,
review images, generated reports and audit logs. Paths are remapped into the
receiving installation during validated restore. Temp tokens, comparison caches
and machine secrets have different lifetimes; do not serialize them as workflow
state.

System State validates models/counts/hashes, file membership, parent relationships
and portable paths before Apply. DB/file rollback is coordinated. Original
timestamps survive restore, so old previews do not become young again.
Explicit legacy adapters do not waive validation of newer archives.

Storage inventory registers references including revision files and protects
them even under overlapping roots. Cleanup rechecks identity/references at
Apply. Clear Database stages app-owned data and preserves audit by default.
[Operations](operations.md) owns the maintenance procedures.

Audit writes JSON Lines to `data/logs/audit.log`; central action names and
legacy aliases belong to `audit_actions.py`. UI formatting never rewrites
stored events. Log safe metadata/hashes, not secrets or binary content.

## Docker Runtime Boundary

Conference Compose runs web plus Nginx and project-specific data/static volumes.
The worker is a separate Compose project on the comparison network.
`SMS_DATA_DIR` is a verified raw backup mirror, not Docker's live data volume.

The deployer validates ownership/env/data/network before changes, provisions
models/credentials only under explicit lifecycle rules, and preflights candidate
images before replacement. Updates are not distributed transactions: an earlier
conference can be updated even if a later one fails. Cutover failure is not
automatic migration/data rollback.

The proxy preserves Host/port and CSRF, serves pinned static assets and provides
maintenance feedback while web is unavailable. Recovery navigates to a new GET,
never replays a POST. Binary ZIP/PDF/Office responses avoid dynamic gzip;
bounded proxy buffering is not shared response caching.

See [Deployment](deployment.md), [Operations](operations.md) and the
[developer regression gate](developer_guide.md#regression-commands).
