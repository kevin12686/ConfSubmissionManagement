# Deployment Validation

This document owns measured deployment evidence, not installation instructions.
Use the [Docker Guide](docker_guide.md) for current commands, the
[Acceptance Runbook](editorial_acceptance_runbook.md) for reproducible release
checks, and [PDF Comparison](pdf_comparison.md) for the comparison contract.
Historical counts and results below apply only to their labelled test round.

## Worker Metadata Acceptance (1.31.1 / Method 8)

- Disposable native source copy: 1,049 Django tests passed, standalone discovery
  ran 111 tests with two Docker opt-in skips, and all 125 JavaScript tests passed.
  Check, migration drift, documentation, compile and whitespace gates passed.
- Built the actual standalone CPU image from its production Dockerfile. In an
  isolated temporary-storage container, real model preflight returned v1.31.1,
  Small/Small, two pages, four threads, 300-second timeout and CPU idle 0.
  Authenticated health and `--status` agreed; unauthenticated health was rejected.
  No existing conference/worker container or volume was used.
- Browser acceptance covered List/Single/Focus at widths 320/390/768/1024/1440,
  safe settings, missing old-worker metadata, compatible release mismatch,
  incompatible contract, explicit last-seen data and ten-second polling. No
  horizontal overflow, JavaScript exception or job submission was observed.
  GPU/offline/legacy UI responses were synthetic; this round did not rerun remote
  CUDA inference or the two full-updater/header Docker opt-ins. The preceding
  residency release retains its separately labelled actual GPU evidence below.
- The actual container compute contract remains
  `3ea6e26d3f9f24c6150b00bf57d0bb18af6edc9ea373750b8db735abcf8f4b26`.
  Version/settings are not evidence/cache identity. No OCR, comparison,
  publication-selection, review-reset or archive-v8 format change was made.

## GPU Model Residency Acceptance (1.31.0 / Method 8)

- Native macOS: 1,038 Django tests passed. Standalone discovery ran 110 tests
  with two opt-in skips; both actual Docker opt-ins passed separately. All 123
  JavaScript tests and check/migration-drift/docs/compile/whitespace gates passed.
  No database migration or State archive change is needed (archive v8).
- Isolated ARM64 Linux app Docker: 1,038 tests ran with ten platform skips;
  the read-only Compose cases ran on macOS. Real updater acceptance retained
  two disposable conferences' DB/review state, publication files, envs and tokens
  through both entrypoints. Custom-header container round-trip also passed.
- Actual CUDA acceptance used the Quadro RTX 5000 16-GiB host, finite 8-GiB RAM
  and eight-CPU test containers, pinned Small/Small models, four pages, four
  threads per engine, unchanged 300 DPI/batch 6 and 300-second work budget.
  The test-only image layered the new standalone source over the installed
  pinned CUDA dependencies; this is real GPU inference, not a fresh dependency
  installation test. No production volume, port or worker was reused/restarted.
- Both supplied 12-page PDFs matched per-job execution exactly: raw OCR strings,
  serialized scores, word segmentation/boxes, native geometry, render hashes
  and final comparisons. The model PID was reused across fresh OCR in distinct
  namespaces. Raw OCR SHA256 was
  `30540752da581042bb42669d4debcac7a664a6d74893f5a6c515e34899c3545e`.
  The same checks cover nine generated blank/rotated/two-column/edited pages
  with unequal document lengths. This is execution parity, not defect accuracy.
- Representative final real-pair times: per-job execution 20.269 seconds;
  resident first fresh pair 18.846 and a second uncached namespace 17.581;
  initial model loading 2.202. Direct isolated pair-cache execution was 1.781
  seconds warm / 2.588 after idle unload (not HTTP ledger-cache lookup timing).
  Residency saves initialization, not neural inference, and uses more standing
  VRAM. These are measurements, not universal speedup guarantees.
- Final real-pair VRAM rose from 6 to 12,058 MiB, then returned to 6 after idle
  process termination. Health/cache reads did not keep it warm; cold pair-cache
  reuse allocated no model process, while a fresh namespace reloaded it.
