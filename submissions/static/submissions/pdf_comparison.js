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
        const state = {selected: null, category: "all", mode: "pages", before: 1, after: 1, linked: true, scale: "page", groups: new Set(["attention", "uncertain", "tolerated", "ignored_spacing", "wrap_hyphen"])};
        const inspection = {finding: null, mode: null, side: null, location: null, point: null, timer: null, frame: null, blocked: false, pairing: null, candidate: null};
        const inspectionPanel = query("[data-comparison-inspection]");
        const detail = window.CFMComparisonDetail?.create();
        query(".cfm-comparison-workspace").insertBefore(inspectionPanel, query(".cfm-comparison-documents"));
        const groupOf = finding => finding.category === "formatting" ? finding.kind : ["uncertain", "tolerated"].includes(finding.category) ? finding.category : "attention";
        const markKind = finding => ["formatting", "tolerated"].includes(finding.category) ? "info" : finding.category === "uncertain" ? "review" : "change";
        const sides = {};
        for (const name of ["before", "after"]) {
            const panel = query(`[data-comparison-side="${name}"]`);
            sides[name] = {
                panel, page: panel.querySelector(".cfm-comparison-page"),
                input: panel.querySelector("[data-comparison-page]"),
                text: panel.querySelector("[data-comparison-text]"),
                layer: panel.querySelector(".cfm-comparison-mark-layer"),
                findingPages: panel.querySelector("[data-comparison-finding-pages]"),
                focus: panel.querySelector("[data-comparison-focus]"),
                scroll: panel.querySelector(".cfm-comparison-scroll"),
                ready: false, source: "", failed: false,
            };
            const empty = document.createElement("div");
            empty.className = "cfm-comparison-page-empty";
            empty.dataset.comparisonPageEmpty = "";
            empty.setAttribute("role", "status"); empty.hidden = true;
            empty.textContent = "No corresponding page found. Select a page manually or turn off Link pages for manual comparison.";
            sides[name].scroll.appendChild(empty);
            sides[name].page.querySelector(":scope > img").addEventListener("error", function () {
                sides[name].ready = false;
                sides[name].failed = true;
                query("[data-comparison-image-error]").hidden = false;
                sides[name].layer.replaceChildren();
                sides[name].focus.disabled = true;
                renderInspection(); drawConnection();
            });
            sides[name].page.querySelector(":scope > img").addEventListener("load", () => {
                const side = sides[name];
                const image = side.page.querySelector(":scope > img");
                side.ready = image.getAttribute("src") === side.source && image.complete && image.naturalWidth > 0;
                side.failed = !side.ready;
                renderMarks(name); updateFocus(name); renderInspection(); drawConnection();
                query("[data-comparison-image-error]").hidden = !Object.values(sides).some(item => item.failed);
            });
            sides[name].focus.addEventListener("click", () => {
                updateFocus(name);
                if (sides[name].focus.disabled) return;
                const mark = selectedMark(name, state.selected);
                root.querySelectorAll(".cfm-comparison-mark.is-focused").forEach(item => item.classList.remove("is-focused"));
                mark.classList.add("is-focused");
                window.CFMContextExpansion?.reveal(mark);
            });
            sides[name].input.addEventListener("change", () => navigate(name, Number(sides[name].input.value)));
            sides[name].findingPages.addEventListener("change", () => navigate(name, Number(sides[name].findingPages.value), true));
            panel.querySelectorAll("[data-comparison-page-step]").forEach(button => button.addEventListener("click", () => navigate(name, state[name] + Number(button.dataset.comparisonPageStep))));
            sides[name].scroll.addEventListener("scroll", drawConnection, {passive: true});
            sides[name].page.addEventListener("pointerenter", event => hoverPage(name, event));
            sides[name].page.addEventListener("pointermove", event => hoverPage(name, event));
            sides[name].page.addEventListener("pointerleave", () => { inspection.blocked = false; clearTimeout(inspection.timer); inspection.candidate = null; });
            sides[name].page.addEventListener("click", event => {
                if (event.ctrlKey || event.target.closest(".cfm-comparison-mark") || !sides[name].ready) return;
                openInspection(null, name, null, "touch", event);
            });
        }
        function renderCoverage() {
            const complete = data.coverage_status === "complete";
            const limited = data.coverage_status === "limited";
            const notice = query("[data-comparison-coverage]");
            notice.classList.toggle("alert-warning", !complete);
            notice.classList.toggle("alert-info", complete);
            query("[data-comparison-coverage-heading]").textContent = complete ? "Engine coverage: complete within supported scope"
                : limited ? "Engine coverage: limited" : "Engine coverage not reported";
            query("[data-comparison-coverage-description]").textContent = complete
                ? "The engine reports its supported checks completed, not that the PDFs are unchanged."
                : limited ? "A result is available, but some evidence could not be fully checked. Unknown or unsupported regions are not treated as unchanged."
                    : "No engine coverage status is available for this result. Review both PDFs manually.";
            const reasons = query("[data-comparison-coverage-reasons]");
            reasons.replaceChildren();
            const coverageReasons = [...(data.coverage_reasons || [])];
            // Older payloads have no coverage reasons; keep their limitations in the same notice.
            if (!coverageReasons.length) {
                if (!data.text_available) coverageReasons.push("One or both PDFs have no extractable text. Inspect both PDFs manually.");
                if (data.uncertain_count) coverageReasons.push("Some OCR ranges have no reliable counterpart; unknown regions are not unchanged.");
                if (data.pairing_limited) coverageReasons.push("Text alignment reached its processing limit. Remaining unmatched ranges need review.");
                if (data.truncated) coverageReasons.push("Not all findings are displayed. Review the full PDFs.");
            }
            for (const reason of coverageReasons) {
                const item = document.createElement("li");
                item.textContent = reason;
                reasons.appendChild(item);
            }
            reasons.hidden = !reasons.childElementCount;
            const details = query("[data-comparison-coverage-details]");
            if (details) {
                details.hidden = reasons.hidden;
                details.open = false;
            }
            const counts = query("[data-comparison-evidence-counts]");
            counts.replaceChildren();
            // Preserve engine count names, including side-specific groups; never infer a score.
            function appendCounts(values, path = []) {
                if (!values || typeof values !== "object" || Array.isArray(values)) return;
                for (const [key, value] of Object.entries(values)) {
                    const label = [...path, key.replace(/_/g, " ")];
                    if (Number.isSafeInteger(value) && value >= 0) {
                        const term = document.createElement("dt");
                        const count = document.createElement("dd");
                        term.textContent = label.join(" / ");
                        count.textContent = String(value);
                        counts.append(term, count);
                    } else if (value && typeof value === "object") {
                        appendCounts(value, label);
                    }
                }
            }
            appendCounts(data.evidence_summary);
            query("[data-comparison-evidence-summary]").hidden = !counts.childElementCount;
            query("[data-comparison-rules]").hidden = !data.visual_exception_table?.length;
            query("[data-comparison-rules-content]").textContent = data.visual_exception_table?.length
                ? JSON.stringify({profile: data.visual_profile, rules: data.visual_exception_table}, null, 2) : "";
        }
        function updateFocus(name) {
            const side = sides[name];
            const image = side.page.querySelector(":scope > img");
            side.focus.disabled = state.mode !== "pages" || !selectedMark(name, state.selected)
                || !side.ready || !image.complete || !image.naturalWidth || root.classList.contains("cfm-comparison-no-highlights");
        }
        function visibleFindings() {
            return data.findings.filter(matchesFilter);
        }
        function matchesFilter(finding) {
            return state.groups.has(groupOf(finding)) && (state.category === "all" || finding.category === state.category || finding.kind === state.category);
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
                    const group = groupOf(finding);
                    list.querySelector(`[data-finding-group="${group}"]`).appendChild(button);
                }
            }
            for (const button of list.querySelectorAll("[data-finding-id]")) {
                const finding = data.findings.find(item => String(item.id) === button.dataset.findingId);
                button.hidden = !matchesFilter(finding);
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
            const legend = query("[data-comparison-mark-legend]");
            if (legend) legend.hidden = state.mode !== "pages";
            for (const name of ["before", "after"]) {
                const side = sides[name];
                side.input.value = state[name] || "";
                side.panel.querySelector("[data-comparison-page-empty]").hidden = state[name] !== null;
                if (state[name] === null) {
                    side.ready = false; side.source = ""; side.page.hidden = true; side.text.hidden = true;
                    side.layer.replaceChildren(); side.focus.disabled = true;
                    side.panel.querySelector(".cfm-comparison-passage").hidden = true;
                    side.panel.querySelectorAll("[data-comparison-page-step]").forEach(button => button.disabled = true);
                    continue;
                }
                side.panel.querySelector(".cfm-comparison-page-controls").hidden = state.mode !== "pages";
                const pages = data[`${name}_pages`];
                const page = pages[state[name] - 1];
                const actualPages = finding?.[`${name}_pages`] || [];
                const status = side.panel.querySelector("[data-comparison-side-position]");
                side.panel.querySelector(".cfm-comparison-passage").hidden = !finding;
                status.textContent = !finding ? "" : !actualPages.length
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
                if (!page) continue;
                side.page.style.aspectRatio = `${page.width} / ${page.height}`;
                side.scroll.hidden = state.mode !== "pages";
                side.page.hidden = state.mode !== "pages";
                side.text.hidden = state.mode !== "text";
                const text = finding?.[`${name}_text`];
                side.text.textContent = text || (finding ? "No matched text on this side. Inspect the page image." : "Select a text finding, or use Page comparison to inspect the full PDFs.");
                if (text) {
                    const changed = document.createElement("span");
                    changed.className = "cfm-comparison-text-change";
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
                if (state.mode === "pages") {
                    const url = data.page_urls[name].replace("/0.png", `/${state[name]}.png`);
                    if (side.page.dataset.comparisonLoadedUrl !== url) {
                        side.page.dataset.comparisonLoadedUrl = url;
                        side.source = url;
                        side.ready = false;
                        side.failed = false;
                        side.layer.replaceChildren();
                        window.CFMImageLoader.load(side.page, url);
                    }
                }
                renderMarks(name);
                side.panel.querySelectorAll("[data-comparison-page-step]").forEach(button => {
                    const value = state[name] + Number(button.dataset.comparisonPageStep);
                    button.disabled = value < 1 || value > pages.length;
                });
                updateFocus(name);
            }
            root.querySelectorAll("[data-comparison-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.comparisonMode === state.mode)));
            query("[data-comparison-link]").disabled = state.mode !== "pages";
            resizePages(); drawConnection(); renderInspection();
        }
        function select(finding, reveal = false) {
            state.selected = finding?.id ?? null;
            root.classList.toggle("cfm-comparison-uncertain-selected", finding?.category === "uncertain");
            root.classList.toggle("cfm-comparison-info-selected", ["formatting", "tolerated"].includes(finding?.category));
            if (finding) {
                state.before = finding.before_page || finding.before_reference_page || null;
                state.after = finding.after_page || finding.after_reference_page || null;
            }
            query("[data-comparison-heading]").textContent = finding?.label || "Document comparison";
            query("[data-comparison-location]").textContent = finding ? `Before ${pageLabel(finding, "before")} / After ${pageLabel(finding, "after")}` : `Before ${state.before === null ? "no corresponding page" : "page " + state.before} / After ${state.after === null ? "no corresponding page" : "page " + state.after}`;
            updatePosition();
            renderList(); renderPages();
            renderEvidence(finding);
            if (finding) openInspection(finding, null, null, "manual");
            else resetInspection();
            if (reveal) {
                for (const name of ["before", "after"]) {
                    const mark = selectedMark(name, state.selected);
                    if (mark) window.CFMContextExpansion?.reveal(mark);
                }
            }
        }
        function renderEvidence(finding) {
            query("[data-comparison-inspection-title]").textContent = finding?.label || "Page magnification";
            let message = "Manual comparison of the saved PDFs.";
            if (finding) {
                message = finding.category === "uncertain" ? "No reliable local correspondence. Inspect both ranges; this is not an unchanged region or a verified character match."
                    : finding.category === "tolerated" ? "Tolerated by the visual exception policy, not proof that the original PDF content is equal. Check the page pixels and rule evidence."
                    : finding.category === "formatting" ? "Formatting information is separate from content changes and uncertainty warnings. Check the finding evidence for the applied rule."
                    : finding.category === "text" ? "Recognized text differs. Confirm against the PDF pixels; recognition errors can also cause differences."
                        : "Possible reference error in recognized text. Confirm against the printed page; OCR can be wrong.";
                if (finding.locations_truncated) message += " Only the first word regions are highlighted; the passage is longer.";
            }
            query("[data-comparison-description]").textContent = message;
            query("[data-comparison-description]").hidden = !finding;
            const evidence = query("[data-comparison-evidence]");
            const reasons = query("[data-comparison-evidence-reason]");
            reasons.replaceChildren();
            for (const reason of [...(finding?.issues || []),
                ...["rule_id", "rule_label", "rule_risk"].filter(key => finding?.[key] !== undefined).map(key => `${key}: ${finding[key]}`)]) {
                const item = document.createElement("li");
                item.textContent = reason;
                reasons.appendChild(item);
            }
            evidence.hidden = inspectionPanel.dataset.view !== "evidence";
            evidence.open = true;
            for (const side of ["before", "after"]) {
                let raw = (finding?.[`${side}_raw`] || "No recognized passage.").replace(/\u200b/g, "[U+200B]");
                if (finding?.[`${side}_ocr`]?.length) raw += `\n\nOCR lines / scores\n${JSON.stringify(finding[`${side}_ocr`], null, 2)}`;
                query(`[data-comparison-raw-${side}]`).textContent = raw;
            }
        }
        function navigate(name, page, keepFinding = false) {
            if (!Number.isInteger(page) || page < 1 || page > data[`${name}_pages`].length) {
                sides[name].input.value = state[name];
                return;
            }
            state.driver = name;
            resetInspection();
            state[name] = page;
            let matched = null;
            if (state.linked && !keepFinding) {
                matched = (name === "before" ? data.page_map : data.inverse_map)[String(page)];
                const other = name === "before" ? "after" : "before";
                if (Number.isInteger(matched) && matched >= 1 && matched <= data[`${other}_pages`].length) state[other] = matched;
                else { matched = null; state[other] = null; }
            }
            if (!keepFinding) select(null);
            if (state.selected === null) query("[data-comparison-location]").textContent = `Before ${state.before === null ? "no corresponding page" : "page " + state.before} / After ${state.after === null ? "no corresponding page" : "page " + state.after}`;
            query("[data-comparison-map-status]").textContent = !state.linked ? "Manual comparison: pages are independently selected." : matched ? "Linked using matching text on these pages." : "No corresponding page found. No unrelated page is displayed.";
            renderPages();
            if (keepFinding && state.selected !== null) {
                const finding = data.findings.find(item => item.id === state.selected);
                renderEvidence(finding); openInspection(finding, null, null, "manual");
            }
        }
        function locations(finding, name) {
            if (!finding[`${name}_pages`].includes(state[name])) return [];
            return (finding[`${name}_locations`] || []).filter(location => location.page === state[name]
                && Array.isArray(location.box) && location.box.length === 4 && location.box.every(value => Number.isFinite(value) && value >= 0 && value <= 1)
                && location.box[2] > location.box[0] && location.box[3] > location.box[1]);
        }
        function selectedMark(name, id) {
            if (id === null || id === undefined) return null;
            const marks = [...sides[name].layer.children].filter(mark => mark.dataset.findingId === String(id));
            return marks.find(mark => inspection.side === name && mark.location === inspection.location) || marks[0] || null;
        }
        function renderMarks(name) {
            const side = sides[name];
            side.layer.replaceChildren();
            if (!side.ready) return;
            for (const finding of visibleFindings()) {
                for (const [index, location] of locations(finding, name).entries()) {
                    const mark = document.createElement("button");
                    mark.type = "button";
                    mark.className = "cfm-comparison-mark";
                    mark.dataset.findingId = finding.id;
                    mark.dataset.kind = markKind(finding);
                    mark.dataset.first = String(index === 0);
                    mark.location = location;
                    mark.setAttribute("aria-label", `${finding.label}: ${name}, page ${state[name]}. ${(finding[`${name}_text`] || "Review region").slice(0, 120)}`);
                    const [x0, y0, x1, y1] = location.box;
                    Object.assign(mark.style, {left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`});
                    mark.addEventListener("focus", () => {
                        if (mark.matches(":focus-visible")) openInspection(finding, name, location, "keyboard");
                    });
                    mark.addEventListener("click", event => {
                        if (event.ctrlKey) return;
                        state.selected = finding.id;
                        renderEvidence(finding); renderList();
                        openInspection(finding, name, location, event.detail === 0 ? "keyboard" : "touch", event);
                    });
                    side.layer.appendChild(mark);
                }
            }
            highlightMarks();
        }
        function pointerPoint(name, event) {
            const rect = sides[name].page.getBoundingClientRect();
            if (!rect.width || !rect.height || !Number.isFinite(event?.clientX) || !Number.isFinite(event?.clientY)) return null;
            return {x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))};
        }
        function hoverPage(name, event) {
            if (inspection.collapsed || state.mode !== "pages" || !sides[name].ready || event.pointerType === "touch" || event.ctrlKey
                || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
            const point = pointerPoint(name, event);
            if (!point) { closeInspection(); return; }
            const mark = event.target.closest(".cfm-comparison-mark");
            let finding = mark ? data.findings.find(item => String(item.id) === mark.dataset.findingId) : null;
            const location = mark?.location || null;
            if (inspection.blocked === (location || sides[name].page)) return;
            inspection.blocked = false;
            if (inspection.point === null) openInspection(finding, name, location, "hover", event);
            if (finding && inspection.finding && finding !== inspection.finding) {
                if (inspection.candidate !== finding) {
                    clearTimeout(inspection.timer); inspection.candidate = finding;
                    inspection.timer = setTimeout(() => {
                        inspection.candidate = null;
                        const currentPoint = inspection.point;
                        openInspection(finding, name, location, "hover", event);
                        inspection.point = currentPoint; renderInspection();
                    }, 300);
                }
            } else {
                clearTimeout(inspection.timer); inspection.candidate = null;
                if (finding && (inspection.finding !== finding || inspection.side !== name)) {
                    openInspection(finding, name, location, "hover", event);
                }
            }
            if (inspectionPanel.hidden) return;
            inspection.side = name; inspection.mode = "hover";
            inspection.point = point;
            if (inspection.frame === null) inspection.frame = requestAnimationFrame(() => {
                inspection.frame = null;
                renderCrops();
            });
        }
        function openInspection(finding, name, location, mode, event) {
            clearTimeout(inspection.timer);
            cancelAnimationFrame(inspection.frame); inspection.frame = null;
            const mark = name && finding ? selectedMark(name, finding.id)
                : finding ? selectedMark("before", finding.id) || selectedMark("after", finding.id) : null;
            const box = location?.box || mark?.location.box;
            const point = name && event && mode !== "keyboard" ? pointerPoint(name, event)
                : ["hover", "touch"].includes(mode) && inspection.side === name && inspection.location === location ? inspection.point : null;
            inspection.point = point || (box ? {x: (box[0] + box[2]) / 2, y: (box[1] + box[3]) / 2} : {x: .5, y: .5});
            Object.assign(inspection, {finding, side: name, location, mode});
            inspection.pairing = null;
            if (finding && name && location && finding.category !== "uncertain") {
                const other = name === "before" ? "after" : "before";
                const here = locations(finding, name), there = locations(finding, other);
                // Only an unambiguous region pair is a usable magnification anchor.
                if (here.length === 1 && there.length === 1) {
                    const center = box => ({x:(box[0]+box[2])/2,y:(box[1]+box[3])/2});
                    inspection.pairing = {side:name, from:center(location.box), to:center(there[0].box)};
                }
            }
            const overlaps = query("[data-comparison-overlap]");
            overlaps.replaceChildren();
            const candidates = name && location ? visibleFindings().filter(item => locations(item, name).some(({box}) =>
                box[0] <= location.box[2] && box[2] >= location.box[0] && box[1] <= location.box[3] && box[3] >= location.box[1])) : finding ? [finding] : [];
            for (const item of candidates) {
                const option = document.createElement("option");
                option.value = item.id;
                option.textContent = `${item.label}: ${(item[`${name || "before"}_text`] || item.after_text || "Review region").slice(0, 80)}`;
                overlaps.appendChild(option);
            }
            overlaps.value = finding?.id ?? "";
            query("[data-comparison-overlap-label]").hidden = candidates.length < 2;
            inspectionPanel.hidden = false;
            inspection.collapsed = false;
            query("[data-comparison-inspector-toggle]").setAttribute("aria-expanded", "true");
            inspectionPanel.dataset.interaction = mode;
            if (state.selected === null && finding) renderEvidence(finding);
            renderInspection(); highlightMarks(); drawConnection();
        }
        function highlightMarks() {
            const id = inspection.finding?.id ?? state.selected;
            for (const side of Object.values(sides)) {
                for (const mark of side.layer.children) mark.classList.toggle("is-selected", String(id) === mark.dataset.findingId);
            }
        }
        function renderInspection() {
            if (inspectionPanel.hidden) return;
            query("[data-comparison-pair-status]").textContent = !inspection.point ? "Move over a displayed page to inspect it." : inspection.pairing && query("[data-inspection-position]").value !== "relative"
                ? "Matched region anchor · pointer offset"
                : "Relative position · not a text correspondence";
            renderCrops();
        }
        function renderCrops() {
            const finding = inspection.finding;
            if (inspectionPanel.hidden || !inspection.point) return;
            for (const name of ["before", "after"]) {
                const side = sides[name], image = side.page.querySelector(":scope > img");
                const canvas = query(`[data-comparison-crop="${name}"]`), context = canvas.getContext("2d");
                context.clearRect(0, 0, canvas.width, canvas.height);
                delete canvas.dataset.source; delete canvas.dataset.findingId;
                canvas.dataset.state = "empty";
                canvas.dataset.resolution = "preview";
                const status = query(`[data-comparison-crop-status="${name}"]`);
                const label = query(`[data-comparison-crop-page="${name}"]`);
                label.textContent = state[name] === null ? " / no corresponding page" : ` / page ${state[name]}`;
                if (state[name] === null) {
                    detail?.clear(name);
                    canvas.hidden = true; status.textContent = "No corresponding page. No unrelated page is substituted."; continue;
                }
                if (!side.ready || !image.complete || !image.naturalWidth || !image.naturalHeight || image.getAttribute("src") !== side.source) {
                    detail?.clear(name);
                    canvas.hidden = true;
                    status.textContent = "Page image unavailable or loading. No other page is substituted.";
                    continue;
                }
                // Share a page-relative center, never a finding's counterpart.
                // Each image keeps its own aspect ratio and actual source pixels.
                let point = inspection.point;
                const pair = inspection.pairing;
                if (pair && query("[data-inspection-position]").value !== "relative") {
                    const reverse = inspection.side !== pair.side;
                    const from = reverse ? pair.to : pair.from, to = reverse ? pair.from : pair.to;
                    if (name !== inspection.side) point = {x:to.x+point.x-from.x,y:to.y+point.y-from.y};
                }
                const width = image.naturalWidth, height = image.naturalHeight;
                canvas.hidden = false;
                // Match the visible crop's shape, including short viewports, without stretching pixels.
                const cssWidth = canvas.clientWidth, cssHeight = canvas.clientHeight || cssWidth * canvas.height / canvas.width;
                const requestedDensity = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
                const pixelWidth = Math.max(1, Math.min(3072, Math.round(cssWidth * requestedDensity)));
                const pixelHeight = Math.max(1, Math.round(pixelWidth * cssHeight / Math.max(1, cssWidth)));
                if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
                if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
                const displayedWidth = side.page.getBoundingClientRect().width;
                if (!displayedWidth || !canvas.clientWidth) {
                    canvas.hidden = true;
                    status.textContent = "Page preview is not visible.";
                    continue;
                }
                const density = pixelWidth / cssWidth;
                const pdfPage = data[`${name}_pages`][state[name] - 1];
                // Shrinking Fit page must not shrink the lens's readable text along with it.
                // Use PDF points for the floor, independently of preview resolution or DPR.
                const minCssPixelsPerPoint = 1.5;
                const cssScale = Math.max(2 * displayedWidth / width, minCssPixelsPerPoint * pdfPage.width / width);
                const padding = 8 * density;
                const scale = cssScale * density;
                const cw = Math.min(width, Math.max(1, canvas.width - padding * 2) / scale);
                const ch = Math.min(height, Math.max(1, canvas.height - padding * 2) / scale);
                const sx = Math.max(0, Math.min(width - cw, point.x * width - cw / 2));
                const sy = Math.max(0, Math.min(height - ch, point.y * height - ch / 2));
                const dx = (canvas.width - cw * scale) / 2, dy = (canvas.height - ch * scale) / 2;
                const draw = (pixels, x, y, w, h) => {
                    context.clearRect(0, 0, canvas.width, canvas.height);
                    context.drawImage(pixels, x, y, w, h, dx, dy, cw * scale, ch * scale);
                    context.save(); context.beginPath(); context.rect(dx, dy, cw * scale, ch * scale); context.clip();
                    if (finding && !root.classList.contains("cfm-comparison-no-highlights")) {
                        context.strokeStyle = markKind(finding) === "change" ? "#b91e30" : markKind(finding) === "review" ? "#a26100" : "#71869a";
                        context.lineWidth = 2 * density;
                        for (const {box: [x0, y0, x1, y1]} of locations(finding, name)) {
                            context.strokeRect(dx + (x0 * width - sx) * scale, dy + (y0 * height - sy) * scale, (x1 - x0) * width * scale, (y1 - y0) * height * scale);
                        }
                    }
                    context.restore();
                };
                draw(image, sx, sy, cw, ch);
                const edge = point.x < 0 || point.x > 1 || point.y < 0 || point.y > 1;
                status.textContent = edge ? "Page edge reached; position is constrained to this page." : "";
                canvas.dataset.state = "ready";
                canvas.dataset.source = side.source;
                const base = data.detail_urls?.[name];
                if (detail && base && inspectionPanel.dataset.view !== "evidence") {
                    const box = [sx / width, sy / height, Math.min(1, (sx + cw) / width), Math.min(1, (sy + ch) / height)];
                    const url = base.replace("/0.png", `/${state[name]}.png`) + `?box=${box.join(",")}&width=${Math.max(1, Math.floor(cw * scale))}&height=${Math.max(1, Math.floor(ch * scale))}`;
                    canvas.dataset.detailKey = url;
                    const current = () => !inspectionPanel.hidden && inspectionPanel.dataset.view !== "evidence" && canvas.dataset.detailKey === url && canvas.dataset.source === side.source;
                    detail.request(name, url, bitmap => {
                        if (!current()) return;
                        draw(bitmap, 0, 0, bitmap.width, bitmap.height);
                        canvas.dataset.resolution = "detail";
                        status.textContent = edge ? "High detail · page edge" : "High detail";
                    }, invalid => {
                        if (!current()) return;
                        if (invalid) {
                            canvas.hidden = true;
                            status.textContent = "Comparison changed or expired. Rebuild comparison.";
                        } else status.textContent = "Preview only; high detail unavailable.";
                    });
                }
            }
        }
        function drawConnection() {
            const svg = query("[data-comparison-connection-layer]");
            svg.replaceChildren();
            if (state.mode !== "pages" || !query("[data-comparison-connections]").checked || root.classList.contains("cfm-comparison-no-highlights")) return;
            const finding = inspection.finding || data.findings.find(item => item.id === state.selected);
            if (!finding) return;
            const a = selectedMark("before", finding.id), b = selectedMark("after", finding.id);
            if (!a || !b || !sides.before.ready || !sides.after.ready) return;
            const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
            for (const [name, rect] of [["before", ar], ["after", br]]) {
                const view = sides[name].scroll.getBoundingClientRect();
                if (rect.top < view.top || rect.bottom > view.bottom || rect.left < view.left || rect.right > view.right) return;
            }
            const bounds = query(".cfm-comparison-documents").getBoundingClientRect();
            const horizontal = sides.before.panel.getBoundingClientRect().right <= sides.after.panel.getBoundingClientRect().left;
            const x1 = (horizontal ? ar.right : ar.left + ar.width / 2) - bounds.left;
            const y1 = (horizontal ? ar.top + ar.height / 2 : ar.bottom) - bounds.top;
            const x2 = (horizontal ? br.left : br.left + br.width / 2) - bounds.left;
            const y2 = (horizontal ? br.top + br.height / 2 : br.top) - bounds.top;
            const middle = horizontal ? (x1 + x2) / 2 : (y1 + y2) / 2;
            const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.setAttribute("d", horizontal ? `M${x1} ${y1} C${middle} ${y1},${middle} ${y2},${x2} ${y2}` : `M${x1} ${y1} C${x1} ${middle},${x2} ${middle},${x2} ${y2}`);
            path.dataset.kind = markKind(finding);
            svg.appendChild(path);
        }
        function resizePages() {
            for (const name of ["before", "after"]) {
                const side = sides[name], page = data[`${name}_pages`][state[name] - 1];
                const height = Math.max(160, window.innerHeight - side.scroll.getBoundingClientRect().top - 20);
                side.scroll.style.maxHeight = `${height}px`;
                if (!page) continue;
                const available = side.scroll.clientWidth;
                side.page.style.width = `${state.scale === "page" ? Math.min(available, height * page.width / page.height) : available * Number(state.scale)}px`;
            }
            drawConnection(); renderInspection();
        }
        function closeInspection() {
            detail?.dispose();
            clearTimeout(inspection.timer);
            cancelAnimationFrame(inspection.frame); inspection.frame = null;
            Object.assign(inspection, {finding: null, mode: null, side: null, location: null, point: null});
            inspectionPanel.hidden = true;
            query("[data-comparison-inspector-toggle]").setAttribute("aria-expanded", "false");
            for (const name of ["before", "after"]) {
                const canvas = query(`[data-comparison-crop="${name}"]`);
                canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
                delete canvas.dataset.source; delete canvas.dataset.findingId; canvas.dataset.state = "empty";
                canvas.hidden = true;
            }
            highlightMarks(); drawConnection();
        }
        function resetInspection() {
            const wasHidden = inspectionPanel.hidden;
            closeInspection(); inspection.pairing = null; inspection.candidate = null;
            inspectionPanel.hidden = wasHidden;
            query("[data-comparison-inspector-toggle]").setAttribute("aria-expanded", String(!wasHidden));
            for (const name of ["before", "after"]) query(`[data-comparison-crop-status="${name}"]`).textContent = "Move over a displayed page to inspect it.";
            renderEvidence(null);
        }
        inspectionPanel.addEventListener("pointerenter", () => { clearTimeout(inspection.timer); inspection.candidate = null; });
        query("[data-inspection-position]").addEventListener("change", renderInspection);
        root.querySelectorAll("[data-inspection-view]").forEach(button => button.addEventListener("click", () => {
            const evidence = button.dataset.inspectionView === "evidence";
            if (evidence) detail?.dispose();
            inspectionPanel.dataset.view = evidence ? "evidence" : "magnify";
            query(".cfm-comparison-crops").hidden = evidence;
            query("[data-comparison-evidence]").hidden = !evidence;
            root.querySelectorAll("[data-inspection-view]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
            renderInspection();
        }));
        query("[data-comparison-inspector-toggle]").addEventListener("click", event => {
            inspectionPanel.hidden = !inspectionPanel.hidden;
            inspection.collapsed = inspectionPanel.hidden;
            inspection.blocked = inspectionPanel.hidden ? inspection.location || (inspection.side && sides[inspection.side].page) : false;
            event.currentTarget.setAttribute("aria-expanded", String(!inspectionPanel.hidden));
            renderInspection();
        });
        function dismissInspection() {
            inspection.blocked = inspection.mode === "hover" ? inspection.location || sides[inspection.side].page : null;
            closeInspection();
        }
        query("[data-comparison-close]").addEventListener("click", () => { inspection.collapsed = true; dismissInspection(); });
        query("[data-comparison-overlap]").addEventListener("change", event => {
            const finding = data.findings.find(item => String(item.id) === String(event.target.value));
            openInspection(finding, inspection.side, inspection.location, inspection.mode);
        });
        document.addEventListener("keydown", event => {
            if (inspectionPanel.hidden) return;
            if (event.key === "Escape" || event.key === "Control") dismissInspection();
        });
        root.addEventListener("focusout", event => {
            if (inspection.mode === "keyboard" && !inspectionPanel.contains(event.relatedTarget) && !event.relatedTarget?.closest(".cfm-comparison-mark")) closeInspection();
        });
        window.addEventListener("resize", resizePages);
        window.addEventListener("pagehide", () => detail?.dispose());
        // Layout changes only resize the document viewport; pointer motion never does.
        if (window.ResizeObserver) {
            const layoutObserver = new window.ResizeObserver(resizePages);
            layoutObserver.observe(inspectionPanel);
            layoutObserver.observe(query(".cfm-comparison-selection"));
        }
        root.querySelectorAll("[data-comparison-mode]").forEach(button => button.addEventListener("click", () => {closeInspection(); state.mode = button.dataset.comparisonMode; renderPages();}));
        root.querySelectorAll("[data-comparison-step]").forEach(button => button.addEventListener("click", () => {
            const findings = visibleFindings();
            if (!findings.length) return;
            const index = findings.findIndex(item => item.id === state.selected);
            const direction = Number(button.dataset.comparisonStep);
            const next = index < 0 ? (direction > 0 ? 0 : findings.length - 1) : (index + direction + findings.length) % findings.length;
            select(findings[next], true);
        }));
        function filterChanged() {
            closeInspection();
            if (!visibleFindings().some(item => item.id === state.selected)) { select(null); return; }
            renderList(); renderPages(); updatePosition();
        }
        query("[data-comparison-category]").addEventListener("change", event => {state.category = event.target.value; filterChanged();});
        root.querySelectorAll("[data-comparison-group-filter]").forEach(input => input.addEventListener("change", () => {
            if (input.checked) state.groups.add(input.dataset.comparisonGroupFilter);
            else state.groups.delete(input.dataset.comparisonGroupFilter);
            filterChanged();
        }));
        query("[data-comparison-highlights]").addEventListener("change", event => {
            root.classList.toggle("cfm-comparison-no-highlights", !event.target.checked);
            closeInspection();
            for (const name of ["before", "after"]) updateFocus(name);
        });
        query("[data-comparison-link]").addEventListener("change", event => {
            state.linked = event.target.checked;
            if (state.linked) {
                const driver = state.driver || (state.before === null ? "after" : "before");
                navigate(driver, state[driver] || 1);
            }
            else { resetInspection(); select(null); query("[data-comparison-map-status]").textContent = "Manual comparison: pages are independently selected."; }
        });
        query("[data-comparison-connections]").addEventListener("change", drawConnection);
        query("[data-comparison-scale]").addEventListener("change", event => {state.scale = event.target.value; closeInspection(); resizePages();});
        query("[data-comparison-manual]").addEventListener("click", () => {state.mode = "pages"; select(null, true);});
        function updatePosition() {
            const findings = visibleFindings();
            const index = findings.findIndex(item => item.id === state.selected);
            query("[data-comparison-position]").textContent = index < 0 ? `${findings.length} displayed` : `${index + 1} / ${findings.length}`;
            root.querySelectorAll("[data-comparison-step]").forEach(button => button.disabled = findings.length === 0);
        }
        renderCoverage();
        state.after = Number.isInteger(data.page_map["1"]) ? data.page_map["1"] : null;
        select(null);
        inspectionPanel.hidden = false;
        resizePages();
        query("[data-comparison-inspector-toggle]").setAttribute("aria-expanded", "true");
        updatePosition();
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true});
    else initialize();
})();
