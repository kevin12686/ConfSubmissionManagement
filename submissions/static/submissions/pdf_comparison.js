(function () {
    "use strict";
    function initialize() {
        document.querySelector("[data-comparison-build]")?.addEventListener("submit", function () {
            const button = this.querySelector("button[type=submit]");
            button.disabled = true;
            button.textContent = "Comparing PDFs...";
        });
        const root = document.querySelector("[data-pdf-comparison]");
        const script = document.getElementById("pdf-comparison-data");
        if (!root || !script) return;
        const data = JSON.parse(script.textContent);
        const query = (selector) => root.querySelector(selector);
        const state = {selected: null, category: "all", mode: "pages", before: 1, after: 1, linked: true};
        const sides = {};
        for (const name of ["before", "after"]) {
            const panel = query(`[data-comparison-side="${name}"]`);
            sides[name] = {
                panel, page: panel.querySelector(".cfm-comparison-page"),
                input: panel.querySelector("[data-comparison-page]"),
                text: panel.querySelector("[data-comparison-text]"),
                layer: panel.querySelector(".cfm-comparison-mark-layer"),
                findingPages: panel.querySelector("[data-comparison-finding-pages]"),
            };
            sides[name].page.querySelector(":scope > img").addEventListener("error", function () {
                query("[data-comparison-image-error]").hidden = false;
            });
            sides[name].input.addEventListener("change", () => navigate(name, Number(sides[name].input.value)));
            sides[name].findingPages.addEventListener("change", () => navigate(name, Number(sides[name].findingPages.value), true));
            panel.querySelectorAll("[data-comparison-page-step]").forEach(button => button.addEventListener("click", () => navigate(name, state[name] + Number(button.dataset.comparisonPageStep))));
        }
        function visibleFindings() {
            return data.findings.filter(finding => state.category === "all" || finding.category === state.category);
        }
        function pageLabel(finding, name) {
            const pages = finding[`${name}_pages`];
            return pages.length ? `${pages.length === 1 ? "page" : "pages"} ${pages.join(", ")}` : "no corresponding text";
        }
        function renderList() {
            const list = query("[data-comparison-findings]");
            const findings = visibleFindings();
            query("[data-comparison-empty]").hidden = findings.length > 0;
            if (!list.querySelector("[data-finding-id]")) {
                for (const finding of data.findings) {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "cfm-comparison-finding";
                    button.dataset.findingId = finding.id;
                    const label = document.createElement("span");
                    label.textContent = finding.label;
                    const location = document.createElement("small");
                    location.textContent = `Before ${pageLabel(finding, "before")} / After ${pageLabel(finding, "after")}`;
                    button.append(label, location);
                    button.addEventListener("click", () => select(finding, true));
                    list.querySelector(`[data-finding-group="${finding.category === "sequence" ? "sequence" : "attention"}"]`).appendChild(button);
                }
            }
            for (const button of list.querySelectorAll("[data-finding-id]")) {
                const finding = data.findings[Number(button.dataset.findingId)];
                button.hidden = state.category !== "all" && finding.category !== state.category;
                button.setAttribute("aria-pressed", String(state.selected === finding.id));
            }
            list.querySelectorAll("[data-finding-group]").forEach(group => {
                const count = group.querySelectorAll("[data-finding-id]:not([hidden])").length;
                group.parentElement.hidden = count === 0;
                group.parentElement.querySelector("[data-finding-group-count]").textContent = count;
            });
        }
        function renderPages() {
            const finding = data.findings.find(item => item.id === state.selected);
            for (const name of ["before", "after"]) {
                const side = sides[name];
                side.input.value = state[name];
                side.panel.querySelector(".cfm-comparison-page-controls").hidden = state.mode !== "pages";
                const pages = data[`${name}_pages`];
                const page = pages[state[name] - 1];
                const actualPages = finding?.[`${name}_pages`] || [];
                const status = side.panel.querySelector("[data-comparison-side-position]");
                status.textContent = !finding ? "Manually selected page." : !actualPages.length
                    ? `No corresponding text. Reference page ${state[name]}${finding[`${name}_reference_page`] === state[name] ? " selected from matched page content." : " is manually selected."}`
                    : actualPages.includes(state[name]) ? `Showing passage on page ${state[name]}.` : `Reference page ${state[name]}; this finding is on ${pageLabel(finding, name)}.`;
                side.findingPages.replaceChildren();
                for (const number of actualPages) {
                    const option = document.createElement("option");
                    option.value = number;
                    option.textContent = `Finding page ${number}`;
                    side.findingPages.appendChild(option);
                }
                side.findingPages.value = actualPages.includes(state[name]) ? state[name] : "";
                side.findingPages.hidden = actualPages.length < 2 || state.mode !== "pages";
                side.page.style.aspectRatio = `${page.width} / ${page.height}`;
                side.page.hidden = state.mode !== "pages";
                side.text.hidden = state.mode !== "text";
                const text = finding?.[`${name}_text`];
                side.text.textContent = text || (finding ? "No matched text on this side. Inspect the page image." : "Select a text finding, or use Page comparison to inspect the full PDFs.");
                if (text) {
                    const changed = document.createElement("span");
                    changed.className = finding.category === "sequence" ? "cfm-comparison-text-sequence" : "cfm-comparison-text-change";
                    changed.textContent = text;
                    side.text.replaceChildren(changed);
                }
                const context = finding?.[`${name}_context`];
                if (context && context !== text) {
                    const label = document.createElement("div");
                    label.className = "small text-muted mt-3 mb-1";
                    label.textContent = "Surrounding text";
                    const passage = document.createElement("div");
                    passage.textContent = context;
                    side.text.append(label, passage);
                }
                side.layer.replaceChildren();
                for (const location of finding?.[`${name}_locations`] || []) {
                    if (location.page !== state[name]) continue;
                    const box = document.createElement("span");
                    box.className = "cfm-comparison-mark";
                    const [x0, y0, x1, y1] = location.box;
                    Object.assign(box.style, {left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${Math.max(0, x1 - x0) * 100}%`, height: `${Math.max(0, y1 - y0) * 100}%`});
                    side.layer.appendChild(box);
                }
                if (state.mode === "pages") {
                    const url = data.page_urls[name].replace("/0.png", `/${state[name]}.png`);
                    if (side.page.dataset.comparisonLoadedUrl !== url) {
                        side.page.dataset.comparisonLoadedUrl = url;
                        window.CFMImageLoader.load(side.page, url);
                    }
                }
                side.panel.querySelectorAll("[data-comparison-page-step]").forEach(button => {
                    const value = state[name] + Number(button.dataset.comparisonPageStep);
                    button.disabled = value < 1 || value > pages.length;
                });
            }
            root.querySelectorAll("[data-comparison-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.comparisonMode === state.mode)));
            query("[data-comparison-link]").disabled = state.mode !== "pages";
        }
        function select(finding, reveal = false) {
            state.selected = finding?.id ?? null;
            if (finding) {
                state.before = finding.before_page || finding.before_reference_page || state.before;
                state.after = finding.after_page || finding.after_reference_page || state.after;
            }
            root.classList.toggle("cfm-comparison-sequence-selected", finding?.category === "sequence");
            const findings = visibleFindings();
            const index = findings.findIndex(item => item.id === state.selected);
            query("[data-comparison-heading]").textContent = finding?.label || "Manual page comparison";
            query("[data-comparison-location]").textContent = finding ? `Before ${pageLabel(finding, "before")} / After ${pageLabel(finding, "after")}` : `Before page ${state.before} / After page ${state.after}`;
            query("[data-comparison-position]").textContent = index < 0 ? "" : `${index + 1} / ${findings.length}`;
            let message = "Browse every page to compare tables, figures, equations, and layout. No automatic completeness verdict is provided.";
            if (finding) {
                message = finding.category === "sequence" ? "This exact token sequence occurs once in each PDF and was paired one-to-one. Its position in the extracted document sequence differs; compare both locations. This does not confirm unchanged layout or meaning."
                    : finding.category === "text" ? "Text did not match here. It may have changed, moved, or been extracted in a different order; check both pages. Repeated or uncertain passages are not automatically paired."
                        : data.text_available ? "This pattern occurs more often in the corrected extracted text than in the baseline. Check the actual page before deciding it is an error."
                            : "This pattern is present in the corrected extracted text, but the baseline has no extractable text. It cannot be classified as new; inspect both PDFs manually.";
                if (finding.locations_truncated) message += " Only the first word regions are highlighted; the passage is longer.";
            }
            query("[data-comparison-description]").textContent = message;
            query("[data-comparison-map-status]").textContent = "Finding locations contain actual text only. A side without corresponding text shows a separately labeled reference page. Page linking uses matched content, not equal page numbers.";
            root.querySelectorAll("[data-comparison-step]").forEach(button => button.disabled = findings.length === 0);
            renderList(); renderPages();
            if (reveal) window.CFMContextExpansion?.reveal(query(".cfm-comparison-workspace"));
        }
        function navigate(name, page, keepFinding = false) {
            if (!Number.isInteger(page) || page < 1 || page > data[`${name}_pages`].length) {
                sides[name].input.value = state[name];
                return;
            }
            const previousPages = [state.before, state.after];
            state[name] = page;
            let matched = null;
            if (state.linked) {
                matched = (name === "before" ? data.page_map : data.inverse_map)[String(page)];
                if (matched) state[name === "before" ? "after" : "before"] = matched;
            }
            if (!keepFinding && (state.before !== previousPages[0] || state.after !== previousPages[1])) select(null);
            if (state.selected === null) query("[data-comparison-location]").textContent = `Before page ${state.before} / After page ${state.after}`;
            query("[data-comparison-map-status]").textContent = !state.linked ? "Pages are independently selected." : matched ? "Linked using matching text on these pages." : "No reliable text match for this page. The other page has not been changed.";
            renderPages();
        }
        root.querySelectorAll("[data-comparison-mode]").forEach(button => button.addEventListener("click", () => {state.mode = button.dataset.comparisonMode; renderPages();}));
        root.querySelectorAll("[data-comparison-step]").forEach(button => button.addEventListener("click", () => {
            const findings = visibleFindings();
            if (!findings.length) return;
            const index = findings.findIndex(item => item.id === state.selected);
            const direction = Number(button.dataset.comparisonStep);
            const next = index < 0 ? (direction > 0 ? 0 : findings.length - 1) : (index + direction + findings.length) % findings.length;
            select(findings[next], true);
        }));
        query("[data-comparison-category]").addEventListener("change", event => {state.category = event.target.value; select(visibleFindings()[0], true);});
        query("[data-comparison-highlights]").addEventListener("change", event => root.classList.toggle("cfm-comparison-no-highlights", !event.target.checked));
        query("[data-comparison-link]").addEventListener("change", event => {state.linked = event.target.checked; navigate("before", state.before);});
        query("[data-comparison-manual]").addEventListener("click", () => {state.mode = "pages"; select(null, true);});
        select(data.findings[0]);
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true});
    else initialize();
})();
