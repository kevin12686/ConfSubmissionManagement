# UI Conventions

This document is the canonical implementation guide for shared UI, worklist,
navigation, and presentation behavior. Publication decisions remain governed by
[Publication Rules](publication_rules.md) and server-side services.

## Design Boundary

The UI is server-rendered Django using locally pinned Tabler 1.4.0 and HTMX
2.0.10. Progressive enhancement may improve navigation and file selection, but
must not own workflow state, publication scope, file resolution, exceptions,
or review decisions.

Every enhanced GET must remain a valid normal URL. Every state-changing action
must remain a normal CSRF-protected, audited server POST.

Browser titles use `<Page Name> · <Conference Name>` from the shared base
template so tabs and bookmarks remain identifiable across conference
instances. Individual pages provide only their page-name title block.

## Feedback And Alerts

Use the feedback channel that matches the lifetime of the message:

| Content | Presentation |
| --- | --- |
| Successful save or short-lived update | Django message in the shared Toast stack |
| Warning or failed operation | Dismissible Toast that does not autohide |
| Workflow state, readiness blocker, confirmation, or validation | Persistent inline content |
| Alert containing lists, tables, or multi-step forms | `.cfm-alert-stack` |
| Short message paired with compact actions | Explicit `.d-flex` alert |

Tabler alerts default to horizontal flex layout; ordinary CFM alerts use
vertical document flow. Never put persistent publication state only in a Toast.

## Visual Language And Accessibility

- Red means blocker or danger.
- Amber means manual attention.
- Blue means tracked or informational state.
- Green means the named review is complete.
- Gray means inactive or history.
- Every color-coded state also includes text.
- Body and table text use 15px; supporting text has a 13px minimum; badges use
  12px.
- Tables use uniform row surfaces and horizontal separators, not zebra striping.
- Long tables use `cfm-table-sticky` and scroll inside their container.
- Buttons retain visible focus, sufficient height, and labels that fit at
  narrow widths.
- Technical paths, actions, and JSON use dark monospace text on a muted light
  surface.

The page itself must not overflow at 390px. Wide tables may scroll within their
containers. Keyboard operation must remain available for controls, modals, and
collapses.

## Navigation And Exact Targets

User search is fuzzy; system navigation is exact.

### Page Header Actions

The Navbar owns ordinary cross-page navigation. A page header is reserved for:

- commands that act on the current page or its records;
- switches between views of the same dataset;
- local summaries such as Note Summary;
- explicit Back actions in focused, confirmation, or detail views.

Do not repeat a generic Navbar destination in the upper-right header merely as
a shortcut. Put a cross-workflow link beside the condition or result that makes
it relevant:

- Dashboard readiness links to Error Report when blocked and to the anchored
  Final Publication Package when clear;
- Process PDFs links to the Organized List PDF-issue filter only when PDF
  issues exist;
- Plagiarism import/report results offer their review link only after data was
  changed;
- Export Reports places readiness review inside the Final Publication Package
  section and blocked-export alert;
- Author Count links to the author exception filter only when an over-limit
  author exists.

Operational controls such as extraction, import, upload, save, add, and
Checklist/Compact view switches remain in their page headers. Moving a link
must never change the server-side scope, readiness result, review state, or
publication source.

| Destination | Exact parameter |
| --- | --- |
| Final Submission workflow | `submission=<pk>` |
| Organized List paper | `paper_id=<exact Paper Master ID>` |
| Exception row | `exception_key=<service row key>` |

Reserve `q` for user-entered search. An exact target outside the workflow scope
renders the shared focused-worklist explanation; it never substitutes a nearby
record or widens the service queryset.

Contextual links to Final Submission Edit pass a same-site `next` URL. The
controller validates it with `url_has_allowed_host_and_scheme()`. Save returns
to the originating worklist with its view, filter, search, sort, tab, page, and
single-paper context.

## Worklist Contract

Shared worklists follow this sequence:

