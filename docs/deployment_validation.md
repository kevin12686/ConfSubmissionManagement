# Deployment Validation

This document owns measured deployment evidence, not installation instructions.
Use the [Docker Guide](docker_guide.md) for current commands, the
[Acceptance Runbook](editorial_acceptance_runbook.md) for reproducible release
checks, and [PDF Comparison](pdf_comparison.md) for the comparison contract.
Historical counts and results below apply only to their labelled test round.

## Latest Validated Scope (1.26.0)

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