- Real actor crash recovery, a stopped actor's 30.172-second explicit failure,
  and Linux parent-death cleanup passed. No timeout/invalid evidence became a
  partial completed result. Unit tests additionally cover idle-death monitoring,
  failed reaping blocking replacement, capability/PDF/runtime mismatch, bounded
  IPC, cancellation, renewed CPU budgets, kernel-default native-call watchdog,
  immediate unavailable status and preflight recovery without replaying jobs.
- CPU actual nine-page extraction matched committed 1.30.1 exactly, including
  unrounded scores/geometry (SHA256
  `65206830c992191bbfc9817e2c76330e90b3fba5f86d79bc94a12560776116a5`).
  Explicit idle 0, invalid settings, selected-env precedence over caller shell,
  credential retention and unchanged CPU defaults are covered.
- The normal GPU entrypoint/Gunicorn path passed startup preflight, authenticated
  HTTP health and job execution, idle Ready/cold state and completed-result lookup
  without model reload. Default worker logs contain both load and unload events.
  The existing production worker retained container ID
  `fa5d8e02004b3ec774471b6224ab788cfd8b63e6f8d07ba27bf5bc7ea35db1e8`
  and start time `2026-11-12T05:20:03.861108696Z` throughout acceptance.

CFM and the tested GPU image share compute contract
`3ea6e26d3f9f24c6150b00bf57d0bb18af6edc9ea373750b8db735abcf8f4b26`.
Publication/revision selection, readiness and review-reset dependencies are
unchanged. Production conference data, original PDFs and existing GPU service
configuration remain untouched. Temporary test models/PDFs/processes/images are
removed after acceptance; only reports are retained. The earlier ARM64 CPU
cold-comparison timeout limitation is unchanged, not fixed by GPU residency.

## CPU Worker Defaults Acceptance (1.30.1 / Method 8)

Standalone CPU defaults are now two pages/four threads per engine. Explicit
settings are retained, native/embedded CPU remains single-page, and CUDA remains
four pages. Models, 300 DPI, batch 6, comparison tolerances, execution deadlines,
publication selection and review dependencies are unchanged. State archive v8
is unchanged; deploy the same compute contract to CFM and workers together.

- Native macOS: 1,013 Django tests passed. Standalone discovery ran 109 tests
  with two opt-in skips; both opt-ins passed separately. All 123 JavaScript
  tests and check/migration-drift/docs/compile/whitespace gates passed.
- Isolated ARM64 Linux app Docker: 1,013 tests ran with eight platform skips
  (read-only Compose checks run on the host). The real two-conference updater
  exercised both entrypoints and preserved explicit one-page/two-thread worker
  env bytes and the running container values, tokens, DB/review state and
  Original/Corrected PDF bytes. The custom-header round-trip passed separately.
- Native actual OCR: both supplied 12-page PDFs plus nine generated boundary
  pages matched explicit sequential execution byte for byte, including raw
  strings, exact scores, word boxes, native geometry and final comparisons.
  Boundary pages include blank, rotated, two-column, added/deleted/modified
  text and unequal document lengths. Linux Docker's same nine-page boundary
  output also matched its own sequential baseline exactly. Neither comparison
  used a result-cache hit; cross-platform floats are not presumed identical.
- Linux ARM64's full-real-PDF sequential reference exceeded 300 seconds before
  finishing the pair (the first 12-page document took about 312 seconds). It
  was stopped and is **not** a passing full-pair parity or timeout result.
  Native full-pair parity and Linux boundary parity must not be presented as
  proof that every cold ARM64 comparison finishes inside the worker budget.
- The actual default CPU service path passed preflight at two pages/four
  threads, then returned explicit `comparison_failed` after 300.16 seconds for
  that same real pair, with no result/partial success. This verifies the
  unchanged parent timeout and failure boundary, **not** a successful full-pair
  Docker comparison. No warm result-cache test is claimed for the failed pair.
  The default does not guarantee every cold ARM64 document fits 300 seconds;
  neither timeout nor recognition quality was changed to hide this limitation.

