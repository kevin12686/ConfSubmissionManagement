# Developer Guide

This guide is for maintaining the Django project.

Use this guide for environment setup, service ownership, data dependencies,
tests, and release work. Shared presentation rules live in
[UI Conventions](ui_conventions.md); publication-facing business rules live in
[Publication Rules](publication_rules.md). Architecture rationale lives in
[Architecture Notes](architecture.md). The complete guide map is in the
[Documentation Home](README.md).

## Local Environment

Use the same virtual environment as the app:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Provision the approved models below, then check the selected runtime.
python scripts/check_comparison_runtime.py
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

macOS operators usually run `start.command` or `./scripts/start_local.sh`. Windows operators run `start_windows.bat`.

## PDF Comparison Runtime

Method 8 requires `rapidocr==3.10.0` and `onnxruntime==1.31.1` from
`requirements.txt`, with the approved PP-OCRv6 small detection/recognition models.
Use the application's Python environment on macOS/Linux/Windows; a successful
installation on one host does not establish Linux/container compatibility.
Do not use a temporary research environment or silently substitute other models.

The unified Docker deployer provisions missing models automatically during
deployment. `scripts/deployment_provision.py` reads the canonical `MODEL_HASHES`
literal without importing native dependencies, uses bounded HTTPS downloads,
stages/checksums both models, and installs missing regular files without
overwriting existing paths. Keep model/version policy in the inference runtime;
do not duplicate it in launchers. Tests must cover offline/dry-run, corruption,
truncation, concurrent provisioning, symlinks and preservation of existing files.

For native/manual provisioning, obtain the approved files explicitly:
`PP-OCRv6_det_small.onnx` and `PP-OCRv6_rec_small.onnx`. RapidOCR 3.10.0's
`default_models.yaml` lists these versioned upstream downloads:

- [Detection model](https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx)
- [Recognition model](https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx)

Download both into a separate local provisioning directory, retaining the exact
filenames, then run the importer below. If an upstream download is unavailable,
transfer these same checksum-verified files from another provisioned machine;
do not substitute another model or bypass verification. The importer validates
the pinned SHA256 values in
`submissions/services/rapidocr_comparison/runtime.py`. The default lookup directory
is `BASE_DIR/runtime/rapidocr-models`, outside conference-managed storage. From
the repository root, import the files there before starting comparisons:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py prepare_rapidocr_models \
  --source /absolute/path/to/approved-models \
  --destination "$(pwd)/runtime/rapidocr-models"
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

The source above is a deployment placeholder, not a supplied model location.
The explicit destination selects the default runtime directory; no environment
variable is needed for that location. On Windows, supply the absolute checkout
path followed by `runtime/rapidocr-models` instead of the shell substitution.
Model files must be regular, non-symlink files with the approved checksums.
The command only copies/validates local files; it never downloads models.

For an external directory, pass an absolute `--destination` and set
`CFM_RAPIDOCR_MODEL_DIR` to that same absolute path in the actual launcher/service
environment. Restart the server after changing its environment. An override
takes precedence over the default; invalid/missing files at the selected path
do not authorize fallback to another model directory. Docker uses container
paths, as described below. No external-directory override is necessary for the
normal checkout-local setup.

