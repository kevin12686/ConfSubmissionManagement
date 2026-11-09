# PDF Comparison

## Implemented Design

The current temporary comparison method is **6**, in app **1.21.1**. This document
is the canonical evidence and engine/UI contract, with a stable filename across
method versions. It does not
serve as a test report or release acceptance record. The comparison UI consumes
the fields below without changing publication selection, upload revision binding,
review state, or System State archive v8. The method version applies to temporary
comparison results only. Method-5 and older signed results require rebuilding;
they must not be relabeled or reused as method-6 results.

## Result Contract

The result retains existing fields, finding IDs, source locations, actual page
lists, reference-page fields, total/display counts, and page maps, and includes:

- `coverage_status`: `complete` or `limited`, describing execution of the
  engine's supported checks, never publication safety or full PDF equivalence.
- `coverage_reasons`: a list of strings explaining limitations. Show these even
  when content counts are zero or PDF bytes are identical. Keep the heading and
  brief coverage warning visible, with reasons in a closed-by-default Coverage
  details disclosure. Missing coverage metadata is not an implicit `complete`
  result.
- `evidence_summary`: engine-supplied counts. The UI displays nonnegative
  integer counts under their supplied names, including grouped counts, without
  deriving a confidence score or a percentage of document safety.
- `issues`: per-finding evidence explanations, rendered as plain text for every
  category, not just extraction errors.
- `category="structure"`: an advisory content/structure warning, with its own
  filter and amber region outlines. It is not an unresolved-reference pattern.

Keep content/structure Warning, uncertain text/formula Warning, and
normalization Info separate. The existing UI category for normalization
information is `formatting`; `info_count` is not a count of verified content.
An engine classification called `normalizationInfo` must retain or explicitly
map to that category at the engine boundary, not be guessed from display text.
Finding totals count findings, not affected pages or papers.

Limited coverage means a result exists but has inspection limitations. A parser
or worker failure that prevents a result is **Comparison unavailable**, not a
successful result with zero findings. Rendering failure is a separate page-image
error. Truncation and pairing-budget limitations remain visible in the coverage
area rather than stacking duplicate warning notices.

## Worker Boundary

Evidence inspection and token alignment run together inside
`pdf_comparison_worker.py`, not in the Django request process. The worker receives
the two exact PDF byte snapshots, returns bounded occurrence and alignment
evidence, and has no Django initialization, database access, publication metadata
access or filesystem writes. Source selection, revision binding, audit logging
and snapshot retention remain the parent service's responsibility.

The worker has a 90-second CPU soft limit (95-second hard limit where OS resource
limits are supported), plus a parent-enforced 95-second wall-clock timeout.
Windows uses the wall timeout and bounded inputs because the CPU-limit API is
unavailable. The parent validates returned evidence and alignment ranges before
using them. A timeout, worker failure or invalid response means Comparison
unavailable, not a complete or zero-findings result. A bounded alignment that
returns successfully with unresolved remainder instead reports limited coverage.

Work limits also bound native items to 500,000 per document, candidate outlines
to 100,000, TTC collection faces to 64 and table-rule ownership work to 2,000,000
steps per page inspection. Exhausted work limits produce a bounded
failure or an explicit limited result, never an unbounded retry or an inference
that uninspected content is unchanged.

## Evidence Rules

- Bind evidence to each used occurrence: code, glyph, font/resource identity,
  operation and position. A defective occurrence must not automatically turn
  every occurrence of the same font on the page into a warning. If occurrence
  attribution is unsupported or ambiguous, retain that uncertainty explicitly;
  do not invent Unicode or declare the region unchanged.
- Exact character/glyph evidence must be traceable to supported mappings or
  exact glyph evidence. Font names, OCR guesses, another PDF's text, approximate
  glyph/vector similarity and raster resemblance cannot establish equality.
  A missing inspection capability must limit coverage for the affected evidence;
  do not present a blanket `noFontTools` explanation as occurrence evidence.
