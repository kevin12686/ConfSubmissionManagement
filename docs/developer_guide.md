# Developer Guide

Start with the [README](../README.md) vocabulary and
[Publication Rules](publication_rules.md) before changing workflows.
Deployment instructions live in [Deployment](deployment.md), not here.

## Native Setup

Python 3.12+ is required. From the repository root, macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
python -c "from pathlib import Path; from scripts.deployment_provision import provision_models; provision_models(Path('runtime/rapidocr-models'))"
python scripts/check_comparison_runtime.py
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

On Windows, create the venv with `py -3 -m venv .venv`, activate with
`.venv\Scripts\activate`, then run the same `python` commands.
Use `start_windows.bat` or the native launchers after this first setup.

The explicit provisioning step downloads missing pinned models, validates
checksums and refuses invalid existing files. Comparison requests and native
launchers do not download models. Offline transfer can use:

```bash
python manage.py prepare_rapidocr_models \
  --source /absolute/path/to/approved-models \
  --destination /absolute/path/to/checkout/runtime/rapidocr-models
```

Default lookup is checkout-local `runtime/rapidocr-models/`. An external
directory requires absolute `CFM_RAPIDOCR_MODEL_DIR` in the actual server
environment. Do not use a research venv or unverified model substitution.

Native Django does not automatically read conference Compose `.env.*` files.
Set native environment variables in its launcher shell. For isolated work, set
`SMS_DATABASE_PATH` and `SMS_MEDIA_ROOT`; many services also use
`BASE_DIR/data`, so use a **disposable checkout** for full integration/browser
tests, not just a different SQLite filename.

## Code Map

| Location | Responsibility |
| --- | --- |
| `conference_final_manager/` | Settings, root routes, common worker HTTP/config |
| `submissions/urls.py` | Canonical application routes |
| `submissions/controllers/` | Parse HTTP/form inputs, call workflows, render/redirect/download |
| `submissions/application/selectors.py` | Read-only page/query composition |
| `submissions/application/commands.py` | Workflow command wrappers |
| `submissions/services/` | Domain rules and transactions |
| `submissions/presentation.py` | Shared display vocabulary, never business decisions |
| `submissions/models.py` | Persistent records and compatibility state |
| `submissions/templates/`, `submissions/static/` | Django UI, shared HTMX/JS behavior |
| `comparison_slave/` | Independent worker server, queue and GPU lifecycle |
| `scripts/` | Deployment, maintenance, acceptance tools |
| `submissions/tests/` | Django/service and JavaScript regressions |
| `tests/` | Standalone deployment/transport tests |

### Where To Add Logic

| Change | Owner |
| --- | --- |
| Publication selection/read snapshot | `file_manager.py`, `publication_read.py` |
| Scope/Not Publishing | `publication_decisions.py` |
| Readiness, author counts | `checks.py` |
| Active selection/processing | `pdf_processor.py`, `recompute.py` |
| Imports/reset dependencies | `import_preview.py`, `import_export.py` |
| Record edit preview | `record_edit_preview.py`, `manual_edit.py` |
| Signed workflow state | `workflow_evidence.py` |
| Formatting/revisions | `formatting.py`, `formatting_history.py` |
| Extraction/evidence renderer | `title_author_extraction.py`, `title_author_verification.py` |
| Advisory comparison | `pdf_comparison.py`, `pdf_comparison_execution.py`, `rapidocr_comparison/` |
| Worker transport/observation | `comparison_client.py`, `comparison_health.py`, `comparison_metadata.py` |
| CrossCheck | `crosscheck*.py` |
| Exports/XLSX | `reports.py`, `excel_workbook.py` |
| Storage/backup | `storage_inventory.py`, `system_state.py` |
| Audit | `audit.py`, `audit_actions.py` |

All service names above are under `submissions/services/`.
Inspect callers before changing a shared function; a presentation fix must not
quietly change publication rules.

## Write Contracts

- Use existing command/service boundaries. No direct field edits in templates,
  GET handlers or worker code.
- Keep `FinalSubmission` and its state mirrors synchronized through
  `final_submission_state.py`; use `bulk_update_submissions()`, not raw
  `bulk_update()` for mirrored fields.
- Validate signed evidence before write, recheck files/current scope after
  expensive work, and reject stale data rather than merging.
