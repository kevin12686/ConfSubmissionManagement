# Docker Guide

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

## Start Here

| Task | Section |
| --- | --- |
| Create a conference and shared CPU worker | [Unified Deployment](#unified-deployment) |
| Add a remote GPU worker | [Unified Deployment](#unified-deployment) |
| Update an existing deployment | [Update Existing Instances](#update-existing-instances) |
| Inspect runtime settings or deploy manually | [PDF Comparison Runtime](#pdf-comparison-runtime) |
| Back up conference data | [Back Up Docker Data](#back-up-docker-data) |
| Migrate or recover older storage | [Migrate A Legacy Bind Mount](#migrate-a-legacy-bind-mount) |
| Review tested hardware, timing and limitations | [Deployment Validation](deployment_validation.md) |

First setup uses `scripts/deploy.py`. Normal updates use the existing
`scripts/update_docker_instances.py`; it and `deploy.py update` share the same
complete orchestration workflow. Worker-only lifecycle commands remain separate.

## Unified Deployment

`scripts/deploy.py` is the operator entry point. It runs on the Docker host
with Python's standard library; a conference virtual environment/database is
not needed to prepare a worker. Docker/Compose and, for GPU, a working NVIDIA
driver/Container Toolkit remain host prerequisites. The script does not install
drivers, change firewall rules, open public bind addresses or log in to SSH.

The deployer automatically provisions missing pinned models, creates private
per-conference worker credentials and delegates validated container lifecycle
work to the existing worker/conference scripts. It does not change publication
selection, scope, review flags, upload files, OCR thresholds or deadlines.

Existing project names, data paths, secrets, client credentials and explicit
settings are preserved. Conflicting or corrupt existing values cause an
actionable error instead of replacement. Generated environment defaults are
filled only when absent. Dry-run does not download, write settings, generate
credentials or start/stop containers. Update previews render actual redacted
Compose plans and local-worker/model requirements using temporary private env
staging only; those temporary files are removed. They do not run live HTTP,
model-session or image-build checks. Offline mode requires the pinned models
already present; an invalid existing model is never automatically replaced.
Deployment requests/results are appended without credentials to private
`runtime/deployment.log`; this host-maintenance log is separate from the
conference Audit Log and is not System State data. Failure to record a request
prevents deployment; a success-log failure explicitly reports that completed
deployment side effects were not rolled back.
Running workers must belong to this checkout; an ambiguous or foreign Compose
project is rejected rather than automatically replaced. To use an externally
managed worker, retain its explicit URL/token and manage its lifecycle from
the owning checkout.

Remote GPU enrollment is explicit and per conference, not an unauthenticated
registration API. The GPU host exports a private enrollment file for a specific
conference project; the CFM host imports that trusted file once. Different
conference projects must not share enrollment credentials. Transfer enrollment
files through a trusted channel such as SSH, keep mode 0600, and do not commit
or include them in conference backups. No manual token editing is necessary.

Worker updates require a maintenance window without new comparison submissions.
The deployer checks for busy work before recreation, but this is not a distributed
atomic drain: a newly arriving request can race an idle check. Do not advertise
an unconditional no-interruption guarantee. Failed or interrupted comparisons
never change publication files or review decisions.

First conference (this also prepares the shared CPU worker):

```bash
python3 scripts/deploy.py conference --name conference-a --port 8000 --dry-run
python3 scripts/deploy.py conference --name conference-a --port 8000
python3 scripts/deploy.py conference --name conference-b --port 8001
```

`--name` is the actual Compose project identity. New env files are named
`.env.<name>`; existing managed env files with that project are discovered and
reused, even when their filenames differ. If needed, specify the existing file
with `--env-file .env.conference-a`. Never rename a project's identity to upgrade.
Repeated setup preserves tokens and reuses an unchanged healthy shared worker.
Worker-source/configuration changes are tracked in private deployment metadata
and require a validated update before the conference is updated.

On the GPU host, set a private LAN address explicitly and name the conference
projects allowed to use it (example IP only):

```bash
python3 scripts/deploy.py worker --gpu --bind-host 192.168.4.90 \
  --client conference-a --client conference-b
```

This creates private `runtime/comparison-slave/enrollments/conference-a.json`
and `conference-b.json`, containing separate credentials. Copy each needed file
to the CFM host through a trusted channel, retain mode 0600, then enroll once:

```bash
python3 scripts/deploy.py conference --name conference-a \
  --remote-enrollment /private/path/conference-a.json
```

The enrollment namespace must match the conference's Compose project name.
Remote settings already present must agree; they are not silently replaced.
Use `--endpoint http://private-host:8091` when the advertised URL differs from
the bind address; wildcard bind addresses cannot be advertised as endpoints.
Remote enrollment files are not general administrator keys and cannot register
new clients through the HTTP API. Adding another remote conference is an explicit
trusted GPU-host `worker --client <project>` operation, not manual token editing.

Later, on each host run the appropriate unified command again. On the CFM host:

```bash
python3 scripts/update_docker_instances.py --dry-run
python3 scripts/update_docker_instances.py
```

Use `--project conference-a` to limit conference updates, or `--offline` to
require models already installed. `deploy.py update` accepts the same update
options and uses this identical workflow. New env-declared conferences are
shown but skipped unless `--create-missing` is supplied. Do not mix the worker's
Compose file with a conference Compose invocation.

### Remote Gateway Authentication

Configure optional headers once in each CFM conference's private `.env.<name>`
file, not the GPU worker env or enrollment file. Leave the value blank when no
gateway authentication is needed. Existing settings are never auto-overwritten.

```dotenv
CFM_COMPARISON_REMOTE_HEADERS='{"CF-Access-Client-Id":"REPLACE_ME","CF-Access-Client-Secret":"REPLACE_ME"}'
```

This is a JSON object, not a shell dictionary. Use one line with outer single
quotes to retain JSON quotes and literal `$`/`#` characters. If a header contains
an apostrophe, encode it inside JSON as `\u0027`. Use JSON escapes for quotes
(`\"`) and backslashes (`\\`). The deployer retains existing file text; generated
temporary env files also preserve these characters. Header values are secrets:
keep env files mode 0600, out of Git/backups, and do not paste `docker inspect`
or `docker compose config` output into logs/support requests. Docker operators
can still inspect container environment variables.

For Cloudflare, publish a dedicated worker hostname through the GPU host's
Tunnel, and protect it with an Access Application using a Service Auth policy
that permits the intended service token. Set the advertised/enrolled remote
endpoint to `https://gpu.example.com`, not the Cloudflare login page. A native
host cloudflared may target the worker's loopback port; a cloudflared container
must reach the worker over its Docker network, not its own localhost. Do not
publish the worker's origin port to the Internet. Existing LAN enrollments are
not silently migrated: keep the worker credential, explicitly reconcile the
conference remote URL and import a matching trusted enrollment when changing
the advertised endpoint. Also check the gateway's combined two-PDF request
size limit. Tunnel configuration/policies are managed outside CFM.

CFM sends the configured headers on remote health checks, job upload, polling,
result retrieval and explicit retries, including startup/cutover checks. It
continues sending its separate per-conference `Authorization: Bearer` token.
No custom headers are sent to the local worker or unrelated integrations.
HTTP is allowed for a trusted isolated LAN; use HTTPS for gateway deployment.
The common HTTP client sends `User-Agent: ConferenceFinalManager/1` by default
on local and remote requests, including preflight. This identifies the compute
transport, not a browser or OCR/cache version. No per-conference User-Agent
setting is necessary. Explicit custom remote User-Agent headers remain allowed;
an obsolete `Python-urllib/...` override can still trigger gateway policy.
Cloudflare 1010 specifically means browser-signature blocking, not proof of a
bad worker token. Preflight includes the HTTP status, safe Cloudflare code and
bounded Ray ID; it withholds remote prose, cookies and redirect URLs. Ordinary
401/403 without a recognized provider code remain access-denied diagnostics,
not a guessed attribution. DNS, TLS, timeout, redirect and non-JSON errors are
distinct. Gateway policy and credentials are never automatically weakened.

Configuration is limited to 32 unique case-insensitive header names, 128
characters per name, 4096 printable ASCII characters per value and 16 KiB of
JSON. Blank/`{}` means no headers. Non-string values, duplicates, control
characters and reserved auth/protocol/transport headers are rejected before
network requests. In particular, `Authorization`, `Host`, `Accept`, content
framing/type/encoding, hop-by-hop headers and `X-CFM-*` cannot be overridden.
Do not use an Access single-header configuration that replaces `Authorization`.

After editing an existing conference env, run the unified update command for
that project. It rebuilds/recreates with candidate preflight; restarting a
container alone does not load changed Compose environment. Invalid headers,
401/403, or a login redirect fail explicitly; they do not authorize local
fallback. An unreachable remote now pauses new comparisons until it recovers or
the editor explicitly confirms local execution for that PDF pair.
Gateway header rotation does not change worker job identity or start a second
copy of accepted work. Changing worker URL or worker Bearer token still uses
the existing signed routing safeguards. Header values are redacted in update
plans and absent from CFM audit/diagnostics and System State archives. They do
not change publication selection, comparison results/cache or review flags.

## Creating Additional Conferences

Create one environment file per conference:

Start the [shared comparison slave](#shared-comparison-slave) first. It creates
the external Docker network required by conference `web` containers. This is
also required before updating an older conference to this Compose layout.

```bash
cp .env.example .env.conference-a
python3 scripts/update_docker_instances.py --create-missing --dry-run
python3 scripts/update_docker_instances.py --create-missing
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

### Shared Comparison Slave

This section explains the runtime and manual provisioning interface. Normal
[unified deployment](#unified-deployment) already performs model/client setup;
do not repeat these manual steps or regenerate existing credentials.

Run **one shared worker per trusted host**, in its own Compose project. It is
independent of every conference project and can run from a standalone checkout
of this repository without starting the conference app. Do not add its Compose
file to a conference's Compose invocation. The worker has its own image, job
ledger, signing secret and persistent `worker_data` volume; it has no conference
database, migration entrypoint or media mount. It uses Django signing utilities
with an empty application/database configuration, not the conference settings.

Native launchers default to `CFM_COMPARISON_TRANSPORT=embedded`. Conference
Compose defaults explicitly to `service`, with these per-conference settings:

| Setting | Default / purpose |
| --- | --- |
| `CFM_COMPARISON_TRANSPORT` | `service` in Compose; `embedded` explicitly opts into local isolated worker execution |
| `CFM_COMPARISON_NETWORK` | `cfm-comparison`; shared Docker network created by the slave project |
| `CFM_COMPARISON_LOCAL_URL` | `http://comparison-slave:8091` on the shared Docker network |
| `CFM_COMPARISON_LOCAL_TOKEN` | Blank; this conference's local-worker bearer token |
| `CFM_COMPARISON_REMOTE_URL` | Blank; optional trusted remote worker |
| `CFM_COMPARISON_REMOTE_TOKEN` | Blank; separate token for that remote worker |
| `CFM_COMPARISON_REMOTE_HEADERS` | Blank; private JSON object of remote-only gateway headers |

URLs alone do not select service mode for a native launcher. Missing tokens,
unreachable services or invalid runtimes mean Comparison unavailable, not a
silent switch to embedded OCR. Set tokens separately in each conference env
file and each endpoint. They are deployment secrets, not System State data.

Prepare the same checksum-approved models described in the
[Developer Guide](developer_guide.md#pdf-comparison-runtime). A slave-only host
can receive the two already-validated model files from the provisioning machine;
it does not need a conference database or a running app to install them.
The worker validates their pinned checksums again and never downloads models.
Create `runtime/comparison-slave/clients.json` as a JSON object mapping stable
conference namespaces to distinct random bearer tokens, each at least 32
characters. For example, generate each value separately with
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`:

```json
{"conference-a": "REPLACE_WITH_A_UNIQUE_RANDOM_TOKEN_AT_LEAST_32_CHARS"}
```

Do not use that placeholder as a credential. The clients source must be a
regular non-symlink file with mode `0600` and at most 64 KiB. Set
`chmod 600 runtime/comparison-slave/clients.json` on the host. Compose file-backed
secrets retain host ownership; declaring secret `uid`/`mode` does not remap it.
The container entrypoint therefore reads this source without modifying it,
validates its private mode and bounded content, and atomically writes a
worker-owned `0600` effective configuration at `/worker-data/clients.effective.json`.
Only after validation does it set the server's `CFM_SLAVE_CLIENTS_FILE` to that
effective path. This permits a host-UID-1000 secret with a container-UID-0 worker
without loosening the server's file-ownership checks. Native launchers bypass
the container initializer and must supply a private file owned by their own UID.
Protect env files from other users too. The secret is mounted read-only at
`/run/secrets/clients.json`. Authentication determines the namespace, not a
namespace supplied by the client. Restart the worker after editing credentials.

```bash
mkdir -p runtime/comparison-slave
cp .env.slave.example runtime/comparison-slave/worker.env
# Provision clients.json and approved models; edit worker.env for this host.
python3 scripts/start_comparison_slave.py \
  --env-file runtime/comparison-slave/worker.env --dry-run
python3 scripts/start_comparison_slave.py \
  --env-file runtime/comparison-slave/worker.env
```

The launcher validates Compose ownership, private clients and the read-only
model mount, builds the candidate, and loads the actual CPU/CUDA model sessions
in a disposable preflight container before replacing the existing worker. It
then waits for Docker health, not just a successful `up` command. Missing models,
invalid credentials or an unavailable provider fail before replacement. Dry run
does not build, start containers or infer; it cannot certify provider readiness.
This lower-level launcher does not provision models/client credentials; the
unified deployer does so before invoking it. Neither interface overwrites
existing credentials or silently replaces corrupt models. Rerun the launcher after editing credentials; successful deployment
recreates the worker so file-backed secrets are reloaded. Allow current jobs to
finish first; interrupted jobs are not automatically moved to another worker.

The default project is `cfm-comparison-slave`, distinct from all conference
projects. `CFM_SLAVE_PROJECT_NAME` controls that project; keep the identity stable
and do not set a conference's `COMPOSE_PROJECT_NAME` in the worker environment.
The command is `gunicorn --workers 1 --threads 4 --bind 0.0.0.0:8091
--timeout 30 comparison_slave.server:application`. The HTTP timeout is not the
OCR job budget; inference remains isolated and bounded by `CFM_RAPIDOCR_TIMEOUT`
(300 seconds by default), with an outer watchdog 30 seconds longer.
Compose's 30-second health check authenticates to loopback `/v1/health` using
the effective private config, primes lazy service startup, and accepts only
ready/busy states. Its command arguments and output never contain the token.
Allow the 60-second startup grace period and wait for Docker health to become
healthy before sending the first comparison. A not-ready/failed runtime stays
unhealthy instead of silently selecting another backend.

`CFM_SLAVE_ROOT=/worker-data` is the dedicated volume. It persists worker identity,
namespace caches, signing material and the job ledger across container recreation.
The image entrypoint initializes only this directory's mode to `0700`, after
verifying it is a real directory owned by the current UID. It does not recursively
change or delete files. Identity and signing material are created automatically;
do not generate or supply a worker identity by hand. `CFM_SLAVE_SECRET` is an
optional server signing-secret file path, not the clients-file path.
Never use `down -v` when updating it. Keep this volume separate from `sms_data`,
and do not clone it to create another worker identity. Rebuild/update the worker
with the explicit commands above, separately from conference maintenance.
Store the operational env file outside the root `.env.*` discovery pattern.
The conference updater excludes the reserved `.env.slave` and `.env.slave.*`
names, including `.env.slave.example`; it never starts or updates the worker.
It retains comparison transport, network, URLs and separate tokens during
conference recreation, and masks both tokens in update/rebuild plans.

The worker creates the named bridge network `cfm-comparison` and the DNS alias
`comparison-slave`. Every conference `web` joins this external network as well
as its own project-default network; `proxy` remains only on its default network.
Set `CFM_COMPARISON_NETWORK` identically in the worker and conference env files
if changing the shared network name. Do not use `host.docker.internal` for this
default: Linux's host gateway cannot reach a host-loopback-only published port.
Start the shared worker before starting/updating conferences. Stop conferences
before removing its network; routine worker updates use the launcher above, not
`down`. Explicit embedded-only or remote-only Docker installations still need
this external network provisioned once (`docker network create cfm-comparison`)
even when no local worker is started.

The conference create/rebuild/migration tools check this network and run the
candidate image's `scripts/check_comparison_runtime.py` without its migration
entrypoint, followed by Django system checks against temporary in-memory SQLite,
before cutover. Local tokens/readiness/contract compatibility are
checked even when a preferred remote is online, so the configured local service
is usable after explicit confirmation. An unreachable optional remote is a startup
warning when local is ready, not authorization to silently compute on CPU;
authentication/configuration errors stop deployment. Failed preflight leaves
the existing conference container running. Raw `docker compose up` is not a safe
update substitute: its web entrypoint checks before migrating, but Compose may
already have replaced an old container. Use the updater for existing conferences.

Raw backup checks restart prerequisites before stopping running web images that
declare startup-preflight support. Legacy images retain their prior backup
behavior. Checks never migrate or inspect publication data. If an external
dependency fails after validation, normal bounded health/error handling still
applies; preflight is not a promise of permanent network/service availability.

The separate published port remains loopback-only for native clients, which
can explicitly use `http://127.0.0.1:8091`. For authorized remote clients choose
a private host bind and firewall rules explicitly. No public `0.0.0.0` bind is
required for same-host conference containers. Bearer tokens over HTTP
are only appropriate on the trusted host/private network; use a trusted TLS
gateway for other deployments. This is not a hardened public service.

### Optional GPU Override

Normal GPU setup uses the [unified deployer](#unified-deployment). The following
commands describe the lower-level override for already provisioned deployments.

CPU is the default image and backend. GPU is opt-in on an NVIDIA Linux host
with the NVIDIA Container Toolkit, a compatible driver and finite container
memory enforcement. Use the same project and volume, adding the override on
**every** GPU configuration/build/update command:

```bash
python3 scripts/start_comparison_slave.py \
  --env-file runtime/comparison-slave/worker.env --gpu --dry-run
python3 scripts/start_comparison_slave.py \
  --env-file runtime/comparison-slave/worker.env --gpu
```

The GPU target alone installs `onnxruntime-gpu[cuda,cudnn]==1.31.1`; it never
installs the CPU `onnxruntime` wheel beside it. The CPU target never installs
GPU dependencies. `CFM_SLAVE_GPU_DEVICE` selects one host index or UUID; the
selected device is visible as ordinal 0 inside the container. The override sets
`CFM_OCR_BACKEND=cuda`. Other backend names are rejected, not auto-detected.
ORT's CUDA/cuDNN extras supply runtime libraries; the host still needs a
compatible NVIDIA driver. Check the pinned wheel's requirements against the
[official CUDA provider requirements](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html).

RapidOCR 3.10.0's installed `config.yaml` and `onnxruntime/provider_config.py`
define `EngineConfig.onnxruntime.use_cuda` and `cuda_ep_cfg.device_id`. This
backend sets those explicit keys, preloads the installed CUDA libraries, loads
both approved models and requires `CUDAExecutionProvider` first in both actual
session provider lists. Missing libraries/devices or CPU fallback cause failure;
listing CUDA among available providers alone is not acceptance. Session options,
device UUID/name/compute capability, driver and NVIDIA library versions enter
the cache fingerprint. CPU/GPU, hardware, package, code and thread changes
cannot share cached evidence. GPU uses the same models, 300 DPI, OCR thresholds
and result rules, with TF32 disabled; it is not asserted to have byte-identical
CPU results.

CUDA virtual-address reservations are incompatible with the CPU worker's 6 GiB
`RLIMIT_AS`. GPU therefore requires a readable finite Linux cgroup v2
`memory.max` or v1 `memory.limit_in_bytes` instead. Compose sets `mem_limit`
and `memswap_limit` to `CFM_SLAVE_MEMORY_LIMIT` (default `8g`, no extra swap).
An unlimited/missing controller fails closed; do not remove the limit to make
GPU work. This bounds host memory, not device VRAM. The CPU-time and wall-clock
bounds remain unchanged for both backends.

Actual Linux Docker CUDA results, including three full PDF pairs, cold/warm
cache timings and the unchanged timeout test, are recorded separately in
[Deployment Validation](deployment_validation.md#gpu-full-document-docker-acceptance).
These measurements do not guarantee every future PDF meets its deadline.

### Embedded CPU Setup

For explicit `CFM_COMPARISON_TRANSPORT=embedded`, method 8 uses CPU inference with `rapidocr==3.10.0`,
`onnxruntime==1.31.1` and the selected, approved PP-OCRv6 Small/Medium detection/recognition model
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

Alternatively use a one-off `web` container with its startup entrypoint explicitly
replaced; the normal entrypoint requires models already provisioned. Build the
image and create the configured comparison network first, then run:

```bash
docker compose --env-file .env.conference-a build web
docker compose --env-file .env.conference-a run --rm --no-deps \
  --entrypoint python -v /absolute/path/to/approved-models:/provision:ro \
  web manage.py prepare_rapidocr_models --source /provision \
  --destination /app/runtime/rapidocr-models
```

Replace the absolute host source with your approved model directory. This writes
the default model directory through the existing app mount. The command imports local files only;
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
`CFM_RAPIDOCR_THREADS` in the worker environment file for service mode, or the
conference environment file for embedded mode (default `4`, valid
`1`-`8`, within the visible CPU count). The standalone Compose file passes it
to the worker; conference Compose passes it to `web` for embedded execution,
and the unified updater retains that setting during recreation. Preview the
conference update as usual.
This is a machine/runtime option, not an AppSetting or System State field.
In service mode, update/recreate the standalone worker with its own Compose
files; changing conference thread settings does not retune the shared service.

Compare 4/6/8 sequentially, not as competing OCR jobs. Check full text, boxes,
scores, locations and authored edit controls as well as timing. Logical threads
are not physical cores, and ARM64 Docker Desktop timing is not a performance
guarantee for native Intel/AMD Linux. Do not assume maximum threads is fastest.
RAM increases do not fix a CPU-bound workload when there is already sufficient
memory. Thread tuning does not change models or 300 DPI. Linux CPU-time accounting
allows the configured page concurrency and supported threads within the chosen
wall budget; defaults use 300 seconds.

### Worker OCR Settings

After a successful worker deployment, the script prints the version/backend/
state observed from the running service and its effective OCR settings.
Formatting's Comparison services shows CPU/GPU release versions; Details lists
the validated model sizes, parallel pages, threads, timeout and model idle
duration. Editing a host env without deploying does not change that observation.
Version is packaged with the image, not configured in `.env`. Read the running
worker directly without displaying tokens:

```bash
docker exec <worker-container> python -m comparison_slave.entrypoint --status
```

This authenticated local read loads no models, changes no conference data and
prints only allowlisted metadata. Older workers may not support this command.
Worker release numbers do not replace protocol/contract compatibility checks.

Set these on the **shared worker**, not each conference, in
`compose.comparison-slave.yml` / `compose.comparison-slave.gpu.yml`, or through the
worker's private environment file. Compose resolves the values; the unified
deployer uses that same resolved model selection when provisioning.

| Worker setting | CPU default | GPU default | Allowed |
| --- | --- | --- | --- |
| `CFM_RAPIDOCR_PAGES` | 2 | 4 | 1-8 concurrent pages |
| `CFM_RAPIDOCR_THREADS` | 4 | 4 | 1-8 per engine, within visible CPUs |
| `CFM_RAPIDOCR_DET_MODEL` | small | small | small / medium |
| `CFM_RAPIDOCR_REC_MODEL` | small | small | small / medium |
| `CFM_RAPIDOCR_TIMEOUT` | 300 | 300 | 30-3600 seconds per pair |
| `CFM_GPU_MODEL_IDLE_SECONDS` | ignored | 1800 | 0 disables residency; 1-86400 idle seconds |

300 DPI, detection limit 2560, recognition batch 6 and comparison rules stay fixed.
For example, in the GPU Compose service's `environment`:

```yaml
CFM_RAPIDOCR_PAGES: ${CFM_RAPIDOCR_PAGES:-4}
CFM_RAPIDOCR_DET_MODEL: ${CFM_RAPIDOCR_DET_MODEL:-small}
CFM_RAPIDOCR_REC_MODEL: ${CFM_RAPIDOCR_REC_MODEL:-medium}
CFM_RAPIDOCR_TIMEOUT: ${CFM_RAPIDOCR_TIMEOUT:-300}
```

Existing values in the selected worker environment file override these defaults.
An explicit `PAGES=1` or `THREADS=2` is not rewritten by deployment/update.
Only omitted page settings use the new CPU default; no automatic retuning occurs.
Native/embedded CPU retains its one-page default. Update CFM and all workers
together because execution-code changes update the shared compute contract,
even when the GPU's default settings stay unchanged.
Do not set `PAGES=1` in a shared example when you intend GPU's default four.
Then preview/apply using the same worker project, client registration and
deployment command previously used (`deploy.py worker`, with `--gpu` for CUDA).
The unified deployer downloads only missing selected pinned models, rejects
corrupt existing files, and detects Compose edits as worker changes. Offline
requires the selected files already present. Lower-level startup never downloads;
it validates the selected runtime before replacing the worker. Allow jobs to finish
before recreation. Conference publication files, DBs and review flags are untouched.

GPU combines dynamic page dispatch with on-device CTC reduction. CPU defaults to
two independent engines/four threads each; an explicit one-page setting retains
sequential inference. Check available RAM and `PAGES * THREADS` oversubscription.
Two pages use more RAM than one. Four concurrent CPU pages failed under the
unchanged 6-GiB address-space bound in measured acceptance; do not assume more
pages will be faster or fit. No automatic memory-limit increase is introduced.
Medium models/concurrent pages can need much more RAM/VRAM. Four Small/Small pages
used about 11.8 GiB VRAM on the tested 16-GiB card; this is not a universal memory
guarantee. Do not assume four Medium pages fit. OOM/timeout means unavailable,
never partial success or silent model/backend/quality downgrade.

GPU models now stay in one supervised inference process between jobs. After
1800 seconds without inference, the process exits and releases its CUDA memory;
the next OCR miss loads it again. Set `CFM_GPU_MODEL_IDLE_SECONDS=0` deliberately
to retain the old per-job lifetime, or e.g. `1800` for 30 minutes. This is a
**shared GPU worker** setting, not a conference or CFM web setting. Existing
explicit values (including 0) survive updates. CPU execution is unchanged.
Residency can retain the peak VRAM allocation until idle expiry; account for
other GPU services. It saves model/session initialization, not neural inference
work. It does not increase deadlines or lower models/DPI/batch quality.

Health checks and complete cache hits neither refresh the idle timer nor reload
idle models. A cold worker can be Ready: its runtime/device was validated and
models load only on new OCR. Formatting's existing service Details shows model
state separately. Cold probes verify pinned files, installed libraries and the
same GPU identity without loading model sessions. Namespace/signing/cache data
remain in isolated job processes; only models are shared. Timeout/OOM/invalid
evidence kills and reaps the actor before reuse; server death kills it too.
No extra LAN port, Docker service, credential or conference migration is needed.
Recreate the GPU worker with the normal unified deployment command after changing
the env. Update CFM and workers from the same checkout for contract compatibility.

Changing models/concurrency invalidates runtime-bound cache. Obsolete completed
worker jobs require explicit Retry; no old evidence is served as the new model's
result. This invalidation is worker-local: the existing complete-cache discovery
policy may still reuse valid evidence from another unchanged worker, recording
that worker rather than claiming it ran the newly configured model. Model
selection configures a worker's new inference, not a per-request force-recompute
or accuracy requirement. Timeout-only changes can reuse complete validated evidence. CFM still
waits at most 240 seconds per service request before returning the same job's
Retry flow; that is separate from the worker's configurable execution deadline.
For embedded execution, also keep `SMS_WEB_TIMEOUT` and proxy timeouts longer
than the chosen OCR budget; these are independent deployment limits. Their
defaults are 360 seconds to leave margin over the default 300-second OCR work.
Preserved explicit web values are not overwritten; adjust them deliberately if
you previously set a shorter limit. Service mode's 240-second polling window
continues to return Retry while the same worker job finishes independently.

The image build excludes `data/`, `runtime/`, `output/` and `.playwright-cli/`.
Research PDFs, screenshots and local models must not be baked into the image.
Provision models separately as described above. A passing image build or unit
suite is not evidence that full PDFs complete within the runtime limit.

### Local CPU Cache

Manual and automatic builds can reuse signed document/pair evidence under
`data/pdf_comparison_cache/`; the worker still validates actual dependencies,
models and runtime fingerprint. Host/container, architecture, package, thread or
code changes invalidate reuse. No cache is baked into images or State ZIPs;
CPU installs need no GPU package. Shared-service caches instead live in the
worker's namespace directories on `worker_data`; the persisted worker identity
excludes ephemeral container hostname/MAC values while retaining runtime and
hardware details. See the
[cache contract](pdf_comparison.md#local-cpu-evidence-reuse). Keep the same resource
and timeout bounds. Repeated comparisons may be fast, but this does not resolve
all cold full-document timeouts in the [validation record](deployment_validation.md).

### Validation Records

See [Deployment Validation](deployment_validation.md) for the latest measured
scope and clearly separated earlier CPU/service/preflight results. In particular,
the older ARM64 CPU timeout record is not a result for the later CUDA deployment.

## Update Existing Instances

Use the existing updater for normal updates, from the owning checkout:

```bash
python3 scripts/update_docker_instances.py --dry-run
python3 scripts/update_docker_instances.py
```

Limit the conference operation with `--project <existing-project>`. On a
worker-only host, repeat its `deploy.py worker` command instead; `update` targets
conferences and does not operate a remote host through SSH. Keep existing project
identities and perform worker changes during a maintenance window.

`python3 scripts/deploy.py update` is an equivalent compatibility entry point;
it does not have different preparation/authentication behavior. Both accept
`--dry-run`, repeatable `--project`, `--offline`, and `--create-missing`.
Dry-run renders the actual per-project env/port/data/settings plan and local
worker/model requirements without persistent writes, downloads or container
changes. It is not a successful live connection test or a runtime guarantee.

The updater:

1. discovers maintained conference environment files;
2. renders and validates every desired Compose configuration;
3. rejects duplicate project names, overlapping endpoints, shared data
   folders, foreign checkout ownership, and changes to an existing
   `SMS_DATA_DIR`;
4. fills absent managed-env prerequisites without replacing existing values,
   validates the entire prepared batch again, provisions missing pinned models,
   and enrolls all selected local clients in one shared-worker preparation;
5. builds and preflights each candidate runtime before its cutover;
6. replaces only that conference's `web`, reloads or recreates `proxy` only
   when required, and waits for readiness;
7. verifies the loaded Nginx Host directive, one static asset, and a
   non-mutating same-origin CSRF POST.

New environment files are reported but not started. After reviewing the dry
run, create them explicitly:

```bash
python3 scripts/update_docker_instances.py --create-missing
```

Use repeatable `--project NAME` options to limit an operation. Secrets are
masked in plans. Missing settings/tokens may be appended to managed env files;
existing nonempty values/comments are retained. Manually supplied remote URL,
per-conference worker token and custom headers are supported; enrollment JSON
is an optional explicit import, not required for subsequent updates.
If the env omits a signing secret or worker connection that the existing
container already uses, preserve that live value rather than generating a new
identity or dropping the connection. An explicitly changed endpoint does not
inherit the old endpoint's token/headers. Explicit blank URL/header settings
are retained. Concurrent env edits after planning abort instead of being overwritten.
Compose resolves variable references in existing assignments; generated literal
values escape dollar signs. Signing keys and credentials containing quotes,
backslashes or dollar signs must keep the same bytes in the recreated container.

A configured custom external worker is not taken over by this updater. Remote-only
mode does not provision a local worker. An env-less instance retains its live
configuration: a complete registered worker connection can be reused; missing
credentials/configuration require an explicit managed env, never an invented
replacement identity. A shared-worker change may affect all its clients, even
when only one conference was selected. Wait for all comparisons to finish and
exclude new submissions during maintenance; the busy check is not an atomic drain.

Batch updates are not an all-conference transaction. A later failure leaves
earlier verified updates in place. Before a conference cutover, failure restores
its staged env and leaves its old container running. Once container replacement
was attempted, retain that env and inspect the failed deployment status; do not
claim automatic rollback of containers, files or migrations.

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

### PDF Reader Static Assets

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
