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

- Run every disruptive command with `--dry-run` first when supported.
- Confirm project name, endpoint, and `SMS_DATA_DIR` before applying.
- Keep `SMS_DEBUG=0` for normal operation.
- Keep one Gunicorn worker with SQLite.
- Download a System State ZIP before major maintenance.
- Verify the Dashboard and one state-changing CSRF-protected action after
  maintenance.
- Never run `docker compose down -v` for a conference instance. `-v` deletes
  the named data volume.