No comparison request downloads models. Embedded mode does not call network
inference; service mode explicitly uses the configured trusted worker. Missing,
corrupt or mismatched models/dependencies cause Comparison unavailable, never
native-text fallback. Local inference and actual rendered-PDF acceptance must be
verified after provisioning. Model installation is a machine prerequisite, not
System State data: provision it again on a restored/new host. Keep model files
out of publication directories, report cleanup and state archives. See the
[Docker Guide](docker_guide.md#pdf-comparison-runtime) for container paths.

Deployment CPU parallelism is configured separately with `CFM_RAPIDOCR_THREADS`
(1-8, default up to 4, within visible CPU capacity). Keep models, DPI and result
rules unchanged when tuning threads; compare actual OCR outputs, not only timing.
Container CPU allocation and hyperthreads do not imply linear speedup. Preserve
the wall timeout, bounded Linux CPU-time accounting across threads, and explicit
failure behavior. Test Docker environment retention when changing this setting.

## CPU Comparison Cache Acceptance

CPU comparison optimization is owned by `pdf_comparison_cache.py` and the
worker's bounded job-artifact protocol. See the
[cache contract](pdf_comparison.md#local-cpu-evidence-reuse). Never use a cache
hit to bypass model checks, publication scope, current-file hashing or saved
revision binding. Preserve byte-identical BGR rendering and recognition batching.
Changes require cold, warm-pair and original-only reuse acceptance against frozen
pre-change results, including OCR scores/coordinates, plus corrupt/expired cache,
concurrency, failure and publication ZIP/CrossCheck byte-integrity tests. Record
actual timing separately from semantic accuracy; a faster retry does not prove
that a first-time CPU comparison will finish within 240 seconds.

## Shared Compute Development

`pdf_comparison_execution.py` is the common isolated execution entry point.
It depends on minimal settings for signed cache storage, not application models.
`comparison_slave/` owns its independent job ledger, bounded scheduler and
authenticated namespace storage. Do not reuse the conference SQLite database,
mount its media, or import publication services from the worker.

`comparison_client.py` owns transport only. Preserve pre/post source/revision
checks in `pdf_comparison.py`, including missing-Corrected fail-closed behavior.
Keep protocol, ordered byte hashes, schema validation and inspection-render
checks mandatory. Add tests for authentication, per-client isolation, duplicate
POSTs, queue bounds, restart/interruption, lost responses, incompatible code,
CPU/GPU identity, corrupt results and source changes during compute. Verify
publication ZIP/CrossCheck bytes and readiness/review state remain unchanged.

Service mode may not silently execute embedded OCR when routing fails. Never
retry an uncertain accepted remote request on a second worker.
With a remote URL, `_select()` must not silently choose local; only a validated
short-lived confirmation from `comparison_execution_choice.py` grants local use
for the exact submission/baseline/hashes/revision/configuration. Keep old local
routes, but require consent before submitting/retrying/waiting for unfinished
local computation; completed results may be reused. Keep health observations
separate: `comparison_health.py` does bounded authenticated GETs and does not
call routing/submission or publication services. The Formatting-only poller
pauses while hidden, prevents overlap and never starts comparisons. Test timer,
abort, HTMX and stale green-state behavior. Snapshot execution labels must use
recorded actual backend, not live health; legacy missing metadata stays unknown.
Deployment credentials and worker caches are not conference backup contents. Run the
standalone Docker smoke in disposable volumes; never reuse conference containers
or existing service volumes for acceptance. GPU acceptance must state the actual
provider/hardware tested; mocked CUDA setup tests are not GPU inference tests.

## Docker Development Boundary

Deployment preflight lives in `scripts/check_comparison_runtime.py`, outside
conference Django initialization. Native launchers and the Docker web entrypoint
call it before migrations; create/rebuild/migration scripts run it in the
candidate image before stopping/replacing the existing web container.
Candidate probes also run Django system checks against in-memory SQLite without
migrations, so form-import queries cannot create a conference database. Embedded
mode actually loads both pinned OCR sessions in a bounded child. Service mode
uses authenticated health/contract checks, requiring a configured local fallback
even when the preferred remote is available. It never submits papers or reads
conference data. Optional GROBID/CrossCheck services are not startup prerequisites.

`scripts/start_comparison_slave.py` owns independent worker build/preflight/start.
`scripts/deploy.py` orchestrates first setup and updates around that interface
and `docker_update_engine.py` (the extracted conference planning/cutover guard
layer). `update_docker_instances.py` delegates to the same update workflow, not
a second implementation. Its private per-project enrollment files,
worker-source fingerprint and deployment log are host state under `runtime/`,
not conference models or System State contents. Preserve existing user values
and token bytes; tests must reject conflicting enrollments and exercise
failure restoration and busy-worker refusal. The idle check is not an atomic
drain, so maintenance must exclude new comparison submissions.
Worker disposable preflight must not change persistent identity, clients or
jobs. Keep CPU/GPU selection explicit. Backup uses the image's
`com.conferencefinalmanager.startup-preflight=1` capability label to check restart
prerequisites before stopping a new-image web; legacy images retain their former
backup behavior. These checks do not change publication rules.

Docker procedures are centralized in the [Docker Guide](docker_guide.md).
`conference_final_manager/comparison_config.py` is the standard-library-only
owner for optional `CFM_COMPARISON_REMOTE_HEADERS` JSON validation. Deployment,
startup checks and `comparison_client.Endpoint` must use that parser; only
remote endpoints receive the headers. Preserve Bearer/protocol headers,
case-insensitive duplicate rejection and secret-safe errors. Header rotation
does not change the signed route identity (worker URL/token) or OCR contract.
Docker management must retain/redact the entire setting. Compose config output
escapes literal dollars; normalize environment values at that structured output
boundary, and escape literal dollars when generating scalar env assignments.
Let Compose resolve existing variable references instead of expanding them in
the deployer. Preserve JSON/quotes/dollars and existing signing-key bytes across
both rendered plans and generated env files.
Test actual HTTP and Compose round-trips, not only mocked `Endpoint.call()`.
`comparison_http.py` owns the descriptive `ConferenceFinalManager/1` User-Agent
and secret-safe network/HTTP diagnostics. Preserve explicit custom remote UA
overrides, but never require one for normal Cloudflare operation. Startup errors
must retain safe HTTP/provider codes (1010 is not bad-token proof), bounded
non-secret Ray IDs, redirects/non-JSON and DNS/TLS/timeout/refusal categories.
Do not print response prose, cookies, Location URLs or request credentials.
For the opt-in actual container env check, build a disposable app image and run
`CFM_HEADER_TEST_IMAGE=<test-image> .venv/bin/python -m unittest discover -s tests -p 'test_comparison_headers_docker.py'`.
It creates/removes unique test projects with fake credentials and no conference
mounts. Normal standalone discovery skips this explicit Docker-only check.
Run the real isolated updater acceptance with
`CFM_UPDATE_DOCKER_ACCEPTANCE=1 .venv/bin/python -m unittest discover -s tests -p 'test_update_workflow_docker.py'`.
It requires Docker, installed test dependencies and the pinned local models;
it creates a disposable checkout, unique images/networks/volumes and two
generated conferences, exercises both update entrypoints and checks database,
env/token preservation. It never mounts existing conference data. The model
directory is read-only. Normal discovery skips this explicit acceptance test.
Developers should use that guide for instance creation, environment ownership,
updates, backups, migration, and rollback.

The development-facing boundaries are:

- Docker runs Django/Gunicorn as `web` behind the published Nginx `proxy`.
- Conference state lives in the project-scoped `sms_data` volume; collected
  static assets and gateway status are rebuildable or temporary.
- Keep one Gunicorn worker with SQLite.
- Nginx must preserve the public Host header, serve static/media independently
  of `SMS_DEBUG`, and never participate in publication file selection.
- Code or environment changes are applied through
  `scripts/update_docker_instances.py`, beginning with `--dry-run`;
  `scripts/deploy.py update` is the same orchestration entry point. Prepare
  shared-worker clients once per selected batch, never once per conference.
  New env-declared projects still require `--create-missing`. Dry-run performs
  actual static/Compose planning with private temporary env staging but no
  persistent writes, live HTTP probes, builds, downloads or restarts.
- Validate all prepared static plans before infrastructure mutations. Keep each
  env commit scoped to its own apply: a later failure must not restore an earlier
  updated conference's old settings. A typed `CutoverStartedError` preserves
  recovery configuration after a replacement was attempted. Do not describe a
  batch as transactional or automatically roll back migrations/containers.
  Missing env signing secrets and worker connections must retain the existing
  instance's live values, not rotate keys or silently switch compute endpoints.
  Never carry an old token/header set to a different explicitly selected URL.
- The Docker operation scripts share a lock and must retain the current data
  mount type until an explicit migration or rollback.
- Never use `docker compose down -v` for a conference instance.

Changes to Compose, Nginx, gateway behavior, or the Docker management scripts
must update the Docker Guide, affected troubleshooting entries, architecture
rationale, and Stage 8 of the acceptance runbook.
Record measured acceptance outcomes and hardware limitations in
[Deployment Validation](deployment_validation.md), keeping historical runs
separate instead of interleaving benchmark reports with operator instructions.

## Regression Commands

For comparison layout changes, also exercise the real comparison page with
isolated signed PDF snapshots in desktop, short and narrow viewports. Assert
that the top inspection dock precedes documents, magnification has no internal
overflow, Evidence alone can scroll, and switching views does not shift the
document headers. Retain coverage for absent counterpart pages, image failures,
paired-anchor boundaries, close/reopen and pointer-relative canvas aspect ratios.
These layout changes must not modify the comparison method or file identities.
For connection positioning, verify real SVG endpoints and actual crop centers,
not only status labels. Cover Next/Previous, keyboard/click, multi-region and
candidate connections, reverse hover, edges, hidden/offscreen lines, current-page
changes and stale detail callbacks (including identical URLs for different findings).
For small Fit-page previews, assert the actual destination/source draw scale per
PDF point, not just canvas-width/source-width (which ignores padding). Exercise
both positioning modes, different PDF sizes, DPR and the canvas backing-size cap.
Detail rendering must read through `comparison_pdf_bytes()`, never resolve a
new publication source. Test DPR 1/2/3, rendering bounds/rotation, tampered and
stale snapshots, pixel equality to the selected PDF, debouncing, timeouts and
late responses. Assert the original preview/render hashes, signed result payload,
review state and readiness remain unchanged after requesting detail images.

Run these before finishing code changes:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
.venv/bin/python manage.py test submissions
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node --test submissions/tests/js/*.test.mjs
.venv/bin/python -m compileall -q submissions conference_final_manager comparison_slave manage.py scripts
```

For documentation-only changes, run at least:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python scripts/check_docs.py
```

## Project Structure

- `conference_final_manager/`: Django settings and root URL config.
- `submissions/controllers/`: HTTP views grouped by workflow.
- `submissions/application/selectors.py`: page/query context builders.
- `submissions/application/commands.py`: workflow command wrappers.
- `submissions/presentation.py`: canonical user-facing worklist vocabulary;
  business logic must never branch on these display strings.
- `submissions/services/`: domain services.
- `submissions/templates/submissions/`: server-rendered Tabler/Bootstrap-compatible templates and shared partials.
- `submissions/tests/`: acceptance regression tests and factories.
- `sample_data/`: CSV templates.
- `docs/`: documentation home plus operator, Docker, developer, architecture,
  troubleshooting, rules, UI, and acceptance guides.

## README Preview Assets

README screenshots live under `docs/assets/readme/`. Capture them from a
disposable database and media root populated only with synthetic conference,
paper, author, and submission data. Never capture the operator's normal
`db.sqlite3` or `data/` tree.

Update the affected preview when a user-visible Dashboard or Organized List
change makes the current image misleading. Keep the image format, extension,
alternative text, and README link consistent, then run the documentation gate.

## Where Logic Belongs

PDF browsing is presentation-only. Use the `pdf_viewer_url` template filter for
an existing PDF URL, or `{% pdf_viewer 'submissions:route_name' ... %}` for a
named PDF endpoint. Load `pdf_viewer` in each template/partial that uses it.
Do not wrap source/ZIP downloads, images or comparison-workspace URLs. Keep
ordinary PDF download links raw. New PDF endpoints must be explicitly added to
the reader allowlist, with tests proving their original file selection and
failure behavior remain unchanged. Do not teach the viewer how to resolve
publication files, access filesystem paths or proxy remote PDFs.

The shared reader uses pinned local PDF.js display, worker and viewer assets
under `static/submissions/vendor/pdfjs-6.4.299/`. License/provenance and upgrade
instructions are in that folder's `CFM-NOTICE.md`. Keep all three versions in
sync, retain CMaps/fonts/WASM/ICC resources and licenses, and disable PDF
scripting, annotation editing and form filling. Do not add another canvas/text
matching implementation, a CDN or application file writes. Browser QA must
cover actual canvas pixels, selection/search, page/zoom controls, narrow widths,
short/landscape windows, load errors, local-only runtime requests and
direct/download escape routes. The reader uses base-template layout blocks to
omit work-page chrome, not a second copy of shared styles or a fixed-position
overlay. Keep viewport sizing scoped to reader classes and verify other pages
retain their Navbar/footer, normal scrolling and workflow alerts. Layout changes
must not change source URLs, PDF.js loading, file helpers or snapshot binding.
The reader's Fit page/Single page defaults and optional Continuous scrolling
use PDF.js's public scale/scroll-mode APIs. Test current-page/zoom preservation,
cross-page search, first/last-page controls, and zoomed-page panning in both
modes, including mixed page sizes/orientations. Coalesce native fit-preset
updates after PDF.js page transitions and on viewport resize; numeric zoom
must remain unchanged. Do not hide PDF.js page nodes yourself.
The separate reader-only `pdf_navigation.mjs` owns wheel gesture and held-key state,
not file loading or PDF geometry. It delegates zoomed-page panning to the browser,
requires a fresh gesture/keypress at the boundary and uses native page navigation.
Keyboard and wheel must share boundary/entry positioning, not duplicate it.
Settle queued entry positioning before interpreting a fresh input so rapid
opposite-direction keys cannot read the previous page's scroll offset.
Bind keydown only to the PDF container; do not interfere with controls, modifiers,
IME, search or native continuous-mode vertical scrolling. Release held-key state
on keyup/focus loss/blur and unregister listeners at teardown. Initial document
focus must not steal an already focused input or control.
Test momentum, direction changes, line/page delta modes, first/last-page bounds,
modified/horizontal input, cleanup and native fit updates with:

```bash
node --test submissions/tests/js/pdf_navigation.test.mjs
```

This uses Node's built-in test runner (Node 20+), without npm packages. Run it
alongside the Python gate when changing reader navigation behavior; Node is not a
runtime requirement for the application. Also verify actual wheel input in
Chromium/WebKit, including zoomed-page top/bottom entry, key repeat, input focus,
wheel/keyboard interleaving, and continuous mode.
Avoid re-navigating to an unchanged page number: Enter followed by input blur
must not reattach a single-page view and swallow the user's next link click.

`pdf_thumbnails.mjs` owns the reader's collapsible sidebar. Pass the exact
already loaded `PDFDocumentProxy`; never call `getDocument`, fetch a second PDF,
select a file or clean up a page proxy shared with the main viewer. Use native
page render/getViewport APIs, serial rendering, near-visible IntersectionObserver
requests and a 24-canvas cache target (retain current/near-visible pages).
Cancel/invalidate work on close/rotation
or teardown; late promises must not install stale pixels. Page/rotation events
come from the existing viewer, and clicks use its public page-number property.
Test lazy rendering, cache eviction, failure/retry, cancellation, focus, mobile
selection and one-document binding with:

```bash
node --test submissions/tests/js/pdf_thumbnails.test.mjs
```

Browser QA must also check actual thumbnail canvas pixels, independent scrolling,
search/keyboard synchronization, mobile overlay/auto-close, resize/native fitting,
rotation and unchanged raw endpoints/download bytes. Never persist browser
thumbnails to database workflow state or managed media.

Keep controllers thin. A controller can validate forms, choose commands, set messages, redirect, and render templates.

Put reusable workflow behavior in services:

- Import preview/apply: `import_preview.py`.
- CSV/XLSX parsing and templates: `import_export.py`.
- PDF processing: `pdf_processor.py`.
- Publication file resolution: `file_manager.py`.
- Paper Master publication decisions: `publication_decisions.py`.
- Paper ID verification: `verification.py`.
- Title/author extraction and manual override: `title_author_extraction.py`, `builtin_title_author_extractor.py`, and optional `grobid_extractor.py`.
- Formatting workflow: `formatting.py`.
- Immutable Formatting snapshots: `formatting_history.py`; these are evidence,
  never publication input. Use `revision_files_transaction()` around the owning
  DB transaction so newly created files are compensated on failure. Capture an
  unrecorded corrected pair before original-file invalidation. Do not add file
  copies to migrations or create revisions from GET requests.
- Read-only Formatting PDF comparison: `pdf_comparison.py` and the isolated
  RapidOCR comparison engine. Keep After on the publication helper and Before
  on Original/verified revision readers. Builds use audited POSTs; GETs only
  validate/read existing signed snapshots. Never call processing, Formatting
  save, author-cache rebuild or workflow recomputation from comparison.
  Method 8 uses rendered-page RapidOCR, not the retired method-6 font/glyph
  proof engine. Native geometry guides region/row order only; it never supplies
  OCR text or an equality shortcut. Preserve raw OCR text, scores and boxes,
  headers/footers, image/formula text and low-confidence recognition.
  Keep the approved context anchors, bounded small-edit alignment, field
  ownership and complete one-to-one accounting. Do not cancel repeated values
  globally, infer a location from a reference page, or tune per paper.
  All 38 direct visual groups are enabled by the broad policy. Only a wholly
  explained local difference is tolerated; a mixed real edit keeps its original
  finding. Preserve tolerated occurrences as neutral information with rule IDs
  and risks. Empty OCR, unlocated ranges and exceeded work bounds cannot look
  unchanged. Explicitly test real similar-character/whitespace blind spots.
  See the [comparison contract](pdf_comparison.md) for the full policy.
  Run rendering/OCR/alignment behind the worker boundary with exact byte
  snapshots, bounded inputs/work and parent validation. Keep Django, database
  and publication metadata out of the engine. Missing models/dependencies or
  worker errors fail explicitly; never silently substitute native text or
  download models in a request. Production modules must not import research
  scripts or require the research virtual environment.
  Advance temporary method versions for algorithm/result changes; old signed
  results and cached automatic requests must rebuild, not be relabeled.
  Preserve upload receipts, revision binding, audit, expiry and archive exclusion.
  Keep differences, unlocated ranges and information separately counted; coverage
  metadata and display caps cannot imply safety. The document-first UI consumes
  actual side-specific locations, raw text and explanations without reclassifying
  evidence. Keep selected Evidence independent of pointer motion; use unambiguous
  region anchors with persistent offsets or relative positioning. Missing linked
  pages must clear opposite images, marks and crops. Keep
  crop labels/status distinct from recorded finding evidence; never load a
  different page to fill a crop. Hover inspection must not navigate or scroll
  either PDF. Cover unmarked/zero-finding pages, one-sided and off-page findings,
  unequal dimensions, image failure/retry and stale animation/image callbacks.
  Run `node --test submissions/tests/js/pdf_comparison.test.mjs` and actual-payload
  browser QA in addition to the regression gate. Test real rendered-PDF OCR,
  stale/absent/contradictory native layers, order/value swaps, reflow, repeated
  deletions, mixed tolerated/real edits and failure paths. Record exact fixture
  hashes and expected locations; research counts or synthetic UI tests are not
  production acceptance. Verify unchanged database/review/readiness state and
  byte-identical publication/CrossCheck output.
  Record the PyMuPDF extraction version as well as OCR/model versions: native
  block/line geometry can change despite identical raster pixels and recognition.
  Frozen-input parity and fresh PDF acceptance are separate gates; do not change
  the publication PDF runtime just to reproduce an experiment's finding count.
- Automatic post-upload comparison: `automatic_pdf_comparison.py`, with the
  shared `formatting_comparison_status` partial/CSS/JS. Prepare a signed receipt
  only after a successful save returns its exact `FormattingSaveResult`;
  never infer the saved event with a later latest-revision query. Preserve the
  legacy save/apply return contracts through their thin wrappers. Do not run
  extraction/comparison in the save transaction or turn comparison exceptions
  into upload failures. GET reads status only; build/retry is CSRF-protected
  POST, bound to the saved revision and current publication bytes. Revalidate
  that binding on cached-result access and prohibit fallback to newer files.
  Deduplicate builds under Django's portable file lock and reuse verified
  signed results. No polling or background thread is needed. Test stale and
  concurrent uploads, interrupted/repeated requests, storage/audit failures,
  title guard confirm/cancel, and List/Single/Focus/HTMX behavior.
- CrossCheck/plagiarism: `crosscheck.py`.
- Automatic CrossCheck HTTP: `crosscheck_client.py`; preview/job/result apply:
  `crosscheck_service.py`. Reuse the manual export's publication bytes and
  signed provenance. Never select PDFs from debug/current paths or infer
  success from a job's completed status. External POST timeouts are uncertain
  submissions and must not be retried automatically.
  Batch countdown/polling UI uses one timer and at most one automatic request
  in flight. Check timing, visibility pauses, errors and completion reload with
  `node --test submissions/tests/js/crosscheck_polling.test.mjs` (Node 20+);
  these tests do not contact the service.
- Signed multi-editor evidence: `workflow_evidence.py`.
- Readiness and author checks: `checks.py`.
- Exceptions: `exceptions.py`.
- Report data and publication ZIPs: `reports.py`.
- Shared XLSX presentation: `excel_workbook.py`. Keep CSV/package output out of
  this formatter; CSV schemas are machine-readable contracts. Editorial
  Workbook supporting sheets must use the whitelist in `reports.py`; raw/debug
  sheets stay separate from the workbook selector. All Excel exporters must use
  the shared atomic/audited workbook transaction in `reports.py`; do not write
  final XLSX paths directly.
- Storage cleanup: `storage_inventory.py`.
- Backup/restore: `system_state.py`.
- Audit logging: `audit.py`.
- Final Submission state persistence and batch writes:
  `final_submission_state.py`.
- Active/duplicate derived-state coordination: `recompute.py`.

Do not put processing or integration logic directly in views.

The built-in extractor must derive stored author text, author count, and
verification-renderer input from one parsed author list. Do not infer author
count from punctuation or independently re-split the formatted display string.

Shared worklist wording belongs in `submissions/presentation.py`. Keep database
choices, filter query values, readiness checks, and workflow transitions based
on stable internal values. A wording change must not alter publication
eligibility or require templates to infer state from rendered label text.

Storage inventory code must preserve the request boundary in
`storage_inventory.py`: collect database references once, scan each managed
root once, and classify from the resulting index and file records. Do not add
per-file database queries, per-reference filesystem walks, or repeated path
`stat()` calls. Treat directory references and exact file references
explicitly. Overlapping roots must use the documented category protection
priority; never use first-seen/last-seen iteration order to decide whether a
publication-managed file is generated cache. Cleanup apply must recheck both
current database references, current policy classification, and the previewed
filesystem identity. Report-folder cleanup must preserve known
non-regenerable managed subtrees even if folder settings overlap. The Settings
controller must not synchronously build the inventory
or contact GROBID; those operations belong to their separate UI/JSON
endpoints. Read-only middleware, context processors, Settings GET, and storage
inventory must use `AppSetting.read()`; `AppSetting.load()` is reserved for
workflows that are allowed to persist the default singleton.

Clear Database filesystem staging, rollback, and quarantine disposal belong to
`storage_inventory.py`. The Settings controller owns confirmation, the atomic
database reset, user messages, and audit orchestration; it must not recursively
delete configured folders directly.

## Final Submission Write Rules

`FinalSubmission` remains the compatibility source of truth while five
one-to-one state tables mirror its lifecycle domains.

- Keep all compatibility-to-state field mappings in
  `submissions/services/final_submission_state.py`.
- Ordinary model saves use the model `save()` path, which performs
  domain-aware state upserts.
- For several existing submissions, use `bulk_update_submissions()` instead of
  direct `bulk_update()`. It preserves derived review fields, timestamps, and
  state rows in one transaction.
- Use `sync_all_submission_state_records()` for repair/restore and specify
  domain keys when a workflow changed only one lifecycle domain.
- Use `defer_submission_state_sync()` only inside a short outer transaction.
  Long PDF, file, or remote-service loops must flush bounded batches.
- Use `recompute_active_and_duplicate_state()` whenever both active and
  duplicate/replaced values may change.
- Bulk APIs bypass model signals by design. Every new mirrored field must be
  added to the central mapping; mapping-coverage tests enforce this contract.

Organized List may expose paper-level exception actions, but it must reuse `exceptions.py` row builders and approve/remove services. Do not duplicate page/author/plagiarism exception validity rules in templates or controllers. Author paper-count exceptions remain author-level and belong in Author Count / Exceptions, not a single paper row.

Organized List exception POSTs replace one stable per-submission `<tbody>`.
After every action, rebuild and hydrate the complete row from a fresh
`PublicationReadContext`; do not patch badge text from JavaScript. Every
exception textarea has a type-specific draft field. The controller may carry
those drafts into the replacement row only when that section has no persisted
reason. Persisted backend state wins, successful remove/reset clears the target
draft, and validation failure preserves the submitted target draft with an
inline error. Drafts are presentation state only and must never be passed to
another exception service or stored implicitly. Keep ordinary POST/redirect as
the no-HTMX fallback.

Error Report exception actions use the same exception-row keys, signed
evidence tokens, approve/remove services, and audit path. Readiness findings
may carry an additive `exception_key` plus the expected Final Submission scope;
that metadata must never replace or weaken the readiness condition. Rebuild the
complete Error Report worklist from a fresh `PublicationReadContext` after an
HTMX action because severity, category counts, pagination, and the finding
itself can all change. Preserve the active known category when its count drops
to zero so the editor can see that the selected blocker was resolved. Unknown
category query values remain invalid. Keep normal POST/redirect as the
non-HTMX fallback.

Organized List `Details` is the publication-record view for the active row. Its
authors must come from that submission's `extracted_authors`, and its files must
come from the publication-facing helpers. Do not substitute Paper Master authors,
another Final Submission version, legacy current paths, or debug copies as the
publication source.

For display, the Details author list is parsed with the shared `split_authors()`
helper and numbered in publication order. This is presentation only; never
rewrite `extracted_authors` while preparing the display list.

## Shared UI And Worklists

[UI Conventions](ui_conventions.md) is the canonical guide for worklists,
feedback, exact navigation, pagination, partial updates, evidence rendering,
and accessibility. Keep the following implementation boundaries when applying
that guide:

- Controllers perform lightweight selection, filtering, sorting, and
  pagination before expensive row hydration.
- Signed evidence is generated only for the displayed page or exact focused
  record.
- `PublicationReadContext` and its `FileInspectionContext` remain explicit
  request-scoped objects; do not replace them with controller caches, globals,
  or writes from GET requests.
- Final export uses strict fresh file validation and snapshot byte reads as
  defined in [Publication Rules](publication_rules.md#export-integrity).
- `_worklist_return_url()` and `_formatting_redirect_after_save()` preserve
  filter, search, page size, page, and card context for audited POST redirects.
- Final Submission return URLs are same-site only and validated with
  `url_has_allowed_host_and_scheme()`.
- Tabler 1.4.0, HTMX 2.0.10, and Tom Select 2.6.2 remain pinned under
  `submissions/static/submissions/vendor/` with their licenses.
- Server-side verification font assets remain under
  `submissions/assets/fonts/` with their licenses. Header fallback must use the
  shared font-run measurement and drawing helpers; do not depend on host fonts,
  replace unsupported extracted characters, or add source-specific renderer
  branches.
- Title evidence may use normalized words to locate a candidate sequence, but
  final evidence rectangles must be built from raw PDF characters represented
  by the extracted title. Never fall back to whole-word geometry when that
  would include PDF-only symbols or punctuation.
- The shared Paper picker is limited to Paper Master selection workflows. Its
  read-only endpoint returns no results for an empty query, caps responses at
  20, and returns Master ID, Title, and Authors. Keep Paper selection validation
  in Django forms/services; picker values are presentation input, not workflow
  authority.
- Shared behavior belongs in the existing pagination, navigation, magnifier,
  focus, tabs, and alert components; do not create page-specific alternatives.
- Normal links and forms remain the fallback, CSRF remains enabled, and UI
  caches never feed publication or export decisions.

## Data And Review Reset Rules

When changing data that affects a review, reset only dependent review flags.

Examples:

- Changed PDF resets processing, title/author extraction, title match review, plagiarism scores, formatting review, and related file-derived exceptions, including plagiarism score exceptions.
- Changed source resets formatting review.
- Changed extracted authors resets author-number and duplicate-author review state.
- Changed Paper ID resets Paper ID verification and active-version grouping.
- Changed Paper Master notes must not reset any review/check status.
- Active-version rule changes must be previewed and applied without resetting review flags.
- Changed Paper Master publication decision recalculates publication scope and
  Final compatibility mirrors, but must not reset review/check evidence.
- New Paper Master creation must use
  `create_paper_master_with_publication_guard()`. Direct model creation is
  acceptable only in controlled tests and migrations. Matching orphan
  exclusion evidence must produce Decision Required.

Workflow ownership is also a reset-safety boundary. `FinalSubmissionForm` must
not expose processing messages/status, Title/Author Review status,
duplicate-author review, or Not Publishing fields. Paper Master publication
decisions must use `publication_decisions.py`; mapped Final exclusion fields are
compatibility mirrors only. Use the dedicated services and pages so required
locking, evidence checks, synchronization, and audit events cannot be bypassed.
`Decision Required` must preserve legacy mirror evidence. Final, draft, and
CrossCheck exports must call the shared publication-decision integrity check;
do not rely on UI state or Master scope filtering alone.

Manual Final Submission create and edit paths are intentionally separate. Create must use `create_final_submission_manual()` so Paper ID evaluation, file paths, initial review state, active/duplicate selection, and audit logging happen atomically. Edit must use `apply_final_submission_manual_edit()` with an existing record; do not pass `None` or create a placeholder original record.

Existing Paper Master and Final Submission edit controllers must create a
preview through `record_edit_preview.py`; they must not call the apply services
directly on the first POST. The preview service compares cleaned model values
and file bytes, while `apply_initial_paper_manual_edit()` and
`apply_final_submission_manual_edit()` remain authoritative for locking,
resets, active-version recomputation, and audit success. Revalidate database
evidence, current-file hashes, and staged upload hashes before apply. Do not
infer a change from an input event, filename, or datetime formatting loss.

Prefer preview-before-apply for imports, re-uploads, restore, and any setting change that can materially alter current publication candidates.

Final re-import is keyed by Final ID. Keep Official Paper ID resolution in
`preview_final_import()`:

- preserve a nonblank Official ID when Author-entered ID is unchanged;
- resolve when Author-entered ID changes or Official ID is blank;
- never use a Final Title change alone to remap an existing submission;
- never infer Not Publishing from a missing/invalid Official ID.

Paper Master and Final Submission imports are separate services. Do not
reintroduce a combined Mapping Table workbook or hidden mapping metadata path.

An orphan Final Paper ID may be resolved from Not Publishing List, but that
controller must call `verify_submission()` with a current `paper-id-review`
evidence token. Use `paper_id_review_snapshot()` to construct that token and
the shared Paper Master picker partial for selection. Never assign
`paper_id_filled` directly from this UI. Orphan Not Publishing actions must
pass an explicit reason; `mark_not_publishing()` validates the reason in the
service layer and has no semantic default.

## Dashboard Readiness Rules

Dashboard must consume `publication_readiness_rows()` through the application selector. Do not build a second list of blockers from `dashboard_counts()`; otherwise Dashboard can appear clear while final export is blocked.

`dashboard_counts()` is for conference totals and non-blocking tracking information. Next-action primary and breakdown counts must be derived from the grouped readiness rows, not from parallel queries. Counts labeled as papers must deduplicate by affected Paper ID; breakdown groups may overlap but must use the same row scope as their primary workflow card. Inactive, discarded, and Not Publishing versions must not inflate active issue counts. Keep verified/reviewed title differences separate from unverified title-mapping blockers. Title/Author `Review OK` is the completion decision for both extracted metadata and its title comparison; do not add a second publication blocker for a reviewed title difference.

When adding or renaming a publication readiness category, update the Dashboard workflow registry and its breakdown grouping. Acceptance coverage must prove categories are assigned to at most one workflow, all blocking Error Report categories are registered, valid Allowed exceptions stay out of Next actions, stale exceptions remain actions, and unknown categories fail safe into `Other publication blockers`.

Error Report category selection is presentation-only and must filter the
already annotated rows in `checks.py`; it must never reimplement readiness
conditions in the controller or template. Preserve repeated `category` query
parameters, validate them against the current workflow-area rows, apply the
selection before pagination, and keep multi-category matching as OR.

## File Handling Rules

Use app-managed file helpers instead of ad hoc path logic.

- `source_pdf_path()` is processing/extraction input: corrected PDF, then original PDF.
- `publication_pdf_info()` is publication-facing output: corrected PDF, then original PDF.
- `publication_source_info()` is publication-facing output: corrected source, then original source.
- Organized List source classification must not infer a source-file issue only
  because Formatting is Pending/Needs edit and `source_hash` is empty. Source
  review binding is required only after Formatting Review OK; the independent
  Format Not OK status remains the blocker before then.
- `publication_debug_pdf_info()` describes generated inspection copies. It is never the source for publication package export or CrossCheck export.
- Publication package export, CrossCheck export, duplicate checks, and both Organized List views use publication-facing helpers.
- Final Submissions list file links are row-scoped display links and intentionally show only Original/Corrected files for that row, not another active submission's publication files.
- Do not delete old uploads for traceability.
- Do not expose editable path text fields for user-managed files when upload/link UI is safer.
- System State backup must include referenced review artifacts, including title/author verification images, PDF thumbnails, and format previews.
- `data/formatting_comparison_previews/` contains expiring signed comparison
  payloads and independent PDF copies, not review artifacts. Exclude it from
  System State, protect it from ordinary overlapping Reports cleanup, and
  include it in Clear Database. No migration/archive version change is needed
  for this ephemeral feature. Its audit events are backed up normally.
  Automatic request caches and OS lock files are beneath its `requests/`
  subtree; they share that exclusion/protection/clear policy. A lock is held by
  an open descriptor, not by the mere existence of `request.lock`.
- System State restore must remap files into the current project `data/` tree and must not preserve old machine-specific absolute paths.

Process PDFs is not a read-only page-count operation. It recalculates active
versions, then processes only active, undiscarded Final versions whose Paper
Master decision is Publishing. For those candidates it calculates
page/hash/thumbnails from the Corrected/Original PDF source, resets page-limit
exceptions when page count changes, rebuilds author cache, and syncs the
publication PDF debug folder. Historical, discarded, Master Not Publishing,
Decision Required, and invalid-ID records must not create processing errors.
It must not scan incoming folders, create submissions, rewrite
original/corrected files, or update publication source selection through
`current_file_path`. Any future refactor that changes this behavior must update
Operator Guide, Architecture Notes, Troubleshooting, and acceptance tests
together.

Thumbnail rendering must use operation-unique directories. Batch persistence
compares `final_submission_state_evidence()` under row locks; stale generated
directories are removed, and replaced directories are removed only after
commit when no row references them. Never render directly over a shared
Final-ID directory.

Process PDFs also exposes formatting triage through
`record_formatting_issue_from_pdf_preview()`. Keep this action in the Formatting
service and persist only through the existing `format_status`, `format_notes`,
and `source_hash` fields. Notes are appended after `clean_note_text()`;
Review OK becomes Needs edit and its source binding is cleared. The action must
not reset Title/Author, Paper ID, plagiarism, page, hash, thumbnail, or file
state. It must reject records that are no longer current Paper Master
publication candidates and must write an audit event.

## Audit Logging Requirements

Any new workflow that changes records, files, review status, publication readiness, settings, exports, cleanup, or backup/restore must write an audit event through `submissions/services/audit.py`.

Report exports are read-only operations with audited output. They must not call
`rebuild_paper_authors()` or persist any derived cache. Build all selected
Editorial Workbook sheets from one stable `PublicationReadContext`, validate
the temporary workbook, recheck publication-critical database state, and
atomically promote it. Requested and failed export attempts must be auditable;
if audit persistence fails, no downloadable workbook may remain.

All string values written to XLSX reports or package CSVs must be safe when
opened in spreadsheet software. XLSX preserves formula-like input as literal
text. CSV protection applies only to string values that begin, after leading
whitespace, with `=`, `+`, `-`, or `@`; numeric report values remain numeric.

Register every production action in
`submissions/services/audit_actions.py` and use its canonical
`<domain>_<operation>[_<phase>]` code at the call site. Keep workflow phases
such as `preview`, `apply`, `cancel`, and `undo` in the action only when they
represent distinct operations. Keep outcomes such as `success`, `failed`, and
`blocked` in the event status. Aliases are only for reading historical logs;
new code must not introduce another spelling for an existing action.

Use the helper that matches the result:

- `audit_preview()` for preview-before-apply steps.
- `audit_requested()` for dangerous requests such as Clear Database.
- `audit_success()` after a successful state change or export.
- `audit_failure()` when an operation fails.
- `audit_blocked()` when the app intentionally blocks an export or workflow because readiness checks failed.

Audit events should include the relevant Paper ID, Final Submission ID, changed fields, before/after values, reset flags, file changes, file hashes, and result counts. Store paths as portable project/media-relative references; never log binary PDF/source/report content.

Clear Database must preserve `data/logs/audit.log` unless the user explicitly checks the audit-clear checkbox. System State backup must include the active audit log and archived logs.

The default Audit Log request must use the bounded tail reader. Full-file scans
are reserved for explicit search or structured category/action/status filters.
Legacy aliases may be normalized for display and filtering, but stored JSONL
events remain append-only and must never be rewritten. Django admin remains
read-only for publication-critical models; new writes belong in audited
services.

## Tests

Most regression coverage lives in `submissions/tests/test_acceptance.py`. Add scenario tests when changing:

- Active-version selection.
- Import preview/apply behavior.
- Preview-file byte changes between preview and apply, including Final import,
  Editor Upload, and Formatting title guards.
- Review reset flags.
- Publication readiness and export blocking.
- File priority or publication package output.
- File replacement between readiness and ZIP writing, and sanitized ZIP
  filename collisions.
- System State export/restore.
- Storage cleanup policy.
- Excel report formula safety, read-only behavior, atomic cleanup, audit
  lifecycle, and one-snapshot Editorial Workbook consistency.
- Storage inventory exact-file and referenced-directory protection, including
  the fresh reference check between cleanup preview and apply.
- Audit logging for state-changing workflows.
- Editor Upload, discard, and Not Publishing behavior.
- Automatic CrossCheck version binding: identical PDF bytes on a replacement
  Final must not receive an earlier Final's results. Cover scope changes,
  missing selected Corrected files, mixed valid/stale batches, late-item and
  audit failures, expired evidence, tampered inputs/results, and cross-job file
  URLs. Use mocked transport; never submit conference PDFs to test the service.
- Retry-download coverage must include partial/repeated retries, no replay of
  applied results, apply during retry, old-API/busy responses, uncertain-request
  lineage recovery, changed Final/PDF scope, corrupt later ZIPs, atomic DB/audit
  failures and v7/v8 restore. JSON retry requests must not contain PDF uploads.
- CrossCheck connection setup must cover blank addresses, authenticated folder
  requests with unsaved credentials, explicit credential persistence without
  changing Settings, environment precedence, URL isolation, stale evidence and
  late browser responses. Run `node --test submissions/tests/js/*.test.mjs` for
  connection setup, polling and the shared reader; never log test passwords.
- Archive compatibility must preserve current publication bytes, revision pairs,
  thumbnails and review state. Incomplete model payloads are rejected; corrupt
  file previews must report the defect and must not be applicable.
- Compare archived automatic creation/update fields with restored records;
  `bulk_create()` still calls field `pre_save()` and must not reset chronology
  or renew a CrossCheck upload preview's two-hour age limit.
- Multi-editor long-running Process PDFs/extraction races, including generated
  file output as well as database fields.
- Worklist UI or local frontend assets. The publication byte-level regression must keep ZIP entry names, PDF/source SHA256 values, manifest rows, and readiness categories unchanged across UI-only requests.
- Pagination performance coverage should assert expensive helper call counts,
  not wall-clock thresholds: normal pages must hydrate only the selected page,
  while `page_size=all` hydrates the complete filtered result.
- Natural sorting may load IDs and sort keys before pagination, but must not
  materialize full Paper Master or Final Submission rows until the page is
  selected.
- Settings performance coverage must assert that its main request does not call
  `build_storage_inventory()` or `check_grobid_api()`. Storage scale benchmarks
  should use generated fixtures outside the committed test suite; functional
  tests should assert call boundaries and cleanup behavior rather than
  machine-dependent wall-clock limits.

Title-upload safeguards must use `build_title_guard_context()` and the shared
`includes/title_guard_comparison.html` partial. Do not create separate three-column
Master/Final/PDF title layouts. Full titles remain in a single-column
`minmax(0, 1fr)` flow with explicit wrapping; word-level differences are primary and
character-level differences are optional detail. Preview open/cancel/replace actions
must operate on the server-owned preview token and write audit events without creating
or modifying a submission before confirmation.

Use factories in `submissions/tests/factories.py` rather than duplicating setup when possible.

## Version And Release Checklist

The app version is `APP_VERSION` in `conference_final_manager/settings.py`. The footer displays it.

Increment `APP_VERSION` for user-visible workflow, docs, UI, schema, or export changes.

Increment `STATE_ARCHIVE_VERSION` only when System State ZIP structure or restore compatibility changes.

Archive version 8 adds CrossCheck attempt/result models and per-execution
immutable ZIPs. Restore validates parent ownership/order, captured paper scope,
matching archive hashes and applied-result evidence. The metadata-only migration
and explicit v7 restore adapter capture legacy jobs as their original execution;
they must not move files, renew timestamps or reset reviews. A v8 archive missing
either model group is invalid, never treated as v7.
Archive version 7 adds automatic CrossCheck job/item models and managed evidence.
The explicit v5/v6 readers validate their original model sets and restore no
automatic job history. Secrets under `data/secrets/` must never enter System
State, audit, or model serialization. Clear Database removes those local files;
restore preserves receiving-machine secrets. Batch provenance restored from a
validated archive must be signed for the receiving installation before reuse.
Archive version 6 added `FormattingRevision` records and immutable files.
Version 5 remains accepted through explicit validation of its original model
set, counts, payload hash, and file hashes before restore; missing revision
rows mean empty history only for v5. Version 6 requires the revision model key,
valid parent/sequence identities, and matching snapshot file hashes/sizes.
Earlier versions remain unsupported. Never relax integrity checks to accept
an incomplete v6 archive.

Upgrade with migrations followed by `backfill_formatting_revisions`. The
command captures all existing corrected pairs under per-submission locks,
is idempotent, reports failures, and leaves review/active state untouched.
History files live in `MEDIA_ROOT/formatting_revisions/` with exact storage
references registered in inventory and protected under overlapping roots.

Exact-navigation and focused-worklist changes do not alter System State archive
contents, so they require an app version change but not an archive version
change.

Record-edit preview tokens are temporary operational state and are excluded
from System State ZIPs. Adding this preview boundary does not change archive
by itself; the Formatting revision model does require archive version 6.

Before release:

1. Run regression commands.
2. Confirm docs match current routes and feature names.
3. Update the canonical owner for each changed rule:
   `docs/publication_rules.md` for publication behavior,
   `docs/ui_conventions.md` for shared UI behavior, and this guide for
   implementation and release requirements.
4. Confirm `README.md` points to new or renamed docs and update `CHANGELOG.md`.
5. Export a System State ZIP and verify manifest version fields.
6. If publication export changed, test both final and draft package paths.
   Draft export may include ordinary readiness warnings, but structural ambiguity
   (`Multiple Active Final Submissions` or `Duplicate Publication Filename`) must
   fail closed rather than selecting or overwriting a file, and the response
   must render the detailed blocker panel.
7. Commit code, migrations, templates, docs, and sample data together when they describe one user-facing change.
