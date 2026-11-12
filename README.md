<p align="center">
  <img src="submissions/static/submissions/brand/app-icon-512.png" width="96" alt="Conference Final Manager">
</p>

# Conference Final Manager

A local-first workspace for preparing conference proceedings: collect final
submissions, review the files and metadata, and export a checked publication
package. Built with Django, SQLite and local file storage.

![Dashboard with publication readiness and editorial actions](docs/assets/readme/dashboard-preview.jpg)

The screenshots use disposable example data. The app has no editor login:
run it on a trusted machine or protected network, not an unprotected public URL.

## Understand The System

| Term | Meaning |
| --- | --- |
| Paper Master | The official paper roster and each paper's publication decision |
| Final Submission | One uploaded version, from Start2 or an Editor Upload; a paper can have several |
| Active version | The selected submission for a Paper ID; unresolved version conflicts still block final export |
| Original / Corrected | Uploaded files and the editor's replacements on that submission |
| Publication file | Corrected if selected, otherwise Original, on the active Publishing submission |
| Revision history | Read-only snapshots of Formatting files; never an alternative publication source |
| Readiness | The shared checks used by Dashboard, Error Report and final export |

A missing selected Corrected file **blocks publication**; it does not silently
fall back to Original. Generated debug copies, comparison results and historical
revisions never select the publication file.

## Start Here

| Your task | Read |
| --- | --- |
| Deploy or update CFM and its CPU/GPU workers | **[Deployment](docs/deployment.md)** |
| Run an editorial workflow | **[Operator Guide](docs/operator_guide.md)** |
| Fix a specific problem | [Troubleshooting](docs/troubleshooting.md) |
| Work on the code | [Developer Guide](docs/developer_guide.md) |
| Find a technical rule or test procedure | [Documentation Index](docs/README.md) |

### Docker

Requires Python 3, Docker Engine/Desktop and Compose v2. From the checkout:

```bash
python3 scripts/deploy.py conference --name sms-conf-dsa --port 8000 --dry-run
python3 scripts/deploy.py conference --name sms-conf-dsa --port 8000
```

Open <http://127.0.0.1:8000/>. Setup downloads pinned OCR models and creates
private settings, per-conference tokens and a shared CPU worker automatically.
For another host, remote GPU, existing deployments and updates, follow
[Deployment](docs/deployment.md) instead of manually assembling Compose commands.

### Native macOS / Windows

Requires Python 3.12+ and the approved OCR models. Follow
[native setup](docs/developer_guide.md#native-setup) once, then use:

| Platform | Launcher | Stop |
| --- | --- | --- |
| macOS | `start.command` or `./scripts/start_local.sh` | Ctrl+C in its terminal |
| Windows | `start_windows.bat` | Ctrl+C in its terminal |
| Linux | `./scripts/start_local.sh` | Ctrl+C in its terminal |

macOS and Windows launchers open the browser automatically. Native startup
installs packages, checks the runtime and applies migrations; it does **not**
download OCR models. Native comparison runs on local CPU by default.

## Editorial Workflow

1. Import Paper Master, then Final Submissions and their PDF/source files.
2. Resolve Paper IDs, version conflicts and Not Publishing decisions.
3. Process PDFs; review title/authors, formatting and plagiarism.
4. Resolve readiness blockers or record supported, reasoned exceptions.
5. Export the Final Publication Package ZIP and a System State backup.

Use Organized List as the per-paper checklist. Original/Corrected files in
Final Submissions are scoped to that row; Organized List opens current
publication files. [Publication Rules](docs/publication_rules.md) defines the
exact scope, selection and review dependencies.

PDF comparison is an optional OCR-based review aid, **not publication approval**.
It tolerates some similar characters, whitespace and word hyphens; inspect
unlocated ranges, figures, tables, formulas and layout manually.
See [PDF Comparison](docs/pdf_comparison.md).

## Data And Recovery

| Data | Location / purpose |
| --- | --- |
| Native database | `db.sqlite3` by default |
| Native uploads and generated files | `data/`, including `data/media/` |
| Docker conference data | Project-specific named volume; `SMS_DATA_DIR` is its backup mirror, not the live volume |
| Private deployment settings | Per-conference `.env.*` and `runtime/comparison-slave/` |
| Audit trail | `data/logs/audit.log` inside the application storage |

Use System State Backup / Restore to move a conference, including review
images, revision history and audit logs. It excludes deployment credentials,
OCR models and temporary previews. See [Operations](docs/operations.md) before
maintenance, cleanup or restore. Never delete a conference volume to update it.

[Changelog](CHANGELOG.md) records releases. Current app identity lives in
`submissions/version.py`; archive compatibility is separate.
Import templates are available in the app and [sample_data](sample_data/).
