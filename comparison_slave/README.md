# Comparison Worker Protocol

A private shared HTTP compute service, not a conference Django app.
For deployment/update commands use [Deployment](../docs/deployment.md);
configuration/backup ownership is in [Operations](../docs/operations.md).
Do not mount conference data or databases into this service.

## Responsibilities

CFM selects and freeze-reads the PDFs. The worker receives exact bytes and hashes,
runs the shared bounded comparison engine and returns evidence. It cannot choose
publication files, change reviews or assemble publication ZIPs.

`entrypoint.py` validates configuration/runtime before serving.
One scheduler runs one heavy namespace job at a time. Per-job subprocesses own
namespace settings/signing/cache. CUDA optionally reuses one supervised inference
actor sharing weights only. Runtime settings/defaults are in the Operations table;
algorithm/result constraints are in [PDF Comparison](../docs/pdf_comparison.md).

## Authentication And Health

Every route requires `Authorization: Bearer <token>`. The token selects a
namespace from the private mode-0600 clients JSON; callers cannot select another
namespace by URL. Up to 32 distinct namespaces/tokens are supported.
Responses are bounded JSON with `Cache-Control: no-store`.

`GET /v1/health` reports protocol 1, code contract, backend (cpu/cuda),
state (ready/busy/not_ready), safe error category, packaged worker version and
allowlisted validated settings. No paths, tokens or arbitrary environment values.

Active work reports busy. Queued work reports busy only while runtime readiness
is valid. Successful preflight caches readiness for 60 s; failures back off for 30 s.
Health can request scheduler revalidation, not perform OCR in the HTTP handler.
GPU cold metadata validation must not reload idle model pools.
Settings may be unavailable until validation; release labels are observational,
not a substitute for code/runtime compatibility.

## Job API

| Request | Behavior |
| --- | --- |
| `GET /v1/jobs/<key>` | Namespace-scoped state/evidence lookup |
| `POST /v1/jobs/<key>` | Idempotent submit: 202 queued/running, 200 existing terminal |
| Same POST plus `X-CFM-Retry-Failed: 1` | Explicitly requeue failed job only, at most 3 total attempts; no TTL renewal |

POST uses `application/octet-stream`, mandatory Content-Length,
`X-CFM-Contract`, `X-CFM-Before-SHA256` and `X-CFM-After-SHA256`.
Body is `struct.pack('>QQ', len(before), len(after))` then both byte sequences;
each PDF is nonempty and at most 64 MiB. Hashes and
`comparison_protocol.job_key(hashes, contract)` must match. No chunked upload.

Job replies include protocol, contract, key, ordered hashes, state and safe error.
Completed replies additionally contain result, diagnostics and per-side page
render hashes. Retrieval requires unexpired evidence and current validated runtime;
CFM independently checks evidence/render hashes against its snapshots.

| HTTP | Meaning |
| --- | --- |
| 400 | Invalid framing/hashes |
| 401 | Authorization failure |
| 404 | Missing or other-namespace job |
| 409 | Contract conflict or retry limit |
| 411 / 413 / 415 | Missing length / oversize / unsupported media |
| 429 | Capacity/concurrent-upload bound |
| 503 | Evidence/runtime temporarily unavailable |

429/503 include `Retry-After: 5`. Fixed error codes, not raw subprocess stderr,
are returned. Failed jobs remain terminal until an explicit user retry, not an
automatic polling loop. An uncertain POST must be reconciled on the same worker,
not duplicated locally/remotely.

## Queue, Retention And Recovery

| Bound | Limit |
| --- | --- |
| Outstanding | 4 per namespace, 32 total |
| Dispatch | Least-recently-served namespace, FIFO inside it |
| Retained jobs | 128 |
| Logical input/result reservation | 512 MiB; SQLite/journal overhead is additional |
| Job TTL | 2 hours from original creation; retries do not renew |
| Signed cache per namespace | 7 days, 512 entries, 512 MiB |
| Namespace directories per root lifetime | 32; removed names are never reassigned |

Terminal transitions discard input blobs. Minute-granularity cleanup and lookup
checks expire jobs; expired queued work cannot start. Already running work remains
visible/idempotent until bounded execution ends, then follows its original expiry.
Dormant caches are pruned too. Cleanup never follows symlinks/arbitrary trees.

A durable SQLite ledger retains jobs and transitions. Startup marks previously
running jobs interrupted/failed; queued jobs survive. A root lock and watchdog
prevent concurrent/orphan dispatch. The supervisor kills/reaps the process group
on timeout; failure cannot produce partial completed evidence. Explicit retries,
web restarts and cache reads must preserve original creation time.

GPU broker IPC authenticates/bounds metadata/PDF/raw OCR, validates byte/runtime
identity and replaces a failed actor only after reaping it. Idle unload releases
weights/CUDA memory, not another conference's persisted history. Health and cache
reads do not keep the actor loaded.

## Runtime And Storage

Use one Gunicorn process with 4 threads and worker-owned private storage.
`CFM_SLAVE_ROOT` is mode 0700; clients/signing secret files mode 0600.
The deployer supplies container paths and a persistent worker volume.
`CFM_SLAVE_SECRET`, when set, is a secret **file path**, not secret text;
otherwise the worker creates a stable signing secret in its root.

The child uses minimal Django settings without application models/databases.
Approved model checks/provider/fingerprint and normal OCR/time/memory bounds
remain mandatory. Configure proxy finite upload limits for two 64 MiB PDFs plus
16-byte framing. Do not expose the origin to the public internet or log bodies,
Authorization, custom gateway headers or raw errors.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_worker_deployment.py'
.venv/bin/python -m unittest submissions.tests.test_comparison_slave
```

These mock inference and use isolated storage/HTTP; they do not prove CUDA
inference. Run the [acceptance procedure](../docs/editorial_acceptance_runbook.md#docker-and-worker-lifecycle)
for actual Docker/GPU changes. Worker data is not System State data.
