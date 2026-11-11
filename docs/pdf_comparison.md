# PDF Comparison

## Implemented Design

The production integration uses temporary comparison method **8**, introduced in
app **1.29.0** with inspectable same-letter diacritic tolerance. Apps **1.29.1**
through **1.29.3** improve inspection layout and display-only detail rendering without
changing that method or signed snapshots.
This is the canonical engine/UI contract, not a test report or
release acceptance record. Method-7 and older signed results must be rebuilt;
they must not be relabeled or reused as method-8 results. Historical method
descriptions remain in the changelog, not as requirements for this engine.

Comparison is advisory and read-only. After comes from the publication helper
for the exact active Publishing submission. Before is its Original or a
hash-verified Formatting revision. Builds and snapshot reads revalidate scope,
file identity and SHA256. Missing files never authorize a fallback. The build
is audited, but findings cannot approve/reset reviews, waive exceptions, change
publication selection, or feed readiness. Automatic post-upload requests remain
bound to the exact saved revision and run separately from the save transaction.
Temporary comparison artifacts expire after two hours and are excluded from
System State. **State archive v8 is unchanged.**

## Rendered OCR Workflow

1. Render each saved PDF independently at 300 DPI and recognize its visible
   page content with RapidOCR/ONNX in the selected compute runtime, using the approved detector long-side
   limit of 2560. Preserve raw OCR strings, boxes and scores. Do not exclude
   image text, formulas, headers/footers or low-confidence recognition.
2. Use each document's own native geometry only to guide region ownership and
   reading order. Native text is not an OCR replacement, correction source or
   equality shortcut. Identical, stale, missing or contradictory native text
   must not hide a visible edit.
3. Reorder fragments only with compatible native-region, native-row and visible
   OCR-row evidence, using that document's actual horizontal positions. Do not
   force RTL, vertical, coarse or ambiguous geometry into this ordering. Keep
   punctuation/token order inside a word box.
4. Establish correspondence from unchanged OCR context, not equal page numbers
   or absolute coordinates. Use bounded local token alignment for small edits;
   relative position only resolves alignment ties. Repeated/table values retain
   field ownership and cannot cancel through global word bags or cross-row matches.
5. Retain added, removed and changed local passages with actual side-specific
   locations. Large rewrites, missing anchors, ambiguous pairing, exhausted
   local work bounds and empty OCR remain explicit handoffs/limitations, never
   a clean comparison.
6. Apply only the explicit visual policy below to complete local differences.
   Keep original text, occurrence counts, locations, rule IDs and risks
   inspectable. No dictionary, LLM, semantic repair, global confusables skeleton,
   general Unicode compatibility normalization or paper-specific tuning is used.

PDF rendering and OCR/comparison execute behind the isolated worker boundary;
the request service owns publication selection, snapshots, audit and revision
binding. No OCR/model failure may silently invoke the retired method-6 engine
or native-text-only comparison. Worker timeout, missing dependencies/models,
malformed output or render failure that prevents comparison means
**Comparison unavailable**, not success with zero findings. Runtime and input
bounds must be enforced and validated at the parent boundary. A usable bounded
result with unresolved ranges instead reports its limitations.