- Deduplicate only strictly evidenced paint pairs belonging to the same text
  occurrence, such as a verified fill/stroke pair. Matching coordinates, strings
  or glyph appearance alone do not authorize deletion of a duplicate. Separate
  text-show operations and independent overprints remain distinct; ambiguous
  overprints remain warnings.
- Vector evidence is conservative and bounded. Approximate matching is not
  definitive acceptance: it may propose a possible counterpart for an uncertain
  finding, but cannot supply accepted text or equality. Ambiguous vectors and
  unsupported formula coverage remain warnings/limitations even when extracted
  strings match. A supported structural finding must explain its evidence, not
  imply that arbitrary math structure, figure integrity or image loss has been
  checked.
- Align occurrences one-to-one and context-aware. Repeated text, local context,
  formula position and structural relationships must not be cancelled through
  global word counts or loose similarity. Exact moved text may be accounted for
  without a content finding, but unmatched/ambiguous remainder stays visible.
  No source occurrence may be consumed twice. Headers and footers stay in scope.
- Normalize only evidenced non-drawing U+200B occurrences without a detected
  mapping issue. Preserve counts, positions and raw contexts as Info. Do not
  silently remove other controls, visible marks or real edits beside them.

Neither `complete`, exact text equality, zero warnings nor a supported vector
check certifies figures, image preservation, full mathematical structure,
tables, reading order or layout. Unknown/unsupported regions are not unchanged.
All findings remain advisory; no comparison can approve a review or export.

## Identity And Coverage

Glyph identity, Unicode interpretation and mathematical structure are separate
evidence dimensions. The engine aligns identical exact-outline opaque
glyphs one-to-one when their `opaque_key` and context agree and
`text_identity_supported` is true. This establishes the supported glyph identity
only; it does not invent Unicode, copy a character from the other PDF, or certify
meaning/layout. Unresolved Unicode and limited coverage remain visible even when
these matched occurrences produce no content-change finding: an otherwise
unchanged matched opaque pair can contribute only a coverage limitation, not a
per-occurrence text-change warning. An `opaque_key` alone must not override
resource conflicts or unsupported identity evidence.

Unmatched opaque occurrences remain verification warnings, not confident Unicode
insertions/deletions. Repeated occurrences must still be consumed one-to-one.
Supported subscript/superscript role changes remain structural warnings even when
exact opaque glyph identities align. For known Unicode with unresolved
mathematical baseline attachment, retain the structural coverage limitation
without inventing a text edit. A missing role is not evidence of an ordinary
baseline role; compare roles only where both sides have supported role evidence.

Approximate vector evidence has a narrower use: it may identify a candidate
counterpart using exact neighboring context, but the vector and proposed
counterpart remain uncertainty Warnings. An unknown vector candidate never
becomes an equality anchor or a Unicode guess. Ambiguous or many-to-one
counterpart proposals must not hide findings.

Anchor-based exact vector-region pairing requires all of: an equal
`appearance_key` for a pure opaque fill, including paint properties; equal
`anchor_geometry`; and two mapped text anchors with a unique one-to-one
correspondence. Matching outlines
or nearby text alone is insufficient. Such an exactly evidenced pair can omit
the per-region warning while unknown Unicode and limited coverage remain.

Method 6 also supports exact render-source identity, independently of text
anchors. The worker's `_RenderSource` hashes a bounded closure of page content,
resources and rendering properties, including referenced objects and stream
bytes. It excludes document Info/ID and page metadata, not the rendering inputs.
Parsed numeric objects use exact rational values, so equivalent representations
such as `1` and `1.0` do not create a difference. This is not rounding or a
numeric tolerance: distinct parsed values remain distinct, and content-stream
bytes are not approximately normalized.
The proof uses a strict reader: duplicate dictionary keys and other unsupported
repairs cannot supply exact identity. Inheritable page attributes resolve through
explicit nulls to their effective values; null must not hide inherited resources,
geometry or rotation. Cycles, external/unsupported streams, unresolved objects
or exhausted bounds yield no key, not assumed equality.
Traversal is limited to depth 64, 100,000 visited nodes and a 64 MiB byte budget
per document. Unavailable proof remains an explicit coverage limitation and does
not by itself turn a usable comparison into parser failure.