- Reuse dependency reset helpers. Do not reset all reviews to simplify a feature.
- Coordinate DB rollback with file staging/promotion; filesystem writes are not
  automatically undone by `transaction.atomic()`.
- Preserve Formatting baseline/revision capture on file replacement; source-only
  and PDF-only uploads must retain complete historical pairs.
- Keep preview tokens temporary, expiring and excluded from System State.
- Record requested/success/failure events where the workflow requires them.
  Audit includes changed fields, before/after, reset/file effects and hashes;
  never tokens, credentials, binary content or arbitrary external error prose.
- Register new canonical audit names centrally as
  `<domain>_<operation>[_<phase>]`; preserve legacy aliases without rewriting logs.

## Comparison And Worker Changes

Read [PDF Comparison](pdf_comparison.md) and the
[worker protocol](../comparison_slave/README.md). The app owns file selection
and signing; workers compute exact supplied bytes only.

Keep models/300-DPI rendering/recognition independent of scheduling changes.
Cache identity must bind exact bytes and validated runtime; model paths,
health metadata and release labels cannot authorize reuse. Never replay an
uncertain accepted request on a different worker.

GPU residency shares weights only. Preserve finite cgroup memory, per-job
timeouts, process-group cleanup, parent-death protection, authenticated/bounded
IPC and namespace signing isolation. Failed reaping must block replacement.
Health/cache access must not keep idle model sessions alive.

Use actual OCR to validate model/runtime changes. Frozen OCR proves comparator
parity only; mocked CUDA is not GPU inference. Keep measurements separate from
accuracy. See the [Acceptance Runbook](editorial_acceptance_runbook.md).

## Regression Commands

Run the full gate for code, workflow, UI, storage, comparison or deployment changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
.venv/bin/python manage.py test submissions
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node --test submissions/tests/js/*.test.mjs
.venv/bin/python -m compileall -q submissions conference_final_manager comparison_slave manage.py scripts
git diff --check
```

For documentation-only changes, the required subset is:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
git diff --check
```

Use the matching venv executable on Windows. Network/socket and Docker opt-ins
need their real isolated acceptance runs when affected; report skips explicitly.
Browser QA must check rendered behavior and pixels, not only HTML or status codes.
Never use production conference data/containers for tests.

## Test Evidence

For publication-related changes, assert before/after candidate identity,
selected PDF/source hashes, readiness, review/reset state and exact Publication
ZIP/CrossCheck bytes. Test stale previews, concurrent updates, missing selected
Corrected files, ambiguous scope and rollback on file/DB/audit failure.

For UI changes, test normal and HTMX navigation, lazy fragments, pagination,
keyboard/touch, narrow/short viewports and loading failures.
For comparison, add actual PDF/OCR edge fixtures plus frozen-engine tests.
Do not fit rules to one submitted paper or claim zero findings proves integrity.

README screenshots use synthetic disposable conference data only. Keep the
current assets under `docs/assets/readme/`; do not capture live submissions.
Use fictional names in examples and fixtures. Documentation, screenshots and
file metadata must not expose the maintainer's legal name; public attribution
uses Kevin Hsu.

## Versions And Releases

| Version | Owner | Change when |
| --- | --- | --- |
| App release | `submissions/version.py` | User-visible runtime/workflow/UI/export changes |
| Comparison method | `pdf_comparison.py` and `rapidocr_comparison/result.py` | Result semantics/schema or inspection compatibility changes |
| Compute contract | Code/model fingerprint | CFM and workers must run compatible code; release labels are not compatibility |
| State archive | Django settings / explicit restore adapters | Persistent ZIP schema or restore compatibility changes |

Docs-only reorganization does not change runtime/archive versions. Historical
behavior belongs in Changelog/Git, not interleaved into current instructions.
Archive v8 supports explicit v5/v6/v7 adapters; never accept an incomplete newer
payload as an older archive.

Release checklist:

1. Update the canonical rule/guide and Changelog; repair links.
2. Run the appropriate gate and isolated acceptance; record tested/untested scope.
3. Test final **and** draft exports if publication behavior changed.
4. Test current archive round-trip and supported legacy adapters if state changed.
5. Update CFM and workers together for compute changes.
6. Commit the related code/tests/migrations/docs together. Do not commit private
   envs, conference data, models, experiment workspaces or credentials.
