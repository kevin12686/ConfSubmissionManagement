# Docker Guide

## PDF Reader Static Assets

The shared PDF.js reader runs locally in the browser. The image collects its
pinned `.mjs` display/worker/viewer modules, CMaps, fonts, WASM decoders and ICC
resources through the existing `collectstatic` step. There is no Node service
or runtime npm install. Nginx's static location includes the standard MIME map
and explicitly adds `.mjs` as `application/javascript`, retaining CSS, font and
WASM types. Do not remove this module mapping or apply it to media downloads.

After an update, verify a PDF opens in the shared reader, including module
worker loading, not just that the HTML page returns 200. Missing static assets
or incorrect module MIME types produce a visible reader failure; Open directly
and Download PDF continue to use the original PDF endpoint. Publication file
selection and export never depend on PDF.js or its static cache.

Use this guide to create, update, back up, migrate, or recover a Docker
conference instance.

Docker is intended for trusted local or LAN operation. It does not add
authentication or turn Conference Final Manager into a hardened public
service. Editorial workflow belongs in the [Operator Guide](operator_guide.md);
symptom-based recovery belongs in [Troubleshooting](troubleshooting.md).

## Runtime Model

Each conference uses an independent Compose project:

- `web` runs Django and Gunicorn against SQLite;
- `proxy` is the only published endpoint and runs Nginx;
- `sms_data` stores the conference database and managed files;
- `sms_static` stores rebuildable collected static assets;
- `sms_gateway_state` stores short-lived update, backup, migration, and restart
  status.

Nginx serves `/static/`, serves `/media/` from a read-only view of `sms_data`,
and proxies all other requests to `web`. Keep one Gunicorn worker with SQLite.
`SMS_DEBUG` controls Django diagnostics, not whether static or media files load.

## Create An Instance

Create one environment file per conference:

```bash
cp .env.example .env.conference-a
docker compose --env-file .env.conference-a up -d --build
```

The default endpoint is <http://127.0.0.1:8000/>.

Every environment file must define a unique, stable
`COMPOSE_PROJECT_NAME`. It is the instance identity used by Compose and the
management scripts. Do not infer project ownership from the environment
filename.

Important settings:

| Setting | Purpose |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | Stable conference instance identity |
| `SMS_PORT` | Published host port |
| `SMS_DATA_DIR` | Verified raw host mirror destination |
| `SMS_BIND_HOST` | Bind address; keep `127.0.0.1` unless trusted LAN access is required |
| `SMS_ALLOWED_HOSTS` | Allowed LAN hostname or IP when exposed beyond localhost |
| `SMS_DEBUG` | Django diagnostics; use `0` for normal operation |
| `SMS_PROXY_MAX_BODY_SIZE` | Finite Nginx upload limit |
| `SMS_WEB_WORKERS` | Gunicorn workers; keep `1` with SQLite |
| `SMS_WEB_THREADS` | Gunicorn request threads |
| `SMS_WEB_TIMEOUT` | Gunicorn request timeout |

Use a different project name, port, and data folder for every conference.
Sharing any of them risks sending an operator to the wrong conference or mixing
data ownership.

## PDF Comparison Runtime

Method-7 comparison uses local CPU inference with `rapidocr==3.10.0`,
`onnxruntime==1.31.1` and the approved PP-OCRv6 small detection/recognition model
pair. Rebuild the Linux image with the pinned requirements and verify that both
packages import in the container; a host virtual environment is not used by
Docker and does not prove container compatibility.

