# Operations

Use [Deployment](deployment.md) for installation and updates. This document
covers data ownership, maintenance and recovery.

## What To Preserve

| Location | Contains | Recovery role |
| --- | --- | --- |
| Conference `sms_data` named volume | Live SQLite, uploads, reviews, history, logs, local service secrets | Do not delete |
| `SMS_DATA_DIR` | Verified host mirror when the backup script runs | Not the live Docker database; not continuously synchronized |
| Per-conference `.env.*` | Stable project identity, signing secret, worker URL/token/headers | Keep private and back up separately |
| `runtime/comparison-slave/worker.env`, `clients.json`, `deployed.json`, `enrollments/` | Worker identity and authorization | Needed for safe updates; do not regenerate lost tokens |
| Worker `worker_data` volume | Job ledger, signing key, namespace caches | Independent of conference data |
| `runtime/rapidocr-models/` | Pinned OCR models | Deployment files, not conference backups |
| `runtime/deployment.log` | Host deployment events, without credentials | Separate from conference Audit Log |

`sms_static` and `sms_gateway_state` are not manuscript/review data.
A worker restart affects all conferences using it. Finish comparisons and stop
new submissions before updating; the busy check is not an atomic drain.

## Backup And Restore

### Portable System State

In the app, open **Administration > System State Backup / Restore**
(`/integrations/system-state/`). Download a ZIP before major maintenance.

| Included | Excluded |
| --- | --- |
| Settings and conference records | Deployment envs and worker credentials |
| Original/Corrected files and Formatting revisions | OCR models and worker jobs/caches |
| Verification images, thumbnails and format previews | Temporary import/edit/upload/restore tokens |
| Reports and automatic CrossCheck execution/result history | Comparison snapshots/cache |
| Active and archived audit logs | Local service secrets under `data/secrets/`, previous backup ZIPs |

Restore: start the receiving app, upload ZIP, inspect preview/warnings, confirm.
Managed paths remap to the new installation. File hashes, relationships and
archive completeness are validated; review flags and record timestamps are
preserved. Restore never renews expired CrossCheck previews.

Current archives are **v8**. Validated v5/v6/v7 are accepted by explicit adapters:
v5 has no Formatting revisions; v5/v6 have no automatic CrossCheck jobs; v7
jobs become their original execution. Missing v8 payloads cannot be excused by
pretending the file is v7. Receiving-machine service secrets are preserved.
Recheck service URL/folder and provision models separately after a move.

### Raw Docker Mirror

From the owning checkout:

```bash
python3 scripts/backup_docker_instances.py --dry-run
python3 scripts/backup_docker_instances.py
```

Add `--project sms-conf-dsa` to both commands for one conference. The script
pre-copies, stops only its web service for final sync, verifies SHA256/SQLite
integrity, promotes the mirror and restarts web. Nginx remains available with a
maintenance status. The previous complete mirror is retained.

Raw mirrors include local secrets: protect them as credentials. They are an
operational recovery tool, not a substitute for portable System State ZIPs.
Check the script's per-project result and `.sms-docker-backup-history.jsonl`.

## Cleanup

Use **Settings > Storage Management**, always preview before Apply.

| Policy | Deletes | Preserves |
| --- | --- | --- |
| Conservative | Unreferenced regenerable cache | Referenced images, revision files, debug/legacy output folders |
| Generated reports/exports | Reproducible download packages and system-generated report/manifest files | Original/Corrected uploads, plagiarism reports, state backups, unrelated files |
| Clear Database | Records and app-owned managed files, using its explicit confirmation | Audit trail by default; configured external absolute folders |

Cleanup Apply checks references and file identity again. A file that changed,
became referenced or became protected after preview is skipped. Unreadable roots
block cleanup, rather than being treated as empty. Revision files remain protected
even with overlapping configured folders.

Clear Database stages files for rollback if its DB operation fails.
**Also archive and clear audit log** moves the current trail into its archive
and starts a new log. Download a System State ZIP first.

Do not run general Docker volume pruning as a CFM cleanup procedure.
Build cache and unused images are not conference data, but deleting them loses
build reuse/rollback images. Verify exact ownership before any manual removal.

## Worker Settings

Edit the **existing worker env**, then use its normal update command.
Do not replace it with the example. New-host custom setup may use
`worker --init --template <file>` with a previously absent target.