The preceding isolated CPU study on 1.30.0 compared explicit one/two-page
execution with the same fixed Small models, four threads and no cache hits.
On Linux x86_64/i7-8700K limited to four physical cores/eight hardware threads,
the 24-page pair took 129.67/128.43 seconds sequentially versus 106.55/105.78
seconds with two pages (about 17.7% less elapsed time). Peak RSS rose from
1.31 to 2.47 GiB under the unchanged 6-GiB address-space/memory limits. The
M1 Pro study measured about 44.4% less elapsed time and RSS 2.44 to 4.14 GiB.
All full outputs were identical to their same-platform baseline. These are
throughput/identity measurements, not OCR accuracy or i7-4790 guarantees.
Four concurrent CPU pages failed the unchanged memory bound; the default is
not four and failures must never become partial success or a quality downgrade.

Tests use disposable source/data/images only. Production conferences and GPU
services are not restarted. GPU model residency/idle unloading is not implemented
in this release; model processes still exit with each job.

## Execution Configuration Acceptance (1.30.0 / Method 8)

- macOS: 1,010 Django tests passed. Standalone discovery: 108 tests passed with
  two opt-in skips; both opt-ins were also executed separately and passed.
  JavaScript: 123 tests passed. Django check, migration drift, documentation,
  compilation and whitespace gates passed. No schema migration is needed.
- Isolated ARM64 Linux app Docker: 1,010 Django tests passed with seven platform
  skips. Source was read-only, with disposable database/media and `/app/data`;
  startup fixtures required an executable `/tmp` tmpfs. No conference
  containers or volumes were used for this regression.
- Real updater acceptance used two disposable conferences and a shared CPU
  worker, exercised both entrypoints, and preserved DB/review state, tokens and
  Original/Corrected bytes. Actual Compose/container custom-header round-trip
  passed. Selected models are provisioned from resolved worker Compose, not
  inferred from a conference's environment.
- Two supplied 12-page PDFs (24 pages total) were recognized with the fixed
  Small models at 300 DPI/batch 6 on the Quadro RTX 5000 16-GiB host, inside a
  separately built GPU Docker image. CUDA page counts 1/3/4/5 all matched the
  pre-change sequential runtime's complete OCR/native geometry exactly: strings,
  exact serialized scores, word segmentation and coordinates. All three
  Medium-containing detector/recognizer combinations at one page also matched
  their same-model non-reduced sequential reference over all 24 pages; this is
  execution parity, not evidence that Medium always recognizes better.
- GPU correctness-only instrumentation checked all 525 actual recognition
  batches against NumPy indices and unrounded FP32 maxima, bit for bit. Another
  480 concurrent numeric cases passed; profiling assigned all reduction graph
  operators to CUDA. Exceptional values retain original NumPy handling.
- Generated boundary PDFs covered empty pages, 90-degree rotation, two columns,
  additions/removals/changed text and unequal document page counts. All nine
  pages matched legacy output exactly on CUDA four-page and CPU two-page
  dispatch. CPU's default sequential output also matched both real PDFs exactly.
  CPU Medium/Medium loaded and recognized a synthetic one-page PDF successfully.
- Actual CPU and GPU worker preflight, subprocess execution and complete warm
  pair-cache reuse passed on isolated boundary PDFs. An intentionally stalled
  CPU executable under `CFM_RAPIDOCR_TIMEOUT=30` failed explicitly at about
  30.6 seconds, without complete/partial success. Unit coverage includes bad
  settings/models, cache runtime invalidation, out-of-order completion, work
  bounds, injected failures and independent engine ownership.
- Pre-commit review additionally reproduced a transient runtime-cache
  invalidation failure and verified recovery: readiness stays unavailable,
  old completed evidence cannot be served, and a later preflight retries the
  invalidation before exposing an explicit Retry. No automatic recomputation
  or conference workflow mutation is introduced.