Provision models explicitly before serving comparisons. On a host with the
application environment, use the
[model import command](developer_guide.md#pdf-comparison-runtime) to validate and
copy the approved files into the checkout's `runtime/rapidocr-models` directory.
The default runtime lookup is `BASE_DIR/runtime/rapidocr-models`. Existing Compose
already mounts `.:/app`, so those host files are visible at
`/app/runtime/rapidocr-models` inside `web`. **No additional model mount,
environment variable or Compose change is needed for this default setup.**
The directory is outside the separate `/app/data` named-volume mount.

Alternatively run `python manage.py prepare_rapidocr_models` in a one-off `web`
container, with an absolute path to the approved source visible in that container
and `--destination /app/runtime/rapidocr-models`. This writes the default model
directory through the existing app mount. The command imports local files only;
it never downloads them. The default is a lookup location, not automatic
provisioning: the validated files must actually exist there.

For an **optional external model directory**, the running `web` container needs
both:

- A read-only bind mount of the validated host model directory at a stable
  absolute container path, for example `/opt/cfm-ocr-models`.
- `CFM_RAPIDOCR_MODEL_DIR=/opt/cfm-ocr-models` in the **container** environment.

Configure these only when choosing an external directory, in the deployment's
Compose service definition before updating the instance. A host-only environment
value or a line in an env file is not
enough unless Compose passes it through; the value must name the container path,
not the host path. Keep any deployment override attached to every Compose
operation, including update/recreate, and inspect the rendered configuration
before cutover. The automatic updater does not infer arbitrary extra mounts or
environment variables from a model directory. An explicit override takes
precedence over the default and does not fall back if its files are invalid.

Keep models separate from `sms_data`, publication/media paths and report cleanup.
System State v8 does not provision models or archive machine-level OCR settings;
restore/migration needs this setup again. Requests never download models or use
cloud inference. Missing files, failed checksums or unsupported/missing runtime
packages return Comparison unavailable without a native-text fallback.

After setup, verify an actual disposable comparison through the browser while
offline, including nonblank page crops and an intentional visible edit. Check
worker/time limits through the proxy; do not disable timeouts to force large PDFs
through. Continue manual figure, table, layout and full-math review. Broad visual
exceptions can deliberately tolerate real similar-character edits, so a zero
count is not release approval. Record Linux/Docker verification separately from
host and research results.

### CPU Allocation And Thread Tuning

Docker Desktop's CPU allocation limits what the Linux VM can use. Increasing
the VM allocation does not automatically change CFM's OCR thread count. Set
`CFM_RAPIDOCR_THREADS` in the conference environment file (default `4`, valid
`1`-`8`, within the visible CPU count); Compose passes it to `web`, and the
unified updater retains it during recreation. Preview the update as usual.
This is a machine/runtime option, not an AppSetting or System State field.

Compare 4/6/8 sequentially, not as competing OCR jobs. Check full text, boxes,
scores, locations and authored edit controls as well as timing. Logical threads
are not physical cores, and ARM64 Docker Desktop timing is not a performance
guarantee for native Intel/AMD Linux. Do not assume maximum threads is fastest.
RAM increases do not fix a CPU-bound workload when there is already sufficient
memory. Models, 300 DPI and the 240-second wall limit remain unchanged; Linux
CPU-time accounting permits all eight supported threads within that wall budget.

The image build excludes `data/`, `runtime/`, `output/` and `.playwright-cli/`.
Research PDFs, screenshots and local models must not be baked into the image.
Provision models separately as described above. A passing image build or unit
suite is not evidence that full PDFs complete within the runtime limit.

### Validation Record: 2026-11-10

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

## Update Existing Instances

After changing code or any `.env` / `.env.*` setting, preview the complete
update plan:

```bash
python3 scripts/update_docker_instances.py --dry-run
python3 scripts/update_docker_instances.py
```

The updater:

1. discovers maintained conference environment files;
2. renders and validates every desired Compose configuration;
3. rejects duplicate project names, overlapping endpoints, shared data
   folders, foreign checkout ownership, and changes to an existing
   `SMS_DATA_DIR`;
4. builds before cutover;
5. replaces only `web`, and reloads or recreates `proxy` only when required;
6. waits for readiness;
7. verifies the loaded Nginx Host directive, one static asset, and a
   non-mutating same-origin CSRF POST.

New environment files are reported but not started. After reviewing the dry
run, create them explicitly:

```bash
python3 scripts/update_docker_instances.py --create-missing
```

Use repeatable `--project NAME` options to limit an operation. Secrets are
masked in plans, and management scripts never rewrite operator environment
files.

## Back Up Docker Data

Refresh the raw host mirror for every current instance:

```bash
python3 scripts/backup_docker_instances.py --dry-run
python3 scripts/backup_docker_instances.py
```

The backup performs an online pre-copy, briefly stops only `web` for the final
consistent sync, verifies file SHA-256 values and SQLite integrity, promotes
the mirror, and retains the previous complete mirror. Nginx remains on the
public port and shows the current backup phase.

The raw mirror contains directly usable `db.sqlite3` and managed files. It is
for immediate operational rollback. A System State ZIP remains the portable,
versioned application backup and should also be downloaded before major
changes or handoff.

`sms_static` and `sms_gateway_state` are not conference data and are not backed
up.

### Scheduling Backups

The data scripts support repeatable `--project`, `--dry-run`, and
`--stop-timeout` options. When using Windows Task Scheduler:

- set the repository folder as **Start in**;
- use the installed Python launcher or Python executable as the program;
- use `scripts\backup_docker_instances.py` as the argument;
- run under an account that can access Docker Desktop.

Review `.sms-docker-backup-history.jsonl` regularly; a scheduled task returning
success is not a substitute for checking the verified per-project result.

## Migrate A Legacy Bind Mount

Older bind-mounted instances must move to the project-scoped named data volume:

```bash
python3 scripts/migrate_docker_data_volumes.py --dry-run
python3 scripts/migrate_docker_data_volumes.py
```

The migration builds the current image, performs a verified online pre-copy,
stops `web` for the final sync, validates SQLite, starts the named-volume web
service, and briefly recreates `proxy` so its read-only data mount follows the
new volume. The original host folder is preserved.

## Recover A Legacy Instance

Use `scripts/rebuild_docker_instances.py` only when a running legacy instance
does not have a maintained environment file:

```bash
python3 scripts/rebuild_docker_instances.py --dry-run
python3 scripts/rebuild_docker_instances.py
```

This recovery path treats the running containers as the source of effective
ports, settings, and mount type. It intentionally does not apply later edits
from an environment file.

For ordinary maintained instances, use
`scripts/update_docker_instances.py` instead.

## Roll Back To The Host Mirror

Stop any active backup or migration, then apply the bind override with the same
environment file and project identity:

```bash
docker compose -f docker-compose.yml -f docker-compose.bind.yml \
  --env-file .env.conference-a -p sms-conf-a up -d --build
```

The override mounts `SMS_DATA_DIR` at `/app/data` for `web` and as read-only for
`proxy`. Return to the normal Compose configuration after the named-volume
problem is resolved.

## Gateway And Download Behavior

Nginx stays available while `web` restarts. Its fallback page shows fresh
backup, migration, update, or restart status; otherwise it reports a generic
outage. It checks readiness and returns to a new Dashboard GET after recovery.
It never replays an interrupted POST, upload, import, or export.

The proxy must preserve the browser-visible `Host` header including a
non-default port. Do not replace `$http_host` with `$host`, disable CSRF, add
`csrf_exempt`, or trust arbitrary origins to work around a proxy
misconfiguration.

Dynamic responses use bounded Nginx response buffering. Large ZIPs may spill to
request-scoped temporary storage while a slow client downloads them. This is
not `proxy_cache`: data is not reused between requests, and publication file
selection remains entirely in Django.

## Operation Lock And Recovery Files

Update, rebuild, migration, and raw backup share:

```text
runtime/.docker-data-operation.lock
```

Do not remove the lock while a management script is running. Locks older than
12 hours are treated as stale. A `.backup-swap` directory indicates
interrupted mirror promotion; preserve both it and the current mirror when both
exist, then inspect the backup history before retrying.

Per-project results are recorded in
`.sms-docker-backup-history.jsonl` beside the configured data mirrors.

## Safety Checklist

For the optional CrossCheck Service, Compose defines
`host.docker.internal:host-gateway`. Use `http://host.docker.internal:18765`
when the service runs on the Docker host and listens on an interface reachable
from Docker. A host-loopback-only service may need its private bind address
changed. `127.0.0.1` in Settings points to the CFM container. A service on another
machine needs its reachable private address. CFM needs no additional public port.

Each CFM instance keeps its own folder binding and job history. A shared
CrossCheck service permits one job at a time; a busy response does not retry.
Optional `SMS_CROSSCHECK_WEB_USERNAME/PASSWORD` and
`SMS_CROSSCHECK_IEEE_USERNAME/PASSWORD` override UI secrets when nonempty.
Usually leave them blank to use the service's default IEEE account. UI secrets
live in the conference volume under `data/secrets/`; System State excludes them,
but raw Docker backups include them. Protect env files and raw mirrors as
credentials. Service WebLogin uses HTTP Basic when enabled; a 401 identifies
unsupported or rejected authentication.

- Run every disruptive command with `--dry-run` first when supported.
- Confirm project name, endpoint, and `SMS_DATA_DIR` before applying.
- Keep `SMS_DEBUG=0` for normal operation.
- Keep one Gunicorn worker with SQLite.
- Download a System State ZIP before major maintenance.
- Verify the Dashboard and one state-changing CSRF-protected action after
  maintenance.
- Never run `docker compose down -v` for a conference instance. `-v` deletes
  the named data volume.