Bounded static Link annotations can participate in this closure. A supplied
`/P` backlink must identify the enclosing page before that ownership backlink is
excluded from recursive hashing. Local destination/GoTo page references require
a unique page index and retain exact destination arguments; they are navigation
references, not appearance-resource recursion. Only supported URI/GoTo actions
are accepted. Appearance streams/states (`/AP`, `/AS`), their resources, borders,
colors, opacity, flags, rectangles and other appearance properties remain in the
hash. Unknown annotation types, additional actions (`/AA`), chained actions
(`/Next`), unsupported actions, foreign page backlinks and unsupported cycles
or external streams provide no proof. Annotation lists are bounded to 1,000
entries per page; local page-destination resolution is limited to 100 pages.
This is static-paint evidence only, not hyperlink-navigation equivalence.
Named-destination definitions and URI base resolution are not proved by this
key; retaining destination/action data must not be presented as link validation.

The parent can pair a vector region through this path only when the equal
`render_source_key` identifies exactly one page on each side, with equal page
dimensions, equal `appearance_key` and the same absolute region position
(equal normalized box at those dimensions). The region match must also be
unique one-to-one. This supports metadata/document-ID-only rewrites without
inventing Unicode or using approximate similarity. It can omit the matched
region warning, never consume a text change or remove the underlying Unicode,
graphics or other coverage limitations. Evidence counts distinguish these
`exact_render_source_region_pairs` within `exact_vector_region_pairs`.

When only the two text anchors match, without exact appearance and geometry
proof or render-source proof, show both regions in one uncertainty finding. This
is a candidate pair for manual review, not equality: it cannot consume text or
invent Unicode.
Neither exact-proof path generally proves vector-to-font equivalence. A genuine
vector-to-font substitution without sufficient exact evidence remains a manual
uncertainty finding; a metadata-only control does not justify removing it from
a different original/corrected pair.

## Method 6 Corrections

Method-5 investigation exposed a distinction between preserving evidence and
accurately classifying it. Method 6 changes the evidence classification and
correspondence rules, not warning names or display caps:

- Connected table/grid rules are classified as graphics through detected-cell
  edges and perpendicular ruling at both ends, including filled vertical
  segments and supported corner junctions. Join bounds use actual rule
  thicknesses and float32 coordinate roundoff, not approximate glyph equality.
  A narrow aspect ratio alone is insufficient; inline bars, isolated marks and
  unsupported graphics are not silently removed. Graphics coverage limitations
  remain, without certifying table layout.
- Native boundary extraction disables presentation `ActualText` substitutions
  and retains those spans in a separate inventory. Boundary binding still
  requires an unambiguous occurrence match; it does not copy Unicode from the
  other PDF or promote substituted presentation text to glyph evidence.
  Unresolved binding and independent-overprint ambiguity remain explicit.
- Script attachment uses run-level base/context evidence. A small script run
  with nearby larger text but no supported attachment remains unresolved instead
  of acquiring a baseline role from its own same-sized letters. Real attachment
  changes and unknown attachment remain distinct outcomes. For
  example, an author-list comma genuinely moved from baseline to superscript
  remains a structure finding; the corrective goal is not zero structure counts.
- Repeated row labels can use ordered-cell context only within a unique paired
  table with complete supported cells, unique supported headers and the same
  ordered row-label sequence. A genuinely empty corner header gets a typed
  identity only when glyph/image/non-ruling-vector evidence does not occupy it.
  Row/column positions alone or global word counts cannot establish this
  correspondence. Cell contents still undergo comparison; changed/swapped values
  must remain detectable. Incomplete, duplicated or unresolved table identity
  remains a limitation rather than silently matching ambiguous rows.