No OCR accuracy guarantee follows from parity. Medium/four-page or unusually
large pages can exceed RAM/VRAM; failures remain explicit, with no silent
downsampling, model substitution or partial results. Existing production workers
were not restarted. Temporary PDFs/models/results and disposable images are
removed after acceptance. Current defaults are 300 seconds; historical 240-second
measurements below describe the earlier execution policy, not today's limit.

## Earlier Regression Scope (1.29.0 / Method 8)

- Native: 992 Django tests, 105 JavaScript tests and standalone discovery
  (107 tests, two opt-in skips) passed. Django checks, migration drift,
  documentation validation, compilation and diff checks passed.
- Disposable ARM64 Linux Docker: 992 Django tests passed with six existing
  platform skips, no conference mounts or existing-service updates.
- Actual native OCR on a generated one-page PDF pair took 3.73 seconds cold and
  0.72 seconds for the complete validated cache. Warm reuse with a configured
  remote bypassed local consent and reported a pair hit. Removing only the
  pair cache retained document caches but required consent without starting OCR.
  Source hashes were unchanged. This is not a three-paper or GPU benchmark.
- Boundary coverage includes diacritic-only Info, adjacent true edits, canonical
  Unicode forms, case/script/overlay/digit exclusions, old-method rejection,
  wrong hashes/render evidence, stale runtime, partial/corrupt caches, gateway
  refusal, and completed evidence from another worker without route mutation
  or new submission. Publication and upload-revision binding regressions passed.
- Update CFM and workers together: method 8 has a new code contract; old results
  must be rebuilt, never relabelled. State archive remains v8. The earlier supplied
  three PDF pairs remain unavailable at their original Downloads paths.

## Earlier UI Regression Scope (1.28.0)

- Native: 984 Django tests, 105 JavaScript tests and standalone discovery
  (107 tests, two opt-in skips) passed. Django checks, migration drift,
  documentation validation, compilation and diff checks passed.
- Disposable ARM64 Linux Docker: 984 Django tests passed with the six existing
  platform skips. No conference volumes were mounted or existing services updated.
- Browser acceptance used production templates/assets with generated page images
  and finding payloads, no conference database. It exercised four leading
  unmatched pages, page five to after page one, selected long Evidence, scrolling,
  positioning modes, manual navigation and desktop/narrow layouts. Unit tests
  cover anchor edges, ambiguous regions, selection persistence, stale images and
  crop integrity. No new OCR accuracy benchmark is claimed.
- The three previously supplied PDF pairs were unavailable at their original
  Downloads paths; they were not rerun. Publication readers, signed file/version
  binding, inference and review-reset code are unchanged; existing byte-integrity
  and wrong-version regression remains in the full suite. Archive remains v8.

## Earlier Regression Scope (1.27.0)

- Native: 984 Django tests and 101 JavaScript tests passed. Standalone
  deployment discovery ran 107 tests (105 passed, two opt-in Docker skips).
  Django checks, migration drift, documentation validation, compilation and
  diff checks passed. No model migration or archive-format change is required.
- An isolated browser test used generated Original/Corrected PDFs, a temporary
  database and a real native CPU worker with the pinned OCR models. Remote
  availability was a disposable HTTP fixture, not actual GPU inference.
  Automatic and manual comparison paused without submitting local work while
  remote was unavailable. Explicit local confirmation created exactly one job
  with one attempt; returning to Formatting reused its completed evidence.
  Original/Corrected hashes and review state were unchanged.
- The Formatting service strip refreshed at ten-second intervals, remained
  independent of HTMX worklist replacement and fit desktop/narrow viewports.
  Remote recovery changed live availability without relabelling the completed
  result: it retained `Compared by Local · CPU`. A local worker's periodic
  preflight temporarily refused new computation without changing publication
  files or flags; an explicit retry after readiness completed normally.
