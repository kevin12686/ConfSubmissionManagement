# Validation Record

This is measured evidence, **not deployment instructions or a guarantee of
defect detection**. Use [Deployment](deployment.md) to install/update and the
[Acceptance Runbook](editorial_acceptance_runbook.md) to repeat validation.

Obsolete per-release command narratives and internal production container IDs
have been removed. Older records remain in Git history; release changes remain
in [Changelog](../CHANGELOG.md). The following preserves the relevant baseline
and known limits without presenting old runtimes as the current configuration.

## Latest UI Acceptance: 1.31.7

| Scope | Recorded outcome |
| --- | --- |
| Native Django | 1093 passed, including shared CrossCheck summaries and publication/version/evidence regressions |
| JavaScript | 158 passed |
| Standalone discovery | 127 run, 2 Docker deployment opt-ins skipped; deployment/worker behavior unchanged |
| CrossCheck boundaries | Running, retry with applied papers, partial, awaiting application, complete, failed/rejected, unconfirmed, stale, missing evidence, unknown, empty, preview and cancelled states |
| List/detail parity | Same saved counts and labels; duplicate report history counted once, mismatching digest not counted as ready; reads leave evidence/DB/files unchanged and make no service request |
| Browser page sweep | 27 major pages at 1440/1366/1024/390px, 108 visits, all 200; no page errors, visible broken images or document overflow |
| Browser batches | 11 representative states agreed across list/detail; real 15-second browser polling with a mocked service changed running to awaiting application, not completion; 320px/reduced-motion checks passed |
| Other interactions | Organized Details/filter removal/Toast/drafts, Formatting lazy preview/history, Single restored-value navigation, manual-override lazy form, picker, Medium issue filter, JSON contrast, PDF navigation, repeated workbook download, blocked final ZIP and phone navigation passed |
| Integrity | All 1909 existing production DB/data file hashes unchanged; no added or removed files |

System/migration/docs/compile/whitespace gates passed. These are UI/read-only
summary changes, not changes to CrossCheck submission/download/retry/application,
publication selection, version binding or review reset rules. State archive stays
8 and comparison method stays 9. No real IEEE upload, Docker deployment, OCR or
CUDA inference was run in this UI round; all writes used disposable generated data.

## Previous UI Acceptance: 1.31.6

| Scope | Recorded outcome |
| --- | --- |
| Native Django | 1083 passed, including unchanged publication state and exact Corrected PDF/source and CrossCheck ZIP bytes |
| JavaScript | 158 passed, including picker races, actual-value form guards and detached-target HTMX feedback |
| Standalone discovery | 127 run, 2 Docker deployment opt-ins skipped; deployment/worker behavior unchanged |
| Browser | 23 major pages at 1366/1024/390px, 69 visits; all returned 200, no page errors, visible broken images or document overflow |
| Picker | Delayed initial lookup cannot replace the explicitly selected Paper ID; cancellation, failure/retry and teardown covered |
| Forms | Single/Focus restored notes, status and cleared file selections navigate without false warnings; cancelled departure retains drafts and accepted departure does not prompt twice |
| Feedback | Filtered exception removal shows identified Toast in viewport; sibling drafts survive, validation stays inline and close remains visible |
| Layout | Laptop action column and expanded Details stay within the table; collapsed phone navigation no longer reserves an empty row |
| Other interactions | Lazy first-page preview/history, current publication PDF navigation, repeated workbook downloads, Title/Author save/cursor and blocked final export passed |
| Integrity | All 1909 existing production DB/data file hashes unchanged; no added or removed files |

System/migration/docs/compile/whitespace gates passed. App version changes only;
State archive remains 8 and comparison method remains 9. Browser writes used
generated disposable records, never current conference records. Real Docker,
OCR and CUDA inference were not rerun for these shared UI changes.

## Previous UI Baseline: 1.31.5

| Scope | Recorded outcome |
| --- | --- |
| Native Django | 1082 tests passed, including fresh counts and exact selected Corrected PDF/source and CrossCheck ZIP bytes |
| JavaScript | 142 passed, including stale response rejection, hidden-tab pause, failure recovery and worklist drafts/cursor |
| Standalone discovery | 127 run, 2 Docker deployment opt-ins skipped; deployment/worker behavior unchanged |
| Browser | 23 major pages at 1440/390px, 46 visits; no page errors, visible broken images or document overflow |
| Interaction | Fast Process/discard/undo, cross-tab/history, HTMX conflict tabs, exception summaries/filter removal/page clamp, opening transitions, cross-page drafts and concurrent-save guard passed |
| Failure handling | Initial and last-known status failures/retry, obsolete reads and uncertain write-response wording checked |
| Integrity | All 1909 existing production DB/data files unchanged; testing used disposable generated records |