1. Select lightweight rows for the complete scope.
2. Apply server-side filters and validated sorting.
3. Paginate.
4. Hydrate file checks, previews, diffs, suggestions, and signed evidence only
   for the displayed page or exact focused record.

Supported page sizes are `25`, `50`, `100`, `200`, and `all`; the default is
25. Pagination uses `submissions/application/pagination.py` and renders the
shared component above and below the rows.

Top pagination preserves its visible viewport position when the user changes
page or page size, so a table or card-list header that is already visible does
not move unnecessarily. Bottom pagination returns immediately to the refreshed
worklist start. Both behaviors also protect against invalid absolute scroll
offsets when the next page is shorter. Pagination and worklist restoration must
not inherit framework-level smooth scrolling: routine navigation should not
visibly travel across replaced content. Keep this policy in the shared
pagination component and `worklist_navigation.js`, not in page-specific scripts.

Filtering, sorting, tabs, and pagination preserve one another's query
parameters. Natural identifier sorting uses `natural_text_key()`, so `P2`
precedes `P10`.

Worklist tabs use `nav nav-tabs cfm-tabs`. Tabs that change the result set are
server-side filters applied before pagination; they must not partition only the
current browser page.

## Partial Navigation And Position Restoration

HTMX may replace only the named worklist container and update browser history.
The underlying URL remains directly refreshable and shareable.

`submissions/static/submissions/worklist_navigation.js` restores post-action
position. Worklists opt in with `data-cfm-worklist`; cards provide stable
`data-cfm-worklist-card` IDs. After an ordinary POST/redirect it:

- returns to the original card and viewport offset;
- selects the next or previous visible card if a filter removed the changed row;
- reopens a card-owned collapse where appropriate.

Workflow mutation, evidence checks, filtering, and publication decisions remain
server-side. Lazy components listen for `cfm:worklist-expanded`, and HTMX
initialization uses `htmx:load` with `event.detail.elt`.

## Searchable Paper Pickers

Large Paper ID selectors use the shared local Tom Select component rather than
rendering the full Paper Master List into every form.

- Do not preload results. An empty query shows no papers.
- Master searches rank exact Paper ID first, then ID prefix/contains, Master
  Title, and Master Authors; responses are capped at 20.
- Paper ID Review and Editor Upload results show Paper ID plus Master Title.
  Process PDFs results show only Paper ID and Final ID.
- Do not auto-select the first result and do not allow arbitrary values.
- Server-side form/service validation remains authoritative.
- Initialize on `DOMContentLoaded` and `htmx:load`; destroy instances before
  HTMX removes their elements.
- Keep picker styling scoped under `.cfm-paper-picker` so ordinary selects and
  other worklists retain the shared site styling.

## Expensive Evidence And Request Context

Do not decode, hash, or render evidence for rows that will not be displayed.
Signed evidence tokens are also generated after pagination and must not perform
database queries or publication-file reads.

Publication-wide read pages share
`submissions.services.publication_read.PublicationReadContext`. Pass its
`FileInspectionContext` through publication-facing helpers so a path is
inspected once per request. UI caches and display summaries never feed export
decisions.