- Unit coverage includes expired/cross-submission/configuration/file/revision
  consent rejection, existing local routes, completed-result reuse, gateway
  rejection, lost responses, observation-cache isolation, hidden-page pause and
  non-overlapping status requests. Publication/CrossCheck byte-integrity and
  wrong-version rejection remain in the full regression gate.
- Disposable ARM64 Linux Docker: the 984-test Django gate passed with six
  expected skips (five host Docker-CLI configuration checks and one filesystem
  case check). Host configuration coverage passed in native tests. The real
  Compose/container custom-header round-trip also passed with fake credentials.
  Django checks, migration drift, docs and compilation passed in the image.
  Only pinned OCR models were mounted, read-only; no conference data was mounted
  and the migration/startup entrypoint was bypassed. Generated containers and the
  uniquely tagged image were removed. No existing conference container, database
  or GPU-host service was changed. Earlier GPU measurements below are historical
  evidence, not a new GPU benchmark for this release.

## Earlier UI Regression Scope (1.26.1)

- Native: 961 Django tests passed, plus 95 JavaScript tests. Standalone
  deployment discovery ran 107 tests (105 passed, two opt-in Docker skips).
  Django checks, migration drift, documentation validation, compilation and
  diff checks passed. There is no model migration or archive-format change.
- Isolated browser acceptance used the production Django comparison page,
  signed comparison URLs and rendered generated two/three-page PDFs, with a
  synthetic OCR fixture. This is UI acceptance, not a new OCR accuracy benchmark.
  At 1440x1000 and 390x844, both crops contained actual image pixels, shared the
  same normalized center, kept approximately two-times display magnification
  and caused no horizontal overflow or implicit page/scroll changes.
- Unmarked regions, hidden marks and an added page without a page-map match
  still showed both displayed-page crops. Intentional image-request failure
  cleared the failed-side crop and showed its status; Retry restored only the
  selected page. Unit coverage also includes zero findings, unequal dimensions,
  corners, stale image/frame callbacks and keyboard/touch/dismissal behavior.
- Publication readers, source selection, snapshot/version binding, comparison
  result rules, readiness and reset code are unchanged. Full regression includes
  wrong-version rejection and publication/CrossCheck byte-integrity tests.
- Docker was not rerun for this UI-only release: the local daemon was stopped
  and its socket unavailable. No conference deployment or data was changed.

## Earlier Validated Scope (1.26.0)

- Native: 961 Django tests and 86 JavaScript tests passed. Standard standalone
  deployment/HTTP discovery ran 107 tests (105 passed); two explicit Docker acceptances are skipped by
  normal discovery and were run separately.
- Disposable ARM64 Linux Docker: the full Django gate passed with six expected
  skips (five host Docker-CLI configuration checks and one filesystem-case
  check). Host Compose checks and the actual header/container round-trip passed.
- A real HTTP fixture rejects the default Python-urllib browser signature and
  accepts the common CFM User-Agent. Health/upload/poll/result/retry requests
  retain gateway headers and worker authentication. Forced Cloudflare 1010,
  worker 401, redirects, non-JSON and network failures preserve safe reasons
  through preflight, without arbitrary prose, redirect URLs or credentials.
- Real updater acceptance used a disposable checkout, two generated conference
  projects and one shared CPU worker with unique images/networks/volumes.
  The original updater created both with one worker preparation; both CLI
  dry-runs listed real plans, all-project update succeeded, and a selected-project
  compatibility update left the other web container unchanged. Env/client-token
  bytes, full SQLite contents (including review flags/notes) and managed
  Original/Corrected file hashes were unchanged across updates. All generated
  containers/networks/volumes were removed; models were mounted read-only.
- Unit failures cover later invalid config before provisioning, prepared port
  conflicts, remote-only and env-less legacy instances, concurrent env edits,
  retention of live signing keys/worker connections omitted by env files,
  literal dollar signs/quotes/backslashes in signing keys and gateway headers,
  per-conference restoration before cutover and retaining recovery config after
  attempted cutover. Earlier completed updates are not rolled back.
- Publication selection, inference contract/models/DPI/timeouts and review-reset
  rules are unchanged. State archive remains v8; no DB migration is needed.