## Acceptance Handoff

Run the [developer gate](developer_guide.md#regression-commands), the comparison
Node tests, and the [comparison browser scenarios](editorial_acceptance_runbook.md).
Verify the actual engine payload, not only synthetic UI fixtures. Required cases
include one bad occurrence beside valid same-font prose; strict paint-pair versus
independent-overprint cases; exact versus approximate glyph evidence; ambiguous
vectors and unsupported formulas; repeated/context-sensitive alignment; limited
coverage with zero content findings; parser failure; normalization beside real
edits; and unchanged publication/CrossCheck bytes and revision binding. Also cover
exact opaque pairs with unknown Unicode and limited coverage; unmatched/repeated
opaque occurrences; context or outline mismatches; unsupported identity flags;
supported role changes on matched opaque glyphs; unknown mathematical baseline
attachment of known Unicode without invented text edits; and approximate vector
counterparts that remain uncertain rather than aligning as equal.

The regression-suite total is not a semantic acceptance score. Required release
evidence also includes:

1. Versioned real fixtures with input hashes and independently reviewed expected
   finding identities, actual locations and residual limitations. Preserve the
   known edits; explain every remaining warning class. Do not require zero
   warnings on arbitrary PDFs or use the prototype's lower count as an oracle.
2. Metadata/document-ID-only reserialization compared through the actual pure
   `compare_pdf_bytes` entry point, with database access disabled and no audit or
   publication workflow calls. Verify unchanged page geometry, extracted text
   and drawing evidence, content streams where preserved, and rendered pixels
   before treating a sample as content-preserving. Inspect differences rather
   than silently weakening a failed preservation check. Compare the identical-
   bytes control too: its shortcut alone cannot validate the changed-bytes path.
   For reviewed controls whose local occurrences have supported identity proof,
   require zero content, structure and uncertainty findings while retaining
   applicable coverage limitations. This is a fixture-specific expectation,
   not a zero-warning promise for arbitrary PDFs or original/corrected pairs.
3. Realistic connected table borders in both orientations and drawing forms,
   beside genuine mathematical bars and changed cell values. Successful rule
   classification must not erase unsupported graphics coverage or real edits.
4. Actual PDF extraction through repaired mappings, presentation boundaries and
   script attachment. Mocked evidence exercises downstream contracts but cannot
   replace parser-to-finding regression cases for those paths.
5. Mutation checks for punctuation, values, repeated occurrences, arrows and
   script roles, including edits adjacent to unresolved evidence. A cleaner
   unchanged case must not come at the cost of missed changes.
6. Exact render-source proof through actual PDF extraction: metadata/ID changes
   may retain the key; changed content, resources, paint or position must not
   acquire the same proof. Equivalent numeric representations must retain the
   key without merging distinct values. Test duplicate pages/regions, duplicate
   dictionary keys, unsupported annotations/actions, cycles, external streams
   and work limits as no-proof cases. Test supported static Links without
   discarding appearance: changed `/AP` streams, color, flags, rectangles or
   borders must invalidate the old proof. Cover inherited null attributes,
   foreign page backlinks and cyclic/chained actions. Preserve coverage reasons
   and neighboring text findings even when a region pairs exactly. Downstream
   injected-key tests alone do not validate closure hashing.

Metadata-only transformations must not invent content/structure changes or
misclassify table borders as glyph warnings. Supported identity reconciliation
can remove redundant local findings while Unicode or other coverage limitations
remain explicit. Record any residual review burden and its evidence; neither
relabeling uncertainty nor declaring all drawings unchanged satisfies acceptance.

UI fixtures demonstrate presentation behavior only. Record actual-engine gate
results and engine-owner release acceptance separately; this design document
does not assert that all tests or acceptance cases passed.