Final export uses strict fresh hashing and snapshot byte reads as defined in
[Publication Rules](publication_rules.md#export-integrity).

## Formatting Review Modes

Formatting Review has three distinct modes:

- List mode: compact worklist with one expanded paper at a time.
- Single Paper Mode: session-backed, naturally sorted queue captured from the
  current filter/search result.
- Focus mode: one exact record opened from another workflow.

Status changes do not reorder a Single Paper queue. Previous/Next skip records
that later leave publication scope. Single and Focus modes do not show normal
worklist pagination, and Focus mode never creates a queue.

Every Formatting POST includes a short-lived review snapshot. Save and
title-guard confirmation recheck row time, active scope, and selected file
identity. Do not bypass `save_formatting_review()` with a direct controller
write.

Validation accepts a recognized PDF/source pair even if the two upload fields
were swapped. It rejects two PDFs, two recognized source files, and unknown
files in the PDF field. Bound status, notes, and errors remain visible; browser
file inputs must be selected again after failure.

## Title/Author Evidence

Built-in, GROBID, and Manual Override use the shared renderer in
`submissions/services/title_author_verification.py`.

The renderer:

- reuses only verified blank space above the source page;
- extends upward when the header cannot safely fit;
- preserves case, punctuation, hyphens, superscripts, and digits;
- draws only raw character geometry represented by the extraction;
- allows the last extracted word to be a prefix of a longer PDF word;
- marks alphabetic continuation as an orange partial-word warning;
- treats numeric or symbolic continuation as normal metadata;
- prefers complete matches over partial matches;
- keeps the `A1...AN` legend color consistent with evidence state.

If reliable raw character geometry is unavailable, render no author boundary
instead of inventing a whole-word box. This affects evidence only; it must not
rewrite extracted metadata or title comparison.

Manual Override forms load only when their panel opens. The partial endpoint is
read-only; the audited POST remains the only mutation path.

## Shared Image Magnifier

Formatting previews and Title/Author verification images use
`image_magnifier.js` and `image_magnifier.css` through
`data-cfm-image-magnifier`.

The magnifier:

- runs only on fine hover pointers;
- activates while `Ctrl` is held;
- uses a constrained responsive `3:2` lens;
- clears modifier state on key release and window blur;
- reinitializes after lazy collapse loading and HTMX swaps;
- never writes workflow or publication state.

Touch/coarse-pointer layouts retain the static image and normal full-file link.
Hint text comes from `data-cfm-image-magnifier-hint`, not the browser-native
`title` tooltip.

## Workflow-Specific Conventions

- Process PDFs keeps complete page-thumbnail strips expanded. Filters narrow
  papers, not pages within a matching paper. Fixed dimensions prevent layout
  shift during lazy loading.
- Process PDF formatting triage appends to the existing Formatting notes, sets
  Needs Edit, and clears the previous source binding without changing files or
  unrelated review state.
- Organized List separates publication blockers from tracked information and
  owns both Checklist and Compact candidates views.
- Error Report uses server-side area, severity, and multi-category filtering
  before pagination. Category checkbox pills are arranged in workflow-area
  matrix rows, apply immediately through the scoped HTMX worklist, and use
  Error Report-scoped Critical/Medium/Info colors; do not change shared
  badge/button styling to represent their selected state. Large duplicate
  groups load complete details through a read-only endpoint.
- Final Submissions keeps Import/Re-upload collapsed by default.
- Upload zones may summarize and remove browser-selected files, but server
  extension/hash validation and preview-before-apply remain authoritative.
- Destructive version actions live in a separate collapsed danger area.
- Export Reports keeps Final Deliverable visually and behaviorally separate
  from Excel reporting. The Editorial Publication Workbook presents its
  mandatory core sheet and optional supporting sheets in one selector; raw
  active/old-version spreadsheets live in one collapsed Advanced / Debug Excel
  area and are never mixed into the editorial workbook.
- POST forms that return file attachments use the shared
  `data-cfm-download-form="true"` lifecycle. The browser adds a one-use token,
  prevents duplicate submission while the server prepares the file, and
  re-enables only the controls it temporarily disabled after the matching
  completion cookie arrives. Do not add page-specific download-button reset
  scripts.

## Review Checklist

When adding or changing shared UI:

1. Verify the normal non-HTMX GET or POST path.
2. Verify URL, filter, sort, tab, page, and focus context preservation.
3. Verify publication and review state are unchanged by read-only requests.
4. Verify keyboard, narrow-width, and coarse-pointer behavior.
5. Verify expensive hydration occurs only after pagination.
6. Add acceptance coverage for shared behavior.
7. Update this document instead of duplicating the rule in every workflow guide.