The operator independently confirmed Cloudflare 1010 for Python-urllib and
`HTTP 200 / ready / cuda` after a one-request application User-Agent override.
This release's automated tests used fake gateway credentials and local fixtures,
not the operator's Cloudflare account or production conferences. No new GPU OCR
benchmark or three-paper inference run is claimed; earlier measurements below
remain tied to their original release.

## Earlier Validated Scope (1.25.1)

- Native: 954 Django tests and 86 JavaScript tests passed. The standalone
  deployment/configuration suite passed 85 tests; its additional opt-in Docker
  test was run separately and passed.
- Disposable ARM64 Linux Docker: 954 Django tests passed with six expected
  skips (five host Docker-CLI configuration checks and one filesystem-case
  check). Host Compose checks passed. Only pinned models were mounted read-only;
  no conference database/files were mounted and the migration entrypoint was
  bypassed.
- Real loopback HTTP exercised custom gateway headers alongside the worker
  token through startup checks, upload, polling, completed retrieval and explicit
  retry. Header rotation retained the same job without a duplicate upload.
  Gateway denial/login redirects did not follow redirects or select local.
  Local endpoints did not receive remote credentials.
- A real Compose-to-Linux-container round-trip verified literal dollars,
  variable-looking strings, hashes, apostrophes, quotes and backslashes in both
  initial and updater-generated env files. The test used fake credentials,
  unique projects and no conference volumes; its containers/networks were removed.
- Parser bounds, invalid/duplicate/reserved headers, secret-safe errors,
  env preservation/redaction and the stdlib-only deployment import boundary
  passed. Publication/export, wrong-version and review-state regression passed.
  There is no DB/schema/archive change; State archive remains v8.

This round did not contact an actual Cloudflare account or rerun GPU OCR against
the three real papers: headers change HTTP authentication, not inference or
publication selection. GPU measurements below belong to the earlier release.

## Earlier Validated Scope (1.25.0)

- Native: 945 Django tests, 77 standalone deployment/provisioning tests and
  86 JavaScript tests passed.
- ARM64 Docker: 945 Django tests passed with five expected skips: four host
  Docker-CLI configuration tests and one filesystem-case test. The host ran
  the Compose checks. Final 77 standalone tests also passed in Linux.
- Actual first setup downloaded and checksum-verified both pinned models,
  generated private credentials and started the CPU worker/conference. Repeated
  setup preserved config/token bytes and reused an unchanged worker. Two
  conferences shared one CPU worker with distinct tokens.
- Wrong-project enrollments and conflicting settings failed without replacement.
  A busy worker refused enrollment/restart and its original job completed.
  Source-fingerprint updates and private deployment audit records were verified.
- Unified GPU setup ran on the actual GPU host in Docker; repeated deployment
  preserved settings/token/enrollment hashes. No conference database was used.
- Publication byte-integrity, stale/wrong evidence rejection, readiness and review
  state regression passed. No publication/inference policy was changed.
- Isolated containers, volumes, networks, private credentials and transferred
  PDF copies were removed. Existing conference data and GPU-host open-webui were
  preserved. Shared Docker build cache was not pruned.

These checks are not Windows hardware acceptance or an i7-4790 Ubuntu benchmark.
The idle-worker guard is not an atomic drain: updates require a maintenance
window without new submissions. Passing regression does not guarantee every
PDF is recognized correctly or every future job fits the deadline.

## GPU Full-Document Docker Acceptance

The three full-PDF pairs below ran inside an isolated Linux x86_64 Docker GPU
worker on a Quadro RTX 5000 (16 GiB), driver 595.99.02. This was not native-host
Python inference. Cold jobs used authenticated worker HTTP; warm-cache work
used fresh production job subprocesses. The container had an 8-GiB host-RAM
limit, no swap, fixed models/300 DPI and unchanged production deadlines.