Local inference requires RapidOCR 3.10.0, ONNX Runtime 1.31.1 and the approved
PP-OCRv6 small detection/recognition models. Provision with
`prepare_rapidocr_models --source ... --destination ...` into the default
`BASE_DIR/runtime/rapidocr-models` directory. Existing Compose's `.:/app` mount
exposes this default in Docker without another mount or environment setting.
An optional absolute `CFM_RAPIDOCR_MODEL_DIR` overrides the lookup location;
external Docker paths need their own mount and container environment setting.
See [runtime setup](developer_guide.md#pdf-comparison-runtime).
Provision them before use; a comparison must not depend on a research virtual
environment, files under `output/`, or runtime model downloads. Service mode
uses only the explicitly configured trusted compute endpoint.
The experiment's environment is not a deployment dependency.

Native geometry is also a runtime input: different PyMuPDF versions can group
the same visible words into different blocks/lines or return different boxes,
even with byte-identical rendered images and identical OCR. That can change
alignment boundaries and finding counts. Cached-OCR regression on frozen
geometry proves comparator parity only, not end-to-end PDF parity. Record the
render/extraction runtime and run fresh-PDF acceptance when changing it; do not
upgrade the publication runtime or tune per-paper thresholds to force old counts.

### Local CPU Evidence Reuse

Manual and automatic builds share a best-effort CPU cache under
`data/pdf_comparison_cache/`. Ordered PDF SHA256 values identify a comparison;
each document also has its own byte-bound extraction entry. Replacing a corrected
PDF can reuse the unchanged Original's completed OCR and native geometry, but
the changed PDF is recognized afresh. Byte-identical sides can share extraction
within one worker; they still undergo normal coverage/accounting checks, including
blank-page limitations. No page-similarity or approximate cache lookup is used.

The service signs cache entries and the isolated worker independently checks
their hashes and actual runtime fingerprint: engine/worker code, approved model
hashes, package versions, CPU/backend/build identity, thread configuration and
relevant environment. Models/dependencies are checked before any hit is accepted.
Cache data never supplies publication paths or current revision identity. Every
build still checks live scope, selected files and upload binding before and after
comparison, and issues a fresh two-hour inspection snapshot.

The worker reads/writes bounded JSON sidecars only in a parent-created private
temporary job directory; persistent HMAC-signed storage stays in the parent.
A complete per-document extraction can survive a later failure/timeout, but
no failed pair becomes a completed comparison. Concurrent identical-pair builds
share an OS lock and return the existing busy/retry workflow, not duplicate OCR.
Corrupt, expired, incompatible or unavailable cache entries cause fresh work.
Cache write failures cannot replace a successful result or suppress a failure.

Entries expire after seven days; pruning bounds retained storage to 512 MiB and
512 entries. Oversized intermediate evidence is simply not cached. This temporary
cache is excluded from System State, protected from ordinary cleanup and emptied
by Clear Database. Review images and immutable revisions are not this cache.

Rendering now hands RapidOCR the identical 300-DPI BGR pixel array directly,
without the former lossless PNG encode/decode roundtrip. Models, recognition
batching, reading order, comparison rules and result schema remain unchanged.
That pixel-transfer optimization is retained by method 8; State archive remains v8. First-time extraction
on CPU can exceed the existing wall limit; reuse is not a promise that every
cold comparison finishes. Service deployment below changes the execution
boundary, not the comparison algorithm.

### Shared Comparison Service

`CFM_COMPARISON_TRANSPORT=embedded` preserves native local execution. `service`
uses the configured local worker, optionally preferring a remote worker. URLs
and dedicated API tokens are environment configuration, never AppSetting or
System State. A service configuration must not silently fall back into the web
process when both workers are unavailable.

CFM alone selects/freeze-reads the two PDFs and retains conference/revision
binding. The worker receives only these bytes, their ordered SHA256 values and
the protocol/engine contract. It has no conference DB, media mount, publication
helper, source-file upload or export authority. It runs the same bounded
comparison implementation in an isolated child process.

Use a separate shared Compose project per host, not a worker per conference
project. Each conference has its own bearer token/namespace and signed cache.
One heavy job runs at a time with a bounded queue. Jobs are idempotent within a
namespace and persist across HTTP disconnects. An interrupted process is a
failed job, not completed evidence. Worker jobs/cache are disposable service
storage; a conference Clear Database or restore does not delete another
conference's worker data. Retention is enforced independently by the worker.

Before submitting a new pair, CFM checks protocol/code compatibility and
readiness. With a remote configured, an unreachable, not-ready or incompatible
remote pauses the comparison; no new CPU work starts automatically. Manual and
post-upload builds use the same rule. Local-only service mode remains automatic.
Before new work, use authenticated read-only job lookups to find a complete
result on either configured worker. Validate the current contract, ordered PDF
hashes, result schema/identity and every page render hash. A verified complete
cache needs no local-compute confirmation, even when the preferred remote is
unavailable. A signed accepted route is never moved or resubmitted when another
worker supplies completed evidence. Authentication/redirect failures remain
explicit, not a way to bypass a gateway. Partial/running/failed/wrong-version
results do not count as a complete cache. Embedded mode similarly probes only
its signed full-pair cache, validating models/runtime before reuse; its cache-only
worker path must return a miss without OCR or fresh comparison.

An explicit local choice for fresh computation carries a five-minute signed confirmation bound to the
submission, baseline, ordered PDF hashes, target revision/upload time, upload
receipt (if any), engine contract and deployment configuration. GET/health
requests cannot grant consent. Revalidate before dispatch; never substitute
later files or a new configuration. Busy remains on the selected worker; it is not a signal to
duplicate compute. Authentication/configuration errors are explicit. Once
submission intent is retained, retry checks the same worker/job, including when
the POST reply was lost. An unreachable accepted worker does not silently
launch a second job. A signed routing lease lasts 130 minutes after its last
use and renews only on the same endpoint. The extra ten minutes cover an attempt
started near the job's two-hour expiry and bounded network delays; expiry must
not launch a local duplicate of still-running remote work. Changing endpoint or
worker Bearer credentials requires restoring the configuration or waiting for
lease expiry. Optional gateway header rotation does not change this identity.
Explicit Build/Retry can requeue a failed terminal job within the worker's
bounded retry allowance. Polling never retries failed inference; completed or
running jobs cannot be overwritten by Retry.

Existing local routes do not silently authorize submission, failed-job retries
or long waits when a remote is configured: these require explicit local consent.
A compatible completed result can still be retrieved without new computation.
Do not delete a route or move accepted work to a different worker. A new CPU
confirmation permits local use for this comparison, not a persistent preference.
Authentication, redirect and malformed-evidence failures do not become CPU
fallback opportunities. Embedded mode with a remote URL requires correcting the
mode or explicit local confirmation on a full-pair cache miss; it never quietly ignores remote intent.

Formatting List/Single/Focus share one compact service-status strip outside the
HTMX worklist. It observes authenticated health only: refresh ten seconds after
the preceding request completes, pause/abort when hidden or leaving, resume on
return and never overlap requests. Status calls use two-second endpoint timeouts,
five-second observation caching and one in-flight probe per web process. Last-seen
hardware is explicitly labelled when current health cannot report it. Service
mode reports observed backends; embedded mode labels its configured backend,
not a verified standalone service. Service status is not job status or permission
to dispatch; actual selection always
checks independently. These observational reads do not produce audit spam.
Successful signed comparison snapshots retain a small sanitized execution summary
and show it in upload status and PDF Comparison. Older snapshots say Worker not
recorded, never infer execution from today's health. Audit retains execution and
explicit local confirmations. No worker protocol, method, job key or archive
schema change is needed for this orchestration/presentation metadata.

The API uses short job/status requests. The existing CFM build request polls
for at most 240 seconds; a still-running/queued job returns the existing Retry
workflow. Retrying retrieves that job, not another upload. The per-job OCR
wall budget remains 240 seconds, separately from waiting in the queue. This
does not promise every cold PDF pair will finish. No new browser background
timer or conference workflow writes are introduced.

Before accepting completion, CFM checks protocol/engine contract, exact ordered
input hashes, result schema/identity and every inspection page's rendered pixel
hash. A cross-runtime rendering mismatch is an explicit error, not misplaced
overlays. It then rechecks live publication scope, files and upload revision.
The selected backend/runtime and job identity are included in comparison audit
diagnostics. GPU and CPU outputs/cache are not presumed interchangeable; GPU
results need acceptance on the actual provider/hardware. Cache hits inside a
fresh execution still validate installed models and runtime. Previously
completed jobs are immutable bounded-lived evidence, like completed CFM
inspection snapshots, not a new OCR run. Retrieval still requires a ready,
compatible worker runtime; changed or unavailable models/providers do not
silently serve previously computed evidence.

Bearer tokens require a trusted isolated LAN or TLS; the worker is not a
hardened public internet service. A protected HTTPS gateway can front its private
origin using optional per-conference `CFM_COMPARISON_REMOTE_HEADERS` JSON.
Headers supplement worker authentication on every remote API request and
startup check, never local requests. Gateway 401/403 and redirects fail
explicitly, without silent fallback or secret values in diagnostics. CFM does
not follow worker redirects or environment HTTP proxies. Never expose an
unprotected origin publicly or mount conference data into it. See the
[gateway setup](docker_guide.md#remote-gateway-authentication).
The shared HTTP client supplies the descriptive `ConferenceFinalManager/1`
User-Agent for health/upload/poll/retry calls and startup checks. Explicit
remote UA overrides are retained. This is not an engine/cache/route identity
change. Preflight exposes safe HTTP status, Cloudflare code and Ray ID (1010
means browser signature blocking), not arbitrary response prose or credentials.
Authentication/redirect failures remain explicit and never authorize fallback;
unavailable-before-acceptance and already-pinned job behavior are unchanged.

### Work Bounds

Input PDF bytes remain capped at 64 MiB per side. The OCR runtime limits each
document to 40 pages, 300,000 recognized line characters, 30,000 comparison
tokens and 60,000 native geometry words. A 300-DPI page must fit within 4,000
pixels per side and 16 million pixels, with at most 2,000 OCR lines. Oversized
pages are rejected rather than silently downsampled below the declared rendering
resolution. The detector's 2560-pixel long-side setting is a separate model
processing bound, not the saved page-image resolution.

`CFM_RAPIDOCR_THREADS` controls ONNX intra-operation CPU threads, not recognition
quality. The default is four (or fewer when fewer CPUs are visible); an explicit
value must be an integer from 1 through 8 and cannot exceed the runtime-visible
CPU count. Invalid settings fail rather than silently selecting another mode.
Inter-operation parallelism remains one. The parent wall limit is still 240
seconds. CPU time accumulates across threads, so its separate finite allowance
is 8 * 240 = 1920 CPU seconds, with a 20-second hard-limit margin. Linux retains
the 6 GiB address-space bound for CPU execution. CUDA requires a finite container
memory limit instead because CUDA reserves large virtual address ranges; the
worker validates that prerequisite. More threads do not guarantee faster completion;
benchmark on the actual deployment, without reducing DPI, changing models or
treating timeouts as successful comparisons.

Unchanged anchors require six tokens and 24 characters, with the explicit
identical-sequence control supported separately. Local gaps are bounded to 200
tokens; the small-edit budget is 16 tokens, 80 characters and six edit runs.
Visual-rule alignment is bounded to 1,600 characters per side of a local
difference. Exceeded local budgets retain a manual handoff; document/runtime
limits can make comparison unavailable. These are fixed common limits, not
per-paper tuning or promises that every edit within them is detected.

## Visual Tolerance Policy

The approved broad policy includes all 38 direct groups, including the six
weaker substitutions. This is deliberate tolerance, **not exact glyph identity
or proof that a PDF did not change**. All forms within one row may directly
substitute for one another; rows do not compose transitively. For example,
`8/B` and `B/U+0392` do not imply `8/U+0392`.

The following table spells non-ASCII forms as Unicode code points to make the
policy unambiguous. Multi-character ASCII forms are literal sequences.

| Rule ID | Direct forms |
| --- | --- |
| round | `0`, `O`, `o`, U+039F, U+03BF, U+041E, U+043E |
| vertical | `1`, `I`, `l`, U+007C, U+0406, U+04C0 |
| dash | `-`, U+2010, U+2011, U+2013, U+2014, U+2212 |
| single_quote | U+0027, U+2018, U+2019, U+02BC |
| double_quote | U+0022, U+201C, U+201D |
| plus | `+`, U+FF0B, U+5341 |
| times | `x`, U+00D7, U+2715 |
| middle_dot | U+00B7, U+22C5, U+2219 |
| fi | `fi`, U+FB01 |
| fl | `fl`, U+FB02 |
| ff | `ff`, U+FB00 |
| ffi | `ffi`, U+FB03 |
| ffl | `ffl`, U+FB04 |
| rn_m | `rn`, `m` |
| cl_d | `cl`, `d` |
| vv_w | `vv`, `w` |
| A | `A`, U+0391, U+0410 |
| a | `a`, U+0430 |
| B | `B`, U+0392, U+0412 |
| C | `C`, U+0421 |
| c | `c`, U+0441 |
| E | `E`, U+0395, U+0415 |
| e | `e`, U+0435 |
| H | `H`, U+0397, U+041D |
| K | `K`, U+039A, U+041A |
| M | `M`, U+039C, U+041C |
| N | `N`, U+039D |
| P | `P`, U+03A1, U+0420 |
| p | `p`, U+03C1, U+0440 |
| T | `T`, U+03A4, U+0422 |
| X | `X`, U+03A7, U+0425 |
| Y | `Y`, U+03A5, U+04AE |
| two_Z (broad) | `2`, `Z` |
| five_S (broad) | `5`, `S` |
| eight_B (broad) | `8`, `B` |
| six_G (broad) | `6`, `G` |
| f_t (broad) | `f`, `t` |
| u_v (broad) | `u`, `v` |

Only a whole local difference explained by equality, existing whitespace
tolerance and direct table substitutions can lose its red change mark. A mixed
difference with an ordinary residual edit retains the original finding; do not
fragment or partially excuse it because one character resembles another.
Tolerated occurrences are neutral information, not renamed uncertainty warnings.

There is no empty-string rule: deletion of a minus sign, decimal point, question
mark or word is not a visual exception. There is no general case folding,
subscript/superscript equivalence, arbitrary number substitution or spelling
correction. Explicit multi-character rules such as `rn/m` can nevertheless
tolerate genuine character insertions/deletions. Existing whitespace split/join
tolerance remains inspectable; possible wrap-hyphen information is separate.

### Accepted Blind Spots

Real `O/0`, `f/t`, `rn/m`, `S/5` and other listed changes may be tolerated.
For example, a real `safety/satety` edit cannot be distinguished from the same
OCR misreading by this rule. Whitespace-only `now here/nowhere` and `1 1/11`
changes can also be missed. Identical OCR mistakes on both sides, omitted text,
small image lettering and formula recognition remain risks. Matching OCR is not
a font-map proof, layout check, figure-preservation check or mathematical proof.
No zero-finding result certifies a publication PDF.

## Result And UI Contract

Keep source filenames, exact snapshot links, actual page lists and boxes,
navigation-only reference pages, raw text, context and reasons bound to their
original documents. Totals count records, not affected papers or accuracy.
Display caps never change full totals or conceal omitted/unknown ranges.
Expose these distinct groups:

- **Text differences:** added/removed/changed passages, red outline and arrow.
- **Unlocated ranges:** amber highlight/outline, no arrow; manual confirmation
  is required and the range is not a confirmed deletion or equality.
- **Information:** quiet neutral outlines for tolerated visual differences,
  whitespace joins/splits and possible wrap hyphens. Preserve rule and risk
  details; do not count these as verified unchanged content.

The documents are the primary workspace, not a permanent result-list sidebar.
Engine evidence counts, Visual exception policy and Compared file identities are
retained inside one closed Technical details disclosure. Coverage warnings and
missing-preview errors stay outside it and remain visible.
Show annotated page images immediately. The inspection area sits above the paired
documents and stays at the viewport top when scrolling. Magnification has no
internal scrollbar: compact controls and complete, aspect-correct crops remain
visible. Crop height adapts to short windows without stretching source pixels.
Evidence alone scrolls within the same reserved body height, so changing views or
moving the pointer does not push documents around. Fit-page sizing measures the
remaining viewport below each document's controls, not a fixed height deduction.
The inspection area has
Magnification and Evidence views. Auto match uses only unambiguous region pairs
on displayed pages; the opposite crop follows pointer offsets beyond box edges.
Ambiguous/unlocated regions use relative positioning; Relative position mode
explicitly bypasses anchors. Preserve each image's aspect ratio and edge bounds.
The crop window uses 2x the displayed page scale, with a minimum of 1.5 CSS pixels
per PDF point. Thus shrinking Fit page does not shrink magnified text to an
unreadable size. This is a display scale, not a matching rule. Crop sizes exclude
canvas padding; source pixels retain their aspect ratio, including when the
canvas backing-size cap lowers effective display density. Auto match and Relative
position share the same scale policy and preserve their existing center/offsets.
Canvas backing pixels follow display density (up to 3x and 3072 pixels wide).
After a 160 ms pointer pause, a bounded local detail endpoint rasterizes only the
requested region of the same signed snapshot PDF. It calls the existing snapshot
reader, including expiry, submission ownership, live-source/upload binding and
snapshot SHA256 checks. It never reads a different publication file or changes
the shared evidence renderer, OCR, render hashes, engine contract or cache.
Detail dimensions are capped at 3072 per side / four million requested pixels,
and density at 8 PDF pixels per point. Original raster images inside PDFs still
have their original resolution; higher-density rendering cannot invent detail.
Each side debounces requests, aborts superseded work, times out after ten seconds,
and keeps at most one decoded crop. Close, page changes and Evidence discard
obsolete work. Late replies cannot draw into another view. High detail is labeled;
network failures show Preview only, while rejected snapshots hide the crop and
require rebuilding. No alternate PDF is substituted. No detail pixels are persisted.
Clicking a mark selects Evidence independently of pointer magnification. Selected
raw OCR is not replaced by later hovering. Moving into the inspection area freezes
the last crops; departure and scrolling do not dismiss it.
Keyboard activation, touch, Escape and an explicit close control are supported.
Escape/Control dismiss an open inspector even when hover has left keyboard
focus on the surrounding page; inactive inspection does not consume keys.
Inspection state is page-local and must not persist host state.

Magnification never changes pages or scroll positions. It labels both current
page numbers and explicitly does not imply text correspondence. Keyboard
finding inspection uses the focused mark's center with the chosen positioning mode;
touch can inspect a mark or any page position. Missing/loading page pixels show
an explicit status, not a blank crop or a substituted matching page. Draw only
the active finding's connection: dashed amber for unlocated candidates and none for a
missing counterpart. An absent linked page-map entry clears the opposite page,
marks and crop with an explicit empty state, never page one or a retained page.
Manual page selection remains available. Connections are finding-level, never invented
character-by-character alignment. Explicit previous/next-result commands may
navigate to recorded pages. Page synchronization uses alignment evidence only
for navigation, not as a finding location or equality proof. Fit page is the
initial view; narrow screens retain both documents and independent controls.
Loading/errors clear stale marks/crops rather than show another page's evidence.

Coverage status/reasons and evidence counts remain distinct from findings.
`complete` means supported execution completed, not publication safety;
`limited` means a result with limitations; missing metadata is not complete.
Keep a brief coverage notice visible, with long reasons and evidence counts in
disclosures. Failed comparison and failed page-image display are separate
states. Neither lowered warning counts nor tolerated differences waive manual
review or publication blockers.

## Experiment Provenance

The approved source is the local research bundle
`output/pdf/rapid-visual-exceptions-20261110/` (`README.md`, `RESULTS.md`,
`SYMBOLS.md`) and the interaction reference is
`output/pdf/document-first-prototype-20261110/`. Keep those research artifacts
unchanged; production must import its own maintained modules.

The experiment reports 102 pairs and 235 OCR images. Its literal profile
matches its prior baseline; broad reduced candidate marks across three real
papers from 122 to 101. Those papers lack complete independently adjudicated
truth, so that reduction is neither accuracy nor proof of eliminated false
positives. The reported 13 earlier and 9 new non-tolerated edit checks, plus
explicit real-change tolerance blind spots, are research evidence only.
Prototype browser checks are UI evidence only. Neither is the production gate.

## Acceptance Handoff

Measured CPU/GPU Docker outcomes belong in
[Deployment Validation](deployment_validation.md). Keep those versioned results
separate from the comparison contract and the acceptance procedure below.

Run the [developer gate](developer_guide.md#regression-commands), comparison
Node tests, and [browser/engine scenarios](editorial_acceptance_runbook.md#formatting-pdf-comparison-acceptance).
Record commands, fixture hashes, actual locations, unresolved limitations and
backend/UI acceptance separately. This document does not assert they passed.

Required evidence includes actual rendered-PDF OCR (not just injected strings),
stale/absent/contradictory native layers, same-row ordering versus real word/value
swaps, repeated/table ownership, shifted/reflowed/repaginated controls, small
insertions/deletions/replacements, broken references, minus/decimal deletion,
mixed tolerated/non-tolerated changes, direct-only versus transitive rules,
large rewrites, empty OCR, worker/model errors and work limits. Include the
deliberate tolerance blind spots as expected limitations, not detection wins.
Retain one-to-one token/character accounting before display caps.

Browser QA must use actual payloads and nonblank image/crop pixels on desktop
and narrow screens. Verify hover does not navigate or scroll, keyboard/touch
inspection, overlay crossing/dismissal, synchronized relative-position crops, one-sided
findings, active-only links, explicit navigation and stale-load rejection.
Compare database/review/readiness state and publication/CrossCheck bytes before
and after. Verify saved-upload revision binding, audited builds, old-method
rejection, expiry, cleanup and archive exclusion without changing archive v8.
