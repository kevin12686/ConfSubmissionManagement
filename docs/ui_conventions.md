# UI Conventions

The UI is one server-rendered application, not a set of feature-specific widgets.
Use shared templates, vocabulary and progressive enhancement. Publication state
always belongs to [server rules](publication_rules.md).

## Visual Language

| Element | Convention |
| --- | --- |
| Red | Blocker or dangerous action |
| Amber | Needs human attention |
| Green | Named check is complete |
| Blue / neutral | Tracked information, provenance or history |
| Color meaning | Always accompanied by readable text |
| Tables | Uniform rows, separators and hover; no zebra striping |
| Long text | Wrap within bounded cells; never widen the document |
| Typography | Body/table 15px, supporting text at least 13px, badges 12px |
| Badges | Noninteractive status, not button-like commands |
| Tooltips | Supplemental clarification only; essential state remains visible |
| JSON / paths | Dark monospace text on a muted light surface |

Shared page titles are `Page name · Conference name`. Keep footer at the
viewport bottom on short pages. Use shared icon assets, focus states and
contrasting buttons; do not invent a local color palette.

The page must not overflow at 390px. Wide data tables may scroll inside their
own containers. Avoid nested cards and constrained alert columns containing
full-width tables.

## Vocabulary And Feedback

Use `submissions/presentation.py` labels. Short forms include Current final,
Replaced, Discarded, Original, Corrected, Pending, Review OK, Needs edit,
Needs processing and Allowed exception. Display strings never drive rules.

| Message lifetime | Presentation |
| --- | --- |
| Save/update success | Shared Toast |
| Failed operation / warning | Dismissible, non-autohiding Toast where appropriate |
| Validation, confirmation, readiness blocker | Persistent inline content |
| Table/list/multi-step alert | Vertical `.cfm-alert-stack`, not Tabler's implicit horizontal flex |
| Missing prerequisite | One clear root cause; suppress derivative badge noise in the compact row |

Toast stack sits below a visible navbar, otherwise near the viewport's upper
right. Text/close icons must remain readable. Never put a publication blocker
only in a disappearing Toast.

## Navigation And Worklists

- User search is fuzzy; workflow links use exact record targeting.
- Do not navigate to a paper by using a short numeric ID as broad `q`.
  Preserve the canonical exact-target parameters supplied by the selector.
- Keep originating filter/search/sort/return context where applicable. Validate
  return URLs against same-origin rules.
- Filter/sort **before** pagination. Shared sizes are 25/50/100/200/All, default 25.
  Counts describe their stated scope, not only the current page.
- Show top and bottom controls for main worklists. A nested lazy history list
  does not duplicate outer page-size controls unnecessarily.
- Bottom navigation returns to the list start. Top navigation avoids needless
  scrolling when its header is already visible.
- HTMX and full navigation must preserve equivalent URL/filter behavior.
  Card saves restore the owning card; do not leave another paper's async result
  attached after filtering/reordering.
- Use the shared panel reveal positioning for table details and card reviews:
  if the row/card plus panel fits but is clipped, center it; otherwise align its
  owner at the viewport start. Expansion must not widen a table.
- Header actions operate on the current page or dataset. General navigation
  belongs in Navbar; condition-specific workflow links belong beside evidence.

## Forms And Dynamic Fragments

Titles and authors use wrapping textareas. Notes preserve useful paragraphs.
Changed markers compare against loaded values and clear when restored.
Server preview, not browser dirty state, decides effective changes and resets.

Paper pickers share the searchable lookup/selection component and server
validation. Selection must remain distinguishable from free text. Scope-changing
actions require explicit confirmation; no implicit publication decisions.

Lazy panels use shared loading, success and error states. Initialize images,
magnifiers and forms after HTMX replacements as well as full loads. Position
restoration must open/load a lazy panel, not leave permanent Loading text.
Hide broken-image placeholders until load completes; show a retry/error state
rather than stale pixels.

Keep event listeners scoped/delegated and clean up timers/observers on removal.
Do not duplicate a page-specific fix in every template.

## Formatting Workspace

List, Focus and Single use shared review content and revision-history partials.
Single queue derives from current filter/search. Save stays on the current
paper; navigation is explicit and warns on dirty form state.
A saved paper leaving a filter uses its bounded one-return receipt, not an
async comparison panel floating into unrelated lists.

Revision history loads on expansion, newest first. Use responsive rows rather
than an overflowing table; show current-file identity only when bytes/sources
match. Missing/corrupt files have explicit errors; no restore/select controls.

Comparison services is Formatting-only: compact remote/local release,
backend/status with detailed settings and last-seen labelling. Poll every ten
seconds only while visible, no overlapping requests. Polling must not start
work or silently grant CPU consent.

## Images And PDFs

| Surface | Shared behavior |
| --- | --- |
| Formatting / Title-Author image | Ctrl-hover magnifier, readable nonoverlapping hint, hint hidden immediately while active |
| Lazy image frame | Loading state; after load sizing follows actual image ratio, no fake empty strip |
| Verification image | One renderer for built-in, GROBID and manual override; clear source/title/authors |
| PDF open action | Shared PDF.js URL/tag, exact originating file endpoint |
| PDF reader | Full viewport, compact toolbar, Fit page / Single default, optional continuous mode and lazy thumbnails |

Reader wheel and keyboard share boundary/gesture logic. Inputs/toolbars retain
normal behavior. Open directly / Download keep the same file identity.
No PDF scripting, editing or serialized output enters editorial data.

## Comparison Inspection

The documents are primary, findings secondary. Text changes use red outlines
and arrows; unlocated ranges use amber emphasis; information uses quiet neutral
marks. Word/wrap hyphens share one Hyphens group.

The upper inspection dock reserves stable space for Magnification/Evidence.
Full aspect-correct crops are visible without internal scrolling; only Evidence
can scroll. Hover follows the pointer. Click selects persistent evidence;
entering the dock freezes the last crop instead of dismissing it.

Connection and Auto match share one selected side-specific pair. With a pair,
opposite crop follows the offset even beyond box edges; without one, relative
position. No counterpart means explicit empty state, never an unrelated page.
Candidate links remain unverified. Hiding/offscreen lines does not destroy a
valid logical pair. Page/finding changes invalidate stale callbacks.

High-resolution detail comes from the same validated snapshot, never a fresh
publication resolution. Preserve pixel aspect/scale at narrow widths and Retina
DPR. Loading/failure must not show another finding's crop. Technical evidence
counts/policy/identities stay in a disclosure; coverage warnings stay visible.
Full interaction and render bounds live in [PDF Comparison](pdf_comparison.md).

## UI Review Checklist

Test desktop, short window and 390px; keyboard/touch; normal/HTMX navigation;
top/bottom pagination; lazy open/close; dirty-state warning; Toast contrast;
image/crop loading/failure; and exact-target links with similar Paper/Final IDs.
Check actual screenshots and canvas pixels, not just CSS assertions.