The original Quadro RTX 5000 smoke acceptance was extended on 2026-11-10
(2026-11-11 UTC) to all three supplied full-PDF pairs, with actual CUDA,
300 DPI, the same pinned models, an 8-GiB host-memory cap and no swap:

| Full PDF pair | Pages before/after | Cold HTTP job | Warm execution (pair cache) | Completed-result replay |
| --- | --- | --- | --- | --- |
| 1 | 12 / 12 | 51.796 s | 3.417 s | 0.025 s |
| 2 | 14 / 10 | 43.724 s | 2.904 s | 0.020 s |
| 3 | 10 / 10 | 41.223 s | 2.841 s | 0.013 s |

All three cold jobs completed within the unchanged 240-second OCR limit and
reported no pair/document cache hits. Warm execution used a new production job
subprocess with `pair_hit=true`, not a completed HTTP receipt; its full evidence
matched cold execution exactly. Input/render hashes were validated. These are
measurements for this hardware/runtime/workload, not a guarantee for every PDF
or a claim that GPU evidence is byte-identical to CPU evidence. An earlier
generated edit/cache/container-recreation acceptance also passed. The new full
PDFs were transferred only into an isolated authorized workspace/container, not
to the host's existing services; no conference database or production credentials
were used.

A separate isolated recognition stall exercised the real unchanged 240-second
inner OCR deadline: it returned `comparison_failed` after 240.893 seconds with
no completed evidence. Normal CUDA work then succeeded in the same persisted
test namespace from a fresh container in 3.322 seconds. This was deliberate
fault injection, not a claim that any of the three real papers timed out.

Unified deployment acceptance also exercised actual automatic downloads,
private credential/enrollment generation, repeated CPU/GPU setup with unchanged
settings/token hashes, two conferences sharing one CPU worker, busy-worker
refusal with the original job completing, and the full regression gate.
The Mac host could reach the temporary GPU LAN listener directly, but its Docker
Desktop container received connection refusals to that same IP/port. No firewall
or daemon settings were changed. Authenticated end-to-end CFM-to-CUDA routing
passed through a temporary loopback SSH gateway; stopping only the test GPU
worker then selected local CPU for a new job. This proves routing/fallback, not
direct LAN reachability from every Docker installation. Verify the chosen GPU
endpoint from inside the actual conference container; a host-only curl is not
sufficient. Never describe an unreachable remote using CPU fallback as GPU use.


## Earlier Shared Worker And CPU Validation (1.23.0-1.24.0)

**1.24.0 shared service acceptance:** 910 Django tests and 86 JavaScript tests
passed, plus Django check, migration drift, documentation validation, compilation
and diff checks. The native HTTP worker ran the three supplied PDF pairs and
12 generated controls twice (first execution and completed-job reuse): all 30
full result JSONs matched the frozen pre-change native results. First execution
of the real pairs took 117.27, 88.86 and 84.83 seconds. Source fixture hashes
were unchanged; no conference database was configured for compute acceptance.
Corrupt input and the known native rotated-two-blank-pages failure returned
unavailable, never a clean result.

A disposable ARM64 Linux worker and separate client container exercised the
shared bridge network, authenticated readiness and two-client isolation. All
12 generated controls matched fresh embedded execution on the same Linux
runtime, including full JSON, both before and after worker-container recreation.
Completed HTTP results survived recreation; files/models were mounted read-only
and all worker storage was independent of conference volumes. Browser acceptance
used a disposable CFM database: unavailable-worker messaging, manual Build,
both page images, text/reference marks and inspection details worked with no
browser console errors. Publication regression tests verified unchanged ZIP/
CrossCheck bytes and review/readiness state, including stale/wrong result rejection.

Actual GPU smoke on a Quadro RTX 5000 used CUDA first in both ONNX sessions,
TF32 disabled, an 8-GiB memory limit and no swap. A generated alpha/beta edit
took 3.53 seconds cold and 1.77 seconds through the inference cache, with identical
result hashes. Recreated service retrieval preserved runtime identity and exact
HTTP response bytes. Three full articles were **not** tested on that GPU in this
round. After the final readiness/TTL fixes, the updated service modules were
verified by hash; GET-only retrieval and another container recreation preserved
the exact response, runtime identity and ledger rows without re-running jobs.
CPU/GPU evidence caches remain separate.

