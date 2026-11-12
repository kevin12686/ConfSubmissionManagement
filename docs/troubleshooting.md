# Troubleshooting

Find the symptom, inspect its exact Paper/Final ID, then use the normal workflow.
Do not repair publication state by editing SQLite or swapping files in `data/`.

## Startup And Deployment

| Symptom | Check / safe next step |
| --- | --- |
| Python/venv fails | Native needs Python 3.12+. Rename only the broken `.venv`, recreate/install; preserve DB/data |
| Port 8000 in use | Stop the known server or choose another native `DJANGO_PORT`; do not kill an unidentified process |
| Startup preflight fails | Read the category: models, dependency/provider, worker auth/contract or network; repair that prerequisite before migrations |
| Missing/corrupt OCR models | Provision exact selected models; checksum failures refuse overwrite. Do not substitute models or bypass checks |
| Worker env/auth missing on update | Restore original `worker.env`/clients; never use `--init` to invent replacements |
| Worker project/backend/mount mismatch | Verify owning checkout and preserved identity; a rename is not an update |
| Existing env edits seem ignored | Run selected-conference updater; `docker restart` does not reload Compose env |
| Dry-run succeeds but apply fails | Dry-run is configuration planning, not live build/API/runtime validation |
| Deployment cutover failed | Inspect retained env/containers and earlier completed updates; do not assume migrations rolled back |
| New UI but old worker | Update both from compatible code; check Formatting Comparison services version and Details |

