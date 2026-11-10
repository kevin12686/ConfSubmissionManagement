# PDF Comparison

## Implemented Design

The production integration uses temporary comparison method **7**, in app
**1.22.1** (method 7 was introduced in 1.22.0). This is the canonical engine/UI contract, not a test report or
release acceptance record. Method-6 and older signed results must be rebuilt;
they must not be relabeled or reused as method-7 results. Historical method
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
   page content with local RapidOCR/ONNX, using the approved detector long-side
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
environment, files under `output/`, network inference or runtime model downloads.
The experiment's environment is not a deployment dependency.

Native geometry is also a runtime input: different PyMuPDF versions can group
the same visible words into different blocks/lines or return different boxes,
even with byte-identical rendered images and identical OCR. That can change
alignment boundaries and finding counts. Cached-OCR regression on frozen
geometry proves comparator parity only, not end-to-end PDF parity. Record the
render/extraction runtime and run fresh-PDF acceptance when changing it; do not
upgrade the publication runtime or tune per-paper thresholds to force old counts.

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
the 6 GiB address-space bound. More threads do not guarantee faster completion;
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
Show annotated page images immediately. Hover/focus/tap inspection shows the
existing finding, raw text and two crops from the actual page pixels, without
changing pages or scrolling either document. Moving into the inspector keeps
it open; leaving mark and inspector uses a short crossing grace period.
Keyboard activation, touch, Escape and an explicit close control are supported.
Escape/Control dismiss an open inspector even when hover has left keyboard
focus on the surrounding page; inactive inspection does not consume keys.
Hover is transient and must not persist host state.

For large unlocated ranges, magnify the hovered side at the actual pointer.
The opposite side starts at its own recorded range, explicitly not a character
correspondence, and can be inspected independently. Draw only the active
finding's connection: dashed amber for unlocated candidates and none for a
missing counterpart. Connections are finding-level, never invented
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
inspection, overlay crossing/dismissal, pointer-local unlocated crops, one-sided
findings, active-only links, explicit navigation and stale-load rejection.
Compare database/review/readiness state and publication/CrossCheck bytes before
and after. Verify saved-upload revision binding, audited builds, old-method
rejection, expiry, cleanup and archive exclusion without changing archive v8.
