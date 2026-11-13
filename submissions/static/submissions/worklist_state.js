(function () {
    "use strict";

    function capture(root, submittedForm) {
        const owner = submittedForm?.closest("tbody[id]");
        const rows = Array.from(root.querySelectorAll("tbody[id]"));
        const anchor = owner || rows.find((row) => row.getBoundingClientRect().bottom > 0);
        const opening = new Set(Array.from(root.querySelectorAll('[aria-expanded="true"][aria-controls]'),
            (trigger) => trigger.getAttribute("aria-controls")));
        const active = typeof document === "undefined" ? null : document.activeElement;
        const focus = active?.matches("textarea[id]") && root.contains(active) && !owner?.contains(active)
            ? {id: active.id, start: active.selectionStart, end: active.selectionEnd,
                direction: active.selectionDirection, baseline: active.defaultValue} : null;
        return {
            focus,
            anchors: anchor ? rows.slice(rows.indexOf(anchor)).concat(rows.slice(0, rows.indexOf(anchor)).reverse())
                .map((row) => ({id: row.id, top: anchor.getBoundingClientRect().top})) : [],
            expanded: Array.from(root.querySelectorAll(".collapse[id], .collapsing[id]"))
                .filter((node) => node.classList.contains("show") || opening.has(node.id))
                .map((node) => node.id),
            drafts: Array.from(root.querySelectorAll("textarea[id]"))
                .filter((field) => !owner?.contains(field) && field.value !== field.defaultValue)
                .map((field) => ({id: field.id, value: field.value, baseline: field.defaultValue})),
        };
    }

    function restore(root, state, reposition = true) {
        for (const draft of state.drafts) {
            const field = document.getElementById(draft.id);
            // Never replace a newly persisted value or signed evidence with a draft.
            if (field && root.contains(field) && field.defaultValue === draft.baseline) field.value = draft.value;
        }
        for (const id of state.expanded) {
            const panel = document.getElementById(id);
            if (!panel || !root.contains(panel)) continue;
            panel.classList.add("show");
            for (const trigger of root.querySelectorAll("[aria-controls]")) {
                if (trigger.getAttribute("aria-controls") === id) trigger.setAttribute("aria-expanded", "true");
            }
        }
        const focusedField = state.focus && document.getElementById(state.focus.id);
        if (focusedField && root.contains(focusedField) && focusedField.defaultValue === state.focus.baseline) {
            focusedField.focus({preventScroll: true});
            focusedField.setSelectionRange(state.focus.start, state.focus.end, state.focus.direction);
        }
        if (!reposition) return;
        const anchor = state.anchors.find((item) => document.getElementById(item.id));
        const node = anchor && document.getElementById(anchor.id);
        if (node) {
            window.scrollBy({top: node.getBoundingClientRect().top - anchor.top, behavior: "instant"});
        } else {
            root.scrollIntoView({block: "start", behavior: "instant"});
        }
    }

    if (typeof module !== "undefined") module.exports = {capture, restore};
    if (typeof document === "undefined") return;
    const pending = new WeakMap();
    // Keep drafts that temporarily leave the page after server-side sorting.
    // In-memory only: never persist evidence or editorial text in localStorage.
    const draftsByWorklist = new Map();
    document.addEventListener("htmx:beforeSwap", (event) => {
        const {target, requestConfig, xhr} = event.detail || {};
        if (!event.detail?.shouldSwap || !target?.matches?.("[data-cfm-preserve-worklist]")) return;
        const isPost = requestConfig?.verb?.toLowerCase() === "post";
        const state = capture(target, isPost ? requestConfig.elt?.closest("form") : null);
        const drafts = draftsByWorklist.get(target.id) || new Map();
        for (const field of target.querySelectorAll("textarea[id]")) drafts.delete(field.id);
        for (const draft of state.drafts) drafts.set(draft.id, draft);
        draftsByWorklist.set(target.id, drafts);
        state.drafts = Array.from(drafts.values());
        if (!isPost) state.expanded = [];
        pending.set(xhr, {state, isPost, id: target.id});
    });
    document.addEventListener("htmx:afterSettle", (event) => {
        const saved = pending.get(event.detail?.xhr);
        if (!saved) return;
        pending.delete(event.detail.xhr);
        const root = document.getElementById(saved.id);
        if (root) {
            restore(root, saved.state, saved.isPost);
            for (const draft of saved.state.drafts) {
                const field = document.getElementById(draft.id);
                if (field && field.defaultValue !== draft.baseline) draftsByWorklist.get(saved.id)?.delete(draft.id);
            }
        }
    });
})();