Use [Deployment](deployment.md) for commands and
[Operations](operations.md#recovery) for recovery.

### Remote Worker / Gateway

| Observation | Meaning / action |
| --- | --- |
| 401 or worker unauthorized | Verify that conference's worker bearer token/clients entry |
| Cloudflare 1010 | Browser-signature policy rejected the client, not proof of bad worker token |
| Access redirect/login HTML | Use the direct API origin and correct Service Auth headers/policy; redirects are not followed |
| 403 without recognized provider code | Access denied; do not guess the layer from status alone |
| Ready/cuda health | Worker runtime is validated; it does not certify PDFs or guarantee job duration |
| Busy | Service has work; do not force another copy elsewhere |
| Not ready / preflight pending | Wait/check the reported runtime category; do not treat as auth success plus usable compute |
| GPU models unloaded, service Ready | Expected idle state; a new inference reloads weights |
| Local fallback confirmation | Remote is configured but new work would use local CPU; you can repair remote instead |

CFM uses the shared application User-Agent. Optional custom headers must be
valid one-line JSON in each conference env. They supplement, not replace,
`Authorization`. Do not paste token/env/inspect output into support logs.

## Imports And Record Edits

| Symptom | Action |
| --- | --- |
| Preview changed records but nothing saved | Apply is a separate confirmation |
| Edit/preview says stale | Reopen/review current values and re-upload if required; no unseen-state merge |
| Master notes would be lost | Keep default Preserve existing system notes |
| Existing paper absent from import | Import is not a replace-all/delete operation |
| Mapping Table rejected | Use separate Master/Final templates |
| Upload files unmatched | Check Final ID and template filename rules; verify preview before Apply |
| Too many uploaded files | Split requests above 5000 files; not a CSV row limit |
| Official ID changed unexpectedly | Check Author-entered ID, prior blank mapping and audit; unchanged populated mapping is preserved on re-import |

## Scope, Processing And Reviews

| Symptom | Action |
| --- | --- |
| Master has no Final, will not publish | Mark Master Not Publishing with reason, optional note and confirmation |
| Final not in Master | Resolve through signed Paper ID picker or explicitly exclude; invalid ID is not auto-exclusion |
| Decision Required | Resolve Master publication decision; neither draft nor final can bypass it |
| Integrity conflict | Resolve Master/Final decision mirrors through normal services; do not trust the more convenient field |
| Start2/Editor conflict | Inspect/discard unwanted version with reason |
| Dashboard Process needed | Run Process PDFs for current candidates; missing PDFs are a different issue |
| Old preview/pages after correction | Run Process PDFs, then re-extract/review as requested |
| Pending Format shows no source hash | Normal until Review OK; missing hash is an integrity issue only after approval |
| Review link shows similar IDs | Use exact system link/target; remove a manual broad search if needed |
| Saved paper disappears from filter | Status changed out of that filter; use All/exact link. Single mode retains its captured queue |
| Single queue stale/expired | Return to list and start a new queue |
| Manual override/review panel stuck loading | Check failed request/console; reload. Do not repeatedly save or edit DB |
| GROBID unavailable | Check Settings URL/health; prior extraction is retained on failure |
| Extraction failed with a field error such as 'lines' | Read card message and audit/server traceback; malformed extraction data is failure, not reviewed metadata |

Process PDFs does not rewrite manuscripts. Debug-copy loss cannot change export
selection. A missing **selected Corrected** PDF/source must be restored/replaced,
not worked around by pointing at debug/legacy paths.

## PDF Reader And Images

| Symptom | Check |
| --- | --- |
| PDF reader blank/error | Same-origin endpoint validity, file availability, static worker/module request and MIME type |
| Works with Open directly only | Reader/static problem, not permission to select another PDF |
| Images missing after restore | Verify archive includes referenced review artifacts; do not re-extract automatically and reset approved review |
| Image appears broken before loading | Shared loading/error component should hide incomplete image; inspect failed image request |
| Magnifier absent | Hold Ctrl on supported first-page/verification image; check lazy/HTMX initialization |
| Low-detail comparison crop | High-detail fetch may fail/time out; Preview only is explicit. Embedded raster images cannot gain detail beyond their original pixels |

Nginx must serve PDF.js `.mjs` as JavaScript and retain fonts/CMaps/WASM assets.
Do not "fix" this by changing file selection, disabling CSRF or enabling PDF scripts.

## Comparison

| Symptom | Meaning / next step |
| --- | --- |
| Many red marks | OCR can misread; inspect raw evidence. Counts are not confirmed document defects |
| Many Info marks | Deliberate visual/spacing/hyphen tolerances; these can hide genuine similar-looking edits |
| Limited/unlocated | Missing reliable local correspondence; inspect actual PDFs |
| Unavailable/timeout | No completed result, not no changes. Check backend/work bounds; cold CPU can exceed 300 s |
| Old-method snapshot rejected | Rebuild with current CFM/worker contract |
| Comparison on wrong-looking card | Verify owning Final/revision; refresh/reopen. Never approve from another card's cached panel |
| Blank opposite side | Valid when the linked page has no counterpart; manual page navigation is available |
| Auto match seems relative | Inspect selected finding/pair; no valid pair means relative position |
| Current Corrected missing | Restore/replace current file; history/cache is not fallback |

New-compute confirmation is unnecessary only for complete validated cache.
Health, job completion and zero findings do not approve publication.
See [comparison limits](pdf_comparison.md#accepted-blind-spots).

## CrossCheck

| Symptom | Action |
| --- | --- |
| Login prevents Test connection | Enter optional service credentials before testing/loading folders |
| Service on Docker host unreachable | Use reachable host address, often `http://host.docker.internal:18765`; container localhost is not the host |
| Wrong folder risk | Confirm exact folder/conference association in Settings before upload |
| Countdown but scores unchanged | Poll updates job evidence, not paper scores; result Apply is explicit |
| Complete job, missing reports | Inspect per-paper outcomes; Retry remaining downloads only for confirmed uploads |
| Lost submit/retry response | Check progress to reconcile original job; don't re-upload blindly |
| Result stale | Exact Final/PDF or current plagiarism evidence changed; refresh preview or create a new batch |
| Retry won't overwrite applied result | Intentional replay protection, including after restore |
| P/S exception stale | Score changed from approved value; re-review/re-approve |
| Missing report but scores present | Reports are optional for manual missing-score export; automatic apply needs its required report evidence |

## Exports, Audit And Recovery

| Symptom | Action |
| --- | --- |
| Final package blocked | Follow exact Error Report findings; draft cannot bypass structural ambiguity |
| Workbook contains Not Publishing | Expected in selected internal supporting sheets; not in final ZIP |
| Source missing after Review OK | Restore/review exact selected source; don't weaken source binding |
| Excel generation failed | Check output permissions, snapshot concurrency and audit failure; no partial workbook should be promoted |
| Error count differs from visible rows | Counts can span all filtered results; confirm severity/category/search before pagination |
| Audit empty/unwritable | Check application storage/permissions; don't fabricate past events or hide audit failures |
| Storage inventory fails | Inspect inaccessible roots; cleanup must not treat unreadable storage as empty |
| Restore rejected | Check archive version/completeness/hashes; never bypass validation or relabel its version |
| Old-machine path remains | Stop publication, inspect restore preview/reference warnings and backup completeness |
| Restored timestamps look new | Current restore preserves them; an old restore's overwritten history needs the original backup |

Keep original ZIP/logs when investigating. [Operations](operations.md) covers
what is backed up, cleanup boundaries and mirror rollback.
