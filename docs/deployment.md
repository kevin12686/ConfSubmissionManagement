# Deployment

Run from the repo root. Change the example values, preview, then apply.
For backup/recovery and tuning see [Operations](operations.md).
For non-Docker startup see [Native setup](developer_guide.md#native-setup).

## Prerequisites

| Host | Required |
| --- | --- |
| CFM / CPU | Git, Python 3, Docker Engine/Desktop with Compose v2 |
| GPU | Same, plus NVIDIA driver and Container Toolkit working with Docker |
| First setup | Internet for images/models; user can run Docker |
| Access | Trusted network or protected gateway; CFM has no editor login |

On each host, clone once (use the existing checkout for updates):

```bash
git clone https://github.com/kevin12686/ConfSubmissionManagement.git
cd ConfSubmissionManagement
docker version
docker compose version
```

## 1. CFM + Shared CPU Worker

On the **CFM host**:

```bash
PROJECT=sms-conf-dsa
PORT=8000

python3 scripts/deploy.py conference --name "$PROJECT" --port "$PORT" --dry-run
python3 scripts/deploy.py conference --name "$PROJECT" --port "$PORT"
```

Open `http://127.0.0.1:8000/` (use your chosen port).
Models, tokens and the shared CPU worker are automatic. Repeat with a different
project/port for another conference.

| Value / generated file | Meaning |
| --- | --- |
| `PROJECT` | Unique, stable Compose identity |
| `PORT` | Unused host port |
| `.env.sms-conf-dsa` | Conference configuration and secrets |
| `runtime/comparison-slave/worker.env` | Shared CPU worker settings |
| `runtime/comparison-slave/clients.json` | Separate token per conference |
| `runtime/rapidocr-models/` | Checksum-verified models |

**Already deployed?** Use [Update](#3-update), not a new project name or copied
example env. Existing `.env.dsa` is discovered by its `COMPOSE_PROJECT_NAME`.
For LAN/tunnel browser access, set the existing env's `SMS_BIND_HOST` and
`SMS_ALLOWED_HOSTS` as described in [Conference Settings](operations.md#conference-settings).

## 2. Optional Remote GPU Worker

On the **GPU host**:

```bash
PROJECT=sms-conf-dsa
BIND=127.0.0.1
ENDPOINT=https://gpu.example.com

python3 scripts/deploy.py worker --init --gpu \
  --bind-host "$BIND" --endpoint "$ENDPOINT" --client "$PROJECT" --dry-run
python3 scripts/deploy.py worker --init --gpu \
  --bind-host "$BIND" --endpoint "$ENDPOINT" --client "$PROJECT"
```

| Parameter | Meaning |
| --- | --- |
| `--client` | Exact conference `COMPOSE_PROJECT_NAME`; repeat for several |
| `--bind-host` | `127.0.0.1` for host tunnel; LAN IP for LAN; `0.0.0.0` for all IPv4 interfaces |
| `--endpoint` | CFM-reachable origin, HTTPS hostname or `http://LAN-IP:8091`; never `0.0.0.0` |
| `--init` | First setup only; refuses existing identity/data |
| `--gpu` | Select CUDA; stored for later updates |

### Connect Each Conference

The GPU host creates
`runtime/comparison-slave/enrollments/sms-conf-dsa.json`.
On the **CFM host**, replace the SSH host/path, then copy it privately:

```bash
scp kevin@gpu-host:/absolute/path/ConfSubmissionManagement/runtime/comparison-slave/enrollments/sms-conf-dsa.json /tmp/sms-conf-dsa.json
chmod 600 /tmp/sms-conf-dsa.json
```

For Cloudflare Access, configure the [headers below](#cloudflare-access-or-custom-headers)
**before** connecting. Then run on the CFM host:

```bash
python3 scripts/deploy.py conference --name sms-conf-dsa \
  --remote-enrollment /tmp/sms-conf-dsa.json --dry-run
python3 scripts/deploy.py conference --name sms-conf-dsa \
  --remote-enrollment /tmp/sms-conf-dsa.json
```

Enrollment fills remote URL/token. Existing values must agree; no credentials
are silently overwritten. Keep enrollment files private, out of Git.

To add another conference later, on the GPU host:

```bash
python3 scripts/deploy.py worker --client sms-conf-qrs --endpoint https://gpu.example.com
```

Transfer/import its own JSON; do not reuse another conference's token.

### Cloudflare Access Or Custom Headers

In **each conference's existing CFM env**, add real service-token values:

```dotenv
CFM_COMPARISON_REMOTE_HEADERS='{"CF-Access-Client-Id":"REPLACE_ME","CF-Access-Client-Secret":"REPLACE_ME"}'
```

| Gateway item | Setup |
| --- | --- |
| Tunnel | Worker hostname -> published worker port |
| Host `cloudflared` | Can target `http://127.0.0.1:8091` |
| Container `cloudflared` | Use reachable Docker/LAN address, not its own localhost |
| Access Application | Service Auth policy allowing that service token |
| Headers | One-line JSON; used on every remote request/preflight |

Worker bearer token is separate from Cloudflare credentials.
Direct env configuration is also supported: remote URL is the reachable origin;
remote token is that conference enrollment's `token`.
Keep envs mode 0600. Never publish secrets or override `Authorization`/`Host`.
After env changes, update that conference; restart alone does not reload Compose env.

## 3. Update

Pause new comparisons. **Update GPU first, then CFM/local CPU from the same
code revision.** Preserve all existing envs, names, tokens and volumes.

On the **GPU host**:

```bash
git pull --ff-only
python3 scripts/deploy.py worker --dry-run
python3 scripts/deploy.py worker
```

On the **CFM host**:

```bash
git pull --ff-only
python3 scripts/update_docker_instances.py --dry-run
python3 scripts/update_docker_instances.py
```

| Option / behavior | Meaning |
| --- | --- |
| `--project sms-conf-dsa` on updater | One conference; repeat for several, omit for all |
| `--env-file <path>` on worker | Non-default **existing** worker env |
| Default worker env | Only `runtime/comparison-slave/worker.env`; no search/invention |
| Backend | Stored CPU/CUDA; ordinary update needs no `--gpu` |
| `--offline` | Selected models must already exist |
| `--create-missing` on updater | Explicitly create new env-declared conferences; normally skipped |
| `--dry-run` | Planning only, not build/live API/runtime validation |

Missing worker env/auth means **restore original files**, not `--init`.
The CFM updater handles its managed local CPU worker, not a remote host.
Never use `docker compose down -v` to update.

## 4. Verify

- Formatting > Comparison services: intended release, CPU/GPU and Ready.
- Details: intended models, pages/threads, timeout and GPU idle state.
- Dashboard: intended conference/data; open a PDF and check Error Report.

See [Operations](operations.md) for token-safe CLI status, backup and recovery.