These service tests do not overturn the full-document ARM64 CPU timeout record
below. The per-job 240-second OCR bound is unchanged; a shared queue makes work
recoverable and isolated, not intrinsically faster. Native results and small GPU
controls must not be presented as a full-article Docker/GPU performance pass.

**Earlier CPU performance acceptance:**

The 1.23.0 cache follow-up separately passed a network-disabled ARM64 Linux
single-page control: pre-change 20.02 seconds, new cold 19.48 seconds, warm pair
0.91 seconds, Original-only reuse 10.24 seconds. All three new results exactly
matched that Linux pre-change worker's full JSON. It used read-only code/model/
fixture mounts and a disposable QA output directory, with no conference data
mount. This is cache/runtime smoke evidence, not a new full-document availability
pass or an Intel Ubuntu benchmark.

The isolated ARM64 Docker Desktop test used 8 allocated CPUs, approximately
8 GiB RAM, Python 3.12.16, PyMuPDF 1.27.2.3, RapidOCR 3.10.0 and ONNX Runtime
1.31.1. **Full-document comparison availability did not pass.** All three real
PDF pairs timed out at the unchanged 240-second wall limit at each of 4, 6 and
8 threads (nine sequential runs). Their page counts were 12/12, 14/10 and
10/10. No timed-out run was accepted as a complete result or substituted a PDF.

The nine representative-page recognitions retained identical full OCR outputs
across thread settings; 6/8 threads improved page timing versus 4 but did not
resolve the full-document limit. All 24 fresh authored edge controls passed
(eight at each setting). Both Linux ARM64 and emulated x86-64 images passed
809 Django tests with one existing filesystem-specific skip. Host tests passed
all 809; 86 JavaScript tests passed. Browser acceptance included a fresh Linux
one-page result and separately labelled dense, precomputed Mac OCR UI fixtures;
the latter are not Linux full-document engine acceptance.

The real 240-second stalled-worker rejection, worker reaping and service
recovery passed. A separate test-only 3-second Nginx deadline exercised the
fallback response; production proxy/Gunicorn deadlines stayed 300 seconds.
Model/input failure paths, signed-source HTTP rejection, unchanged QA workflow
state and exact corrected PDF bytes in both CrossCheck ZIP scopes were verified.
Unreviewed QA records still blocked final publication export.

An emulated x86-64 real one-page pair passed the production worker including
Linux resource limits. It proves that smoke case only, not complete-article
performance on the intended i7-4790 Ubuntu host, which was not accessed. Do not
describe these results as a full Docker performance pass. Before accepting this
comparison workload, resolve/validate its execution-time lifecycle without
lowering resolution, replacing models or weakening publication checks.

## Earlier Deployment Preflight Validation (1.24.1)

Disposable ARM64 Docker projects verified fresh worker/conference startup,
missing-model and wrong-token rejection, credential-only worker updates,
conference update/rebuild, verified raw backup, and bind-to-volume migration.
Failed candidate checks preserved the running container IDs and conference-data
hashes. Successful standalone preflight (including Django checks) left an empty
conference volume empty. Explicit remote-only configuration also authenticated
successfully. A generated alpha/beta PDF pair completed real HTTP OCR with the
expected ordered input hashes. Existing conference containers/data were not used.

Native regression: 945 Django tests and 19 standalone deployment tests passed;
86 JavaScript tests passed. The clean Linux image passed the same 945-test suite
with five expected skips (four host Docker-CLI config checks and one
case-insensitive-filesystem test), plus the 19 deployment tests. The four Compose
checks passed on the host. Windows batch coverage is static only, not Windows
machine acceptance; this deployment pass did not repeat GPU inference or the
three full real-paper comparisons. Prior CPU/GPU evidence and the unchanged
240-second first-time CPU limit remain described above.