| Worker setting | Default / accepted values | Purpose |
| --- | --- | --- |
| `CFM_SLAVE_PROJECT_NAME` | `cfm-comparison-slave` on init | Stable worker Compose identity; not renamed by update |
| `CFM_DEPLOY_WORKER_BACKEND` | `cpu` or `cuda`; deployer records it | Preserved backend |
| `CFM_COMPARISON_NETWORK` | `cfm-comparison` | Shared host Docker network |
| `CFM_SLAVE_BIND_HOST` / `CFM_SLAVE_PORT` | `127.0.0.1` / `8091` | Host port publication; container listens on 8091 |
| `CFM_SLAVE_MEMORY_LIMIT` | `8g` | Host RAM/cgroup bound, not VRAM |
| `CFM_SLAVE_MODEL_DIR` | `./runtime/rapidocr-models` | Host model folder |
| `CFM_SLAVE_CLIENTS_SOURCE` | `./runtime/comparison-slave/clients.json` | Private namespace/token map |
| `CFM_RAPIDOCR_THREADS` | 4, bounded by visible CPUs; explicit 1-8 | Per-page ONNX CPU threads |
| `CFM_RAPIDOCR_PAGES` | CPU 2 / GPU 4; 1-8 | Concurrent OCR pages; embedded/native CPU defaults to 1 |
| `CFM_RAPIDOCR_DET_MODEL` / `CFM_RAPIDOCR_REC_MODEL` | `small`; `small` or `medium` | Independently selected, checksum-pinned models |
| `CFM_RAPIDOCR_TIMEOUT` | 300; 30-3600 seconds | OCR pair wall budget |
| `CFM_SLAVE_GPU_DEVICE` | `0` | Host NVIDIA index or UUID; container uses device 0 |
| `CFM_GPU_MODEL_IDLE_SECONDS` | 1800; 0 or 1-86400 | GPU weights unload after idle inference; 0 disables residency |

GPU residency shares model sessions only, not paper data or namespace caches.
Health polls and complete cache hits neither load models nor extend idle time.
Timeout/crash discards the model process. More threads/pages can use more RAM
and do not guarantee faster completion; unchanged 300 DPI/model quality remains
mandatory. Existing explicit values survive updates.

Token-safe status read (also works with a running GPU service; no recreation):

```bash
docker compose --env-file runtime/comparison-slave/worker.env \
  -f compose.comparison-slave.yml ps
docker compose --env-file runtime/comparison-slave/worker.env \
  -f compose.comparison-slave.yml exec comparison-slave \
  python -m comparison_slave.entrypoint --status
```

Use `--env-file` for a non-default worker config. A legacy CUDA env without a
backend marker may use `deploy.py worker --gpu` to fill it; a conflicting marker
is never replaced. Custom first-time settings use `.env.slave.example` as a
separate `--template`, not a replacement for an existing env. New conference
customization similarly uses `.env.example` with a unique identity/port/mirror,
private signing secret and the deployer's explicit `--env-file`.

`deploy.py update`, `rebuild_docker_instances.py` and the historical worker
launcher delegate to the same lifecycle guards, not alternative recovery paths.

## Conference Settings

Edit each conference's private env, then run the selected-conference updater.

| Setting | Purpose |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | Stable identity used to match env to instance |
| `SMS_PORT`, `SMS_BIND_HOST` | Published address; default loopback |
| `SMS_ALLOWED_HOSTS` | Include actual LAN hostname/IP or protected public hostname |
| `SMS_DATA_DIR` | Unique raw backup mirror path; cannot casually change on update |
| `SMS_SECRET_KEY` | Stable signing secret; do not rotate during normal update |
| `SMS_DEBUG` | Keep 0 in normal Docker operation |
| `SMS_WEB_WORKERS` | Keep 1 with SQLite |
| `SMS_WEB_THREADS`, `SMS_WEB_TIMEOUT` | Default 4 / 360 seconds |
| `SMS_PROXY_MAX_BODY_SIZE` | Default finite 10g request-body limit |
| `CFM_COMPARISON_TRANSPORT` | Docker `service`; native `embedded` |
| `CFM_COMPARISON_LOCAL_URL` / `CFM_COMPARISON_LOCAL_TOKEN` | Local worker; default URL `http://comparison-slave:8091` |
| `CFM_COMPARISON_REMOTE_URL` / `CFM_COMPARISON_REMOTE_TOKEN` | Optional preferred worker, separate per-conference token |
| `CFM_COMPARISON_REMOTE_HEADERS` | Remote-only gateway headers, JSON; not worker bearer token |

For remote-only service mode, explicitly leave **both** local URL and token
empty; no local worker is provisioned. Keep gateway/body/time limits sufficient
for the two PDFs (maximum 64 MiB each plus framing). Never fix proxy errors by
disabling CSRF or replacing the browser-visible Host header with the wrong port.

## Recovery

| Situation | Safe action |
| --- | --- |
| Missing worker env or authorization | Restore original private files; never initialize replacement identity |
| Failed build/preflight | Existing service should not have been cut over; inspect the reported failure |
| Failed container cutover | Inspect containers and retained env; completed migrations/earlier conference updates are not automatically rolled back |
| `runtime/.docker-data-operation.lock` exists | Confirm no management process is running; do not delete a live lock |
| Mirror `.backup-swap` exists | Preserve it and the mirror; inspect backup history before retry |
| Need worker rename | Treat as a separate migration, not an update; preserve old credentials/data until new deployment verified |
| Interrupted upload/POST | Reopen workflow and inspect state; maintenance gateway never replays POSTs |

For a legacy bind-mounted conference, preview and apply migration:

```bash
python3 scripts/migrate_docker_data_volumes.py --dry-run
python3 scripts/migrate_docker_data_volumes.py
```

It retains the original host folder. For emergency host-mirror rollback only,
after verifying that the mirror is current, stop concurrent maintenance and use
the **same env and project**:

```bash
docker compose -f docker-compose.yml -f docker-compose.bind.yml \
  --env-file .env.dsa -p sms-conf-dsa up -d --build
```

This switches to the mirror, not a merge. Resolve the volume problem before
returning to normal deployment. Never run `down -v` against a conference.
