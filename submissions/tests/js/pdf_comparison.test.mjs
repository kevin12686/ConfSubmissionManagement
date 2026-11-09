import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script = readFileSync(new URL("../../static/submissions/pdf_comparison.js", import.meta.url), "utf8");

// Small DOM double, following the existing browser-script tests without npm dependencies.
class Element {
    constructor(tag = "div", attributes = {}) {
        this.tagName = tag;
        this.attributes = attributes;
        this.children = [];
        this.listeners = new Map();
        this.style = {};
        this.hidden = false;
        this.disabled = false;
        this.className = attributes.class || "";
        this.dataset = new Proxy({}, {set: (object, key, value) => { object[key] = String(value); return true; }});
        for (const [key, value] of Object.entries(attributes)) {
            if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
        }
        this.classList = {
            contains: name => this.className.split(" ").includes(name),
            toggle: (name, force = !this.classList.contains(name)) => {
                this.className = this.className.split(" ").filter(item => item && item !== name).concat(force ? [name] : []).join(" ");
                return force;
            },
            add: name => this.classList.toggle(name, true),
            remove: name => this.classList.toggle(name, false),
        };
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return (this.text || "") + this.children.map(child => child.textContent).join(""); }
    get firstElementChild() { return this.children[0] || null; }
    get childElementCount() { return this.children.length; }
    append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { this.text = ""; this.children = []; this.append(...children); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(type, handler) {
        this.listeners.set(type, [...(this.listeners.get(type) || []), handler]);
    }
    fire(type, target = this) { for (const handler of this.listeners.get(type) || []) handler.call(this, {target}); }
    matches(selector) {
        if (selector.endsWith(":not([hidden])")) return !this.hidden && this.matches(selector.replace(":not([hidden])", ""));
        if (selector.startsWith(".")) return selector.slice(1).split(".").every(name => this.classList.contains(name));
        const attribute = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
        if (attribute) {
            const [, key, value] = attribute;
            const actual = key.startsWith("data-") ? this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] : this.attributes[key];
            return actual !== undefined && (value === undefined || actual === value);
        }
        return this.tagName === selector;
    }
    querySelectorAll(selector) {
        if (selector.startsWith(":scope > ")) return this.children.filter(child => child.matches(selector.slice(9)));
        return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function finding(overrides = {}) {
    return {id: 0, category: "text", kind: "changed", label: "Changed text",
        before_pages: [1], after_pages: [2], before_page: 1, after_page: 2,
        before_text: "old", after_text: "new", before_raw: "old", after_raw: "new",
        before_locations: [{page: 1, box: [.1, .2, .2, .3]}],
        after_locations: [{page: 2, box: [.1, .2, .2, .3]}], issues: [], ...overrides};
}

function setup(overrides = {}, {legacyTemplate = false} = {}) {
    const root = new Element("div", {"data-pdf-comparison": ""});
    const add = (parent, key, value = "", tag = "div") => parent.appendChild(new Element(tag, {[key]: value}));
    for (const key of ["coverage", "coverage-heading", "coverage-description", "coverage-reasons", "coverage-details", "evidence-summary",
        "evidence-counts", "image-error", "empty", "heading", "location", "position", "description", "evidence",
        "evidence-reason", "raw-before", "raw-after", "map-status", "category", "highlights", "link", "manual", "mark-legend"]) {
        if (legacyTemplate && ["coverage-details", "mark-legend"].includes(key)) continue;
        add(root, `data-comparison-${key}`);
    }
    add(root, "class", "cfm-comparison-workspace");
    const list = add(root, "data-comparison-findings");
    for (const name of ["attention", "uncertain", "formatting"]) {
        const group = add(list, "data-comparison-group", name);
        add(group, "data-finding-group-count", name);
        add(group, "data-finding-group", name);
    }
    for (const name of ["pages", "text"]) add(root, "data-comparison-mode", name, "button");
    for (const direction of [-1, 1]) add(root, "data-comparison-step", String(direction), "button");
    for (const name of ["before", "after"]) {
        const panel = add(root, "data-comparison-side", name);
        for (const key of ["page", "text", "finding-pages", "side-position", "focus"]) add(panel, `data-comparison-${key}`);
        add(panel, "class", "cfm-comparison-page-controls");
        for (const direction of [-1, 1]) add(panel, "data-comparison-page-step", String(direction), "button");
        const page = add(panel, "class", "cfm-comparison-page");
        page.appendChild(new Element("img"));
        add(page, "class", "cfm-comparison-mark-layer");
    }
    const data = {coverage_status: "complete", coverage_reasons: [], evidence_summary: {},
        findings: [finding()], text_available: true,
        before_pages: [{width: 595, height: 842}, {width: 595, height: 842}],
        after_pages: [{width: 595, height: 842}, {width: 595, height: 842}, {width: 595, height: 842}],
        page_urls: {before: "/before/0.png", after: "/after/0.png"}, page_map: {1: 2, 2: 3}, inverse_map: {2: 1, 3: 2},
        ...overrides};
    const loads = [], reveals = [];
    const q = selector => root.querySelector(selector);
    runInNewContext(script, {
        document: {readyState: "complete", querySelector: selector => selector === "[data-pdf-comparison]" ? root : null,
            getElementById: () => ({textContent: JSON.stringify(data)}), createElement: tag => new Element(tag)},
        window: {CFMImageLoader: {load(page, url) {
            const image = page.querySelector("img");
            image.complete = false;
            image.naturalWidth = 0;
            loads.push(url);
        }}, CFMContextExpansion: {reveal: target => reveals.push(target)}},
    });
    return {root, q, loads, reveals, data,
        side: name => q(`[data-comparison-side="${name}"]`),
        selectCategory(category) { const input = q("[data-comparison-category]"); input.value = category; input.fire("change"); },
        loaded(name) { const image = this.side(name).querySelector("img"); image.complete = true; image.naturalWidth = 1190; image.fire("load"); },
    };
}

test("complete coverage is scoped and counts remain engine-named, not safety scores", () => {
    const page = setup({evidence_summary: {before: {exact_glyphs: 4, unresolved: 0}, after: {exact_glyphs: 5}}});
    assert.match(page.q("[data-comparison-coverage-heading]").textContent, /complete within supported scope/);
    assert.match(page.q("[data-comparison-coverage-description]").textContent, /not that the PDFs are unchanged/);
    assert.equal(page.q("[data-comparison-coverage]").classList.contains("alert-info"), true);
    assert.equal(page.q("[data-comparison-coverage-details]").hidden, true);
    assert.deepEqual(page.q("[data-comparison-evidence-counts]").children.map(item => item.textContent),
        ["before / exact glyphs", "4", "before / unresolved", "0", "after / exact glyphs", "5"]);
});

test("limited coverage remains visible with no findings and reasons are literal text", () => {
    const page = setup({findings: [], coverage_status: "limited", coverage_reasons: ["Unsupported formula", "<img src=x onerror=alert(1)>"]});
    assert.match(page.q("[data-comparison-coverage-description]").textContent, /A result is available/);
    assert.equal(page.q("[data-comparison-empty]").hidden, false);
    const reasons = page.q("[data-comparison-coverage-reasons]");
    assert.equal(reasons.hidden, false);
    assert.equal(reasons.children[1].textContent, "<img src=x onerror=alert(1)>");
    assert.equal(reasons.children[1].childElementCount, 0);
    assert.equal(page.q("[data-comparison-coverage-details]").hidden, false);
    assert.equal(page.q("[data-comparison-coverage-details]").open, false);
    assert.equal(page.q("[data-comparison-coverage-heading]").hidden, false);
    assert.equal(page.q("[data-comparison-coverage-description]").hidden, false);
    assert.equal(page.q("[data-comparison-evidence-summary]").hidden, true);
});

test("cached templates without optional disclosures or legend still render findings", () => {
    const page = setup({coverage_status: "limited", coverage_reasons: ["Unresolved evidence"]}, {legacyTemplate: true});
    assert.equal(page.q("[data-comparison-coverage-reasons]").textContent, "Unresolved evidence");
    assert.equal(page.q("[data-comparison-heading]").textContent, "Changed text");
    assert.equal(page.side("after").querySelector(".cfm-comparison-mark-layer").childElementCount, 1);
    page.q('[data-comparison-mode="text"]').fire("click");
    assert.equal(page.side("after").querySelector("[data-comparison-text]").hidden, false);
});

test("legacy limitations use one coverage list, without duplicating engine reasons", () => {
    const page = setup({coverage_status: undefined, text_available: false, uncertain_count: 2, pairing_limited: true, truncated: true});
    assert.equal(page.q("[data-comparison-coverage-reasons]").childElementCount, 4);
    const current = setup({coverage_status: "limited", text_available: false, uncertain_count: 2, pairing_limited: true,
        coverage_reasons: ["Engine-specific evidence limitation"]});
    assert.equal(current.q("[data-comparison-coverage-reasons]").childElementCount, 1);
    assert.equal(current.q("[data-comparison-coverage-reasons]").textContent, "Engine-specific evidence limitation");
});

test("matched opaque identity can leave coverage only without fabricated findings", () => {
    const page = setup({findings: [], coverage_status: "limited", uncertain_count: 0,
        coverage_reasons: ["Exact glyph identity is supported; Unicode remains unknown."]});
    assert.equal(page.q("[data-comparison-coverage-heading]").textContent, "Engine coverage: limited");
    assert.match(page.q("[data-comparison-coverage-reasons]").textContent, /Unicode remains unknown/);
    assert.equal(page.q("[data-comparison-findings]").querySelectorAll("[data-finding-id]").length, 0);
    for (const side of ["before", "after"]) {
        page.loaded(side);
        assert.equal(page.side(side).querySelector(".cfm-comparison-mark-layer").childElementCount, 0);
        assert.equal(page.side(side).querySelector("[data-comparison-focus]").disabled, true);
    }
});

test("unknown vector candidates stay warnings with literal evidence and no invented Unicode", () => {
    const issue = "Vector candidate only; character equality is not established.";
    const page = setup({coverage_status: "limited", coverage_reasons: [issue], findings: [finding({
        category: "uncertain", label: "Check vector / formula", issues: [issue],
        before_pages: [], before_page: null, before_reference_page: 1,
        before_text: "", after_text: "", before_raw: "", after_raw: "",
    })]});
    assert.equal(page.q('[data-finding-group="uncertain"]').childElementCount, 1);
    assert.equal(page.q('[data-finding-group="attention"]').childElementCount, 0);
    assert.equal(page.root.classList.contains("cfm-comparison-uncertain-selected"), true);
    assert.equal(page.q("[data-comparison-evidence-reason]").textContent, issue);
    assert.equal(page.q("[data-comparison-raw-before]").textContent, "No extracted passage.");
    assert.equal(page.q("[data-comparison-raw-after]").textContent, "No extracted passage.");
    assert.equal(page.side("before").querySelector(".cfm-comparison-mark-layer").childElementCount, 0);
    assert.equal(page.side("after").querySelector(".cfm-comparison-mark-layer").childElementCount, 1);
});

test("missing or unknown coverage cannot imply complete, even for byte-identical PDFs", () => {
    for (const coverage_status of [undefined, "unknown"]) {
        const page = setup({coverage_status, identical_bytes: true, findings: []});
        assert.equal(page.q("[data-comparison-coverage-heading]").textContent, "Engine coverage not reported");
        assert.equal(page.q("[data-comparison-coverage]").classList.contains("alert-warning"), true);
    }
});

test("structure warnings, uncertain evidence and normalization Info stay separate", () => {
    const page = setup({findings: [finding({id: 7, category: "structure", issues: ["Exact position changed"]}),
        finding({id: 9, category: "uncertain", issues: ["Unsupported vector"]}),
        finding({id: 12, category: "formatting", before_raw: "a\u200bb", issues: ["Non-drawing U+200B"]})]});
    for (const name of ["attention", "uncertain", "formatting"]) assert.equal(page.q(`[data-finding-group="${name}"]`).childElementCount, 1);
    assert.match(page.q("[data-comparison-description]").textContent, /structural evidence/);
    assert.doesNotMatch(page.q("[data-comparison-description]").textContent, /pattern occurs/);
    assert.equal(page.root.classList.contains("cfm-comparison-structure-selected"), true);
    assert.equal(page.q("[data-comparison-evidence-reason]").textContent, "Exact position changed");
    page.selectCategory("uncertain");
    assert.match(page.q("[data-comparison-description]").textContent, /not been assumed unchanged/);
    page.selectCategory("formatting");
    assert.match(page.q("[data-comparison-description]").textContent, /Normalization information/);
    assert.equal(page.side("before").querySelector(".cfm-comparison-mark-layer").childElementCount, 0);
    assert.equal(page.q("[data-comparison-raw-before]").textContent, "a[U+200B]b");
    page.loaded("before");
    assert.equal(page.side("before").querySelector("[data-comparison-focus]").disabled, true);
});

test("focus reveals only actual loaded regions, preserving the selected pages", () => {
    const page = setup({findings: [finding({before_pages: [], before_page: null, before_reference_page: 1})]});
    const beforeFocus = page.side("before").querySelector("[data-comparison-focus]");
    const afterFocus = page.side("after").querySelector("[data-comparison-focus]");
    assert.equal(afterFocus.disabled, true);
    page.loaded("before"); page.loaded("after");
    assert.equal(beforeFocus.disabled, true);
    assert.equal(afterFocus.disabled, false);
    assert.match(page.side("before").querySelector("[data-comparison-side-position]").textContent, /No corresponding text. Reference page 1/);
    afterFocus.fire("click");
    assert.equal(page.reveals[0], page.side("after").querySelector(".cfm-comparison-mark"));
    assert.deepEqual(page.loads, ["/before/1.png", "/after/2.png"]);
    assert.equal(page.q("[data-comparison-mark-legend]").hidden, false);
    page.q('[data-comparison-mode="text"]').fire("click");
    assert.equal(afterFocus.disabled, true);
    assert.equal(page.q("[data-comparison-mark-legend]").hidden, true);
    page.q('[data-comparison-mode="pages"]').fire("click");
    assert.equal(page.q("[data-comparison-mark-legend]").hidden, false);
    page.q("[data-comparison-highlights]").fire("change", {checked: false});
    assert.equal(afterFocus.disabled, true);
    page.q("[data-comparison-highlights]").fire("change", {checked: true});
    assert.equal(afterFocus.disabled, false);
    page.side("after").querySelector("img").naturalWidth = 0;
    page.side("after").querySelector("img").fire("error");
    assert.equal(afterFocus.disabled, true);
    assert.equal(page.q("[data-comparison-image-error]").hidden, false);
});

test("finding-page changes preserve selection; ordinary page browsing clears it", () => {
    const page = setup({findings: [finding({before_pages: [1, 2], before_locations: [
        {page: 1, box: [.1, .2, .2, .3]}, {page: 2, box: [.1, .4, .2, .5]},
    ]})]});
    const selector = page.side("before").querySelector("[data-comparison-finding-pages]");
    assert.equal(selector.hidden, false);
    selector.value = 2; selector.fire("change");
    assert.equal(page.q("[data-comparison-heading]").textContent, "Changed text");
    assert.match(page.side("after").querySelector("[data-comparison-side-position]").textContent, /Reference page 3/);
    assert.equal(page.side("after").querySelector("[data-comparison-focus]").disabled, true);
    const input = page.side("before").querySelector("[data-comparison-page]");
    input.value = 1; input.fire("change");
    assert.equal(page.q("[data-comparison-heading]").textContent, "Manual page comparison");
    assert.equal(page.side("before").querySelector("[data-comparison-focus]").disabled, true);
});
