# PDF Comparison

**Current comparison method: 9.** This document owns the algorithm, tolerances,
cache and inspection contract. Operator steps are in the
[Operator Guide](operator_guide.md#pdf-comparison).
Measured results are in [Validation Record](deployment_validation.md).

Comparison is an advisory OCR review aid, not publication approval. It is
optimized for small editorial corrections, not arbitrary document rewrites.

## Inputs And Identity

| Side | Selection |
| --- | --- |
| Before | Same submission's Original or a hash-verified Formatting revision |
| After | Exact active Publishing submission's PDF from `publication_pdf_info()` |

CFM freeze-reads selected bytes and binds the submission, baseline, saved upload
revision and ordered SHA256 values. Build and later snapshot access revalidate
live scope, file identity/hashes and ownership. Missing files never select an
alternate. A comparison snapshot expires after two hours and is excluded from
System State.

Automatic comparison starts after a successful corrected-PDF save in a separate
operation. Its request binds that exact saved revision. Failure cannot undo the
save, approve review or apply findings to another upload. Card-local state and
the bounded return receipt prevent stale panels leaking across list filters.

Method/result/inspection compatibility must move together. Method-8 results
cannot be relabelled as method 9; rebuild them. Compute-code changes require
synchronized CFM/worker updates, independent of the app's display release number.

## Recognition And Alignment

1. Render each saved PDF at 300 DPI; RapidOCR recognizes visible content using
   the approved detector long-side limit of 2560. Preserve raw text, scores and
   word/line boxes, including image text, formulas and headers/footers.
2. Use each document's native geometry only for ownership/order guidance.
   Native strings never correct OCR or prove equality.
3. Reorder fragments only when native-region/row and visible OCR-row evidence
   agree, using that document's own coordinates. Preserve ambiguous, RTL,
   vertical and coarse regions rather than forcing a guessed order.
4. Anchor using unchanged OCR context, independent of page number or absolute
   coordinates. Bounded local alignment compares small edits; geometry breaks
   ties, not text equality.
5. Account for each occurrence/character once. Table/field ownership prevents
   repeated values cancelling across rows or global word bags.
6. Keep additions, deletions and modifications with side-specific locations.
   Missing anchors, large rewrites, ambiguous pairing and exhausted local
   budgets become explicit unlocated/manual-review ranges.

No dictionary repair, LLM, paper-specific rule or native-text-only fallback is
used. OCR/model/render failure is **Unavailable**, not zero findings.
A usable result can still have Limited coverage and unresolved ranges.

Runtime geometry matters: changing PyMuPDF can change blocks/order even with
identical rendered pixels. Frozen-OCR parity is not fresh end-to-end PDF parity.

## Tolerance Policy

The default broad policy deliberately downgrades some complete local differences
to inspectable Info. It is not glyph identity or semantic proof.

### Direct Visual Groups

Forms within a row may substitute directly, but rows do not compose transitively.
Unicode code points make non-ASCII forms explicit. The implementation/risk
inventory is `rapidocr_comparison/symbols.py` and the UI's Technical details.

| Rule | Direct forms |
| --- | --- |
| round | `0`, `O`, `o`, U+039F, U+03BF, U+041E, U+043E |
| vertical | `1`, `I`, `l`, U+007C, U+0406, U+04C0 |
| dash | `-`, U+2010, U+2011, U+2013, U+2014, U+2212 |
| single_quote | U+0027, U+2018, U+2019, U+02BC |
| double_quote | U+0022, U+201C, U+201D |
| plus | `+`, U+FF0B, U+5341 |
| times | `x`, U+00D7, U+2715 |
| middle_dot | U+00B7, U+22C5, U+2219 |
| fi / fl / ff / ffi / ffl | Each literal ASCII sequence and its matching U+FB01 / U+FB02 / U+FB00 / U+FB03 / U+FB04 ligature |
| rn_m / cl_d / vv_w | `rn/m`, `cl/d`, `vv/w` |
| A / a | `A`, U+0391, U+0410 / `a`, U+0430 |
| B | `B`, U+0392, U+0412 |
| C / c | `C`, U+0421 / `c`, U+0441 |
| E / e | `E`, U+0395, U+0415 / `e`, U+0435 |
| H / K / M | `H`, U+0397, U+041D / `K`, U+039A, U+041A / `M`, U+039C, U+041C |
| N | `N`, U+039D |
| P / p | `P`, U+03A1, U+0420 / `p`, U+03C1, U+0440 |
| T / X / Y | `T`, U+03A4, U+0422 / `X`, U+03A7, U+0425 / `Y`, U+03A5, U+04AE |
| broad-only weaker groups | `2/Z`, `5/S`, `8/B`, `6/G`, `f/t`, `u/v` |

Same-base, same-case letter diacritics (e.g. e/é/è) and existing whitespace
split/join rules also produce Info. There is no general Unicode compatibility
normalization, case folding or arbitrary-number equivalence.
A mixed non-tolerated edit must remain a Warning, not partially disappear with
a neighboring tolerated character. The literal profile retains literal comparison.

### Hyphen-Only Words

One gate in `hyphens.py` handles inline and line-wrap cases:

| Requirement | Rule |
| --- | --- |
| Join | Tight ASCII hyphen, U+2010 or U+2011; explicit OCR line-end/start join allowed |
| Word parts | Latin letters, at least two per part |
| Counterpart | Same ordered letters and case, joined or whitespace-split |
| Evidence | Reliable unchanged context, whole-word ownership and original OCR offsets |
| Exclusions | Digits/underscores, short variables, negative values, ranges, spaced subtraction, double/unsupported dashes and explicit adjacent math |
| Output | `word_hyphen` / `wrap_hyphen`, both under one Hyphens Info control |

Anchors cannot cut inside an eligible compound. A dedicated bounded alignment
transition consumes the complete pair; full-document math/boundary context stays
available outside the local gap. Adjacent real edits remain separate Warnings.
Parent validation requires same-letter evidence, raw OCR and both locations.
The wrap label is not an independent permissive fallback.

Examples: real-time/realtime and line-wrap hyphenation can be Info.
`-0.05/0.05`, `10-20/1020`, `x-y/xy` and changed letters are not this exception.
This is lexical, not semantic: re-sign/resign is Info. Bare named Latin variables
without distinguishing math syntax can resemble words and need human review.

### Accepted Blind Spots

Real O/0, f/t, rn/m, diacritic and other listed edits can be tolerated.
Whitespace changes such as "now here/nowhere" or "1 1/11" can be missed.
Identical OCR mistakes, unread text, tiny image lettering and formulas remain
risks. No deletion-to-empty rule permits a missing minus, decimal point, question
mark or whole word, but OCR itself can fail to recognize that content.

Figures, image preservation, tables, layout and full mathematical structure need
visual review. **No zero-finding result certifies a PDF.**

## Runtime And Work Bounds

RapidOCR 3.10.0 and ONNX Runtime 1.31.1 use checksum-pinned PP-OCRv6 Small or Medium
detector/recognizer selections. Requests never download models.
Deployment/settings belong in [Deployment](deployment.md) and
[Worker Settings](operations.md#worker-settings).

| Bound | Limit |
| --- | --- |
| Input | 64 MiB per PDF, 40 pages/document |
| Rendered page | 4000 px/side, 16 million pixels at 300 DPI |
| OCR | 2000 lines/page, 300,000 line characters/document |
| Alignment inventory | 30,000 tokens, 60,000 native geometry words/document |
| Context anchor | 6 tokens and 24 characters; identical-sequence control separately supported |
| Local gap / edits | 200 tokens; 16 edited tokens, 80 characters, 6 runs |
| Visual alignment | 1600 characters/side per local difference |
| OCR budget | Default 300 s; configurable 30-3600 s, not automatic quality reduction |
| Worker wrapper | OCR budget plus 30 s |
| CPU isolation | Linux 6 GiB address-space bound and finite aggregate CPU allowance |
| CUDA isolation | Finite container memory required; no CPU address-space limit substituted |

Concurrent lanes use independent OCR engines and restore original page order.
Rendering/native geometry stays on the caller thread. CPU defaults to two pages
in standalone workers, one embedded; CUDA four. GPU reduces logits on-device
but retains the original FP32 CTC decoder/exception handling. No FP16, quantized
model or lower-DPI fallback is permitted to hide failures.

## Cache And Routing

Completed document extraction and ordered-pair results bind exact PDF bytes
and validated code/model/runtime fingerprints. Unchanged Original OCR can be
reused after a new correction; changed bytes are recognized afresh.
No approximate/image-similarity cache is used.

Signed caches have seven-day, 512-entry, 512 MiB per-namespace limits. Corrupt,
expired/incompatible entries require fresh work; failed pairs never count as
complete. Model/runtime validation still precedes acceptance. Cache write failure
cannot suppress a successful result or hide failure. Caches/snapshots are
temporary, excluded from System State and distinct from retained review images.

With remote configured, new local work requires explicit, short-lived consent
bound to exact inputs/configuration. Complete validated cache can display
without that confirmation. Accepted/uncertain jobs remain pinned to their
worker; no duplicate "fallback" execution. Failure never starts embedded OCR
behind a service-mode user's back.

## Document-First Inspection

| Finding | Rendering |
| --- | --- |
| Text change | Red outline/arrow, actual locations |
| Unlocated | Amber range, no claim of equality or definite deletion |
| Tolerated Info | Quiet neutral marks, raw/rule evidence retained |

Counts are findings, not papers or accuracy. Display caps preserve full totals.
Coverage is separate: complete means supported execution completed; limited
means usable evidence with limits; missing metadata is not complete.

Paired documents navigate with the evidence page map. No map entry clears the
opposite side; it never substitutes page one. Page references aid navigation,
not text matching. Manual page selection remains available.

The upper Magnification/Evidence dock preserves stable size. Full crops fit
without internal scrolling; Evidence can scroll. Hover follows pointer;
click selects persistent Evidence. Entering the dock freezes crops.
Connection, focus and Auto match share one current pair. The other crop follows
the selected endpoints' relative offset, including outside box boundaries.
Without a pair, use relative positioning. Candidate pairs remain unverified;
hiding/offscreen lines does not erase the logical pair.

Crop scale is 2x displayed page scale, at least 1.5 CSS pixels/PDF point, preserving
aspect at narrow widths. Canvas density caps at 3x / 3072 px wide.
After a 160 ms pointer pause, detail rasterization reads **the same signed snapshot**
through `comparison_pdf_bytes()`: max 3072 px/side, 4 million pixels, 8 pixels/point.
It does not alter OCR/render hashes/results. Late/superseded requests cannot
paint stale crops. A 10 s timeout leaves Preview only; rejected snapshots require
rebuild. No persistent detail images or alternate-file fallback.

Technical details holds counts/policy/identities. Coverage and preview errors
stay visible. Keyboard/touch, Escape and close controls use the same state;
hover never changes pages or scroll positions.

## Required Verification

See [Acceptance Runbook](editorial_acceptance_runbook.md#pdf-comparison).
Use actual PDF/OCR plus injected/frozen boundary tests, including repeated
values, ordering, shifted/reflowed text, additions/deletions/modifications,
references, math, accepted blind spots, empty OCR and limits.
Assert unchanged file/revision selection, DB/reviews/readiness and exact
Publication/CrossCheck bytes. Frozen geometry parity alone is insufficient for
a runtime/model upgrade.