System/migration/docs/compile/whitespace gates passed. App version changes only;
State archive remains 8 and comparison method remains 9. This UI round did not
rerun real Docker deployments, OCR or CUDA inference; their relevant baseline is
recorded below, not claimed as a new run.

## Latest Engine Acceptance: 1.31.4 / Method 9

| Scope | Recorded outcome |
| --- | --- |
| Native Django | 1074 tests passed |
| Linux Docker Django | 1074 tests run, 10 native-platform checks skipped |
| JavaScript | 130 passed |
| Standalone discovery | 127 tests run, 2 deployment-only opt-ins skipped; deployment code unchanged in this round |
| Gates | System/migration/docs/compile/whitespace passed |
| Supplied R011 pair | Fresh Small/Small OCR, 300 DPI, 11 pages each |
| Final isolated worker run | 116.9 s under concurrent test load; not a performance benchmark |
| Frozen-evidence parity | Final native result identical; Linux comparator matched same frozen evidence |
| Browser | 1440/1024/768/390px, real OCR payloads, nonblank paired crops, no horizontal overflow |
| Integrity | 1909 production files and both input PDF hashes unchanged |

For the unadjudicated real pair, text findings changed 107 -> 98, unlocated stayed 14,
formatting Info 23 -> 31 and visual tolerance stayed 10. Segmentation changed:
these counts are not an accuracy score or proof that all defects were found.

Thirteen actual rendered-PDF/OCR controls passed: inline/split/wrapped hyphens,
minus deletion, numeric range, variable subtraction, letter change, insertion,
deletion, reference-to-question, adjacent numeric edit, shift and metadata-only.
Minus/range/letter/number changes remained Warnings; valid word hyphens were Info.

This round did **not** rebenchmark fresh Linux OCR or remote GPU inference.
Models/providers/DPI were unchanged. Publication/CrossCheck byte and review
regressions passed; old method results require rebuilding. State archive remains 8.

## Deployment Lifecycle Baseline: 1.31.2

Native standalone tests and both real Docker opt-ins passed in disposable
projects. Two synthetic conferences retained DB/reviews, publication hashes,
envs/tokens and explicit worker settings through both updater entrypoints.

Missing worker env/clients caused all guarded CLI paths to fail without
recreating credentials or touching conference data. Tests included stopped,
foreign/ambiguous projects, authorization corruption, backend/port/mount mismatch,
concurrent edits and post-cutover failure. A stopped CPU worker retained its
volume/signing identity when recreated.

These were CPU deployment tests, not fresh CUDA inference acceptance.
No production container/volume was deployed or mounted.

## Hardware Measurements

These are historical execution studies with the same-platform comparisons
described below. They are not method 9 accuracy measurements or universal timings.

| Study | Conditions | Outcome / limitation |
| --- | --- | --- |
| CPU parallelism, 1.30.x | Linux x86_64 i7-8700K, 4 physical cores/8 threads, Small/Small, 4 threads/lane, 24-page pair | About 129 s sequential versus 106 s at 2 pages; RSS 1.31 -> 2.47 GiB; complete evidence identical on that platform |
| ARM64 Linux cold CPU, 1.30.1 | Default 2 pages/4 threads, same 24-page pair, 300 s budget | Explicit failure around 300 s; no complete pair or warm-pair success claimed |
| CPU boundary parity, 1.30.1 | Native full pair plus 9 generated pages; Linux 9-page boundaries | Same-platform raw scores/boxes/geometry/results matched sequential baselines |
| GPU residency, 1.31.0 | Quadro RTX 5000 16 GiB, isolated CUDA Docker, 8 GiB RAM/8 CPUs, 4 pages/4 threads, Small/Small, 300 DPI | Raw OCR/geometry/render/result parity with per-job execution on 24-page pair plus 9 boundary pages |
| GPU latency, 1.31.0 | Same study; fresh namespace OCR | Per-job 20.269 s; resident first 18.846 s, next 17.581 s; model load 2.202 s |
| GPU idle/cache, 1.31.0 | Same study | VRAM 12058 MiB returned to 6 MiB after unload; complete cold-cache read did not reload models |

GPU tests used a test image layered over installed pinned CUDA dependencies,
not a clean dependency-install benchmark. Actor crash recovery, stopped-actor
timeout and parent-death cleanup passed. Cache/health did not extend idle lifetime.
CPU default concurrency is 2, not 4: an earlier 4-lane CPU study exceeded its
unchanged memory bound. More resources do not guarantee linear speedup.

## What Must Still Be Verified On A New Host

- Docker/NVIDIA installation, selected model/provider and finite memory.
- Real fresh full-document execution within that host's configured budget.
- Gateway authentication/request size/time limits.
- Exact current CFM/worker code contract.
- An actual PDF reader/comparison interaction and final-package byte checks.

A ready health response or matching release label is not that complete test.
Record fresh measurements separately when the runtime, models or hardware changes.
