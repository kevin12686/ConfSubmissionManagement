import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const read = path => readFileSync(new URL(path, import.meta.url), "utf8");
const script = read("../../static/submissions/pdf_comparison.js");
const template = read("../../templates/submissions/pdf_comparison.html");
const partial = read("../../templates/submissions/partials/pdf_comparison_document.html");

// Behavioral DOM double. Control structure comes from the production templates, not a second UI.
class Element {
    constructor(tag = "div", attributes = {}) {
        this.tagName = tag; this.attributes = attributes; this.children = []; this.listeners = new Map();
        this.style = {}; this.hidden = "hidden" in attributes; this.checked = "checked" in attributes;
        this.disabled = "disabled" in attributes; this.className = attributes.class || "";
        this.dataset = new Proxy({}, {set: (object, key, value) => { object[key] = String(value); return true; }});
        for (const [key, value] of Object.entries(attributes)) if (key.startsWith("data-")) this.dataset[camel(key.slice(5))] = value;
        this.classList = {
            contains: name => this.className.split(" ").includes(name),
            toggle: (name, force = !this.classList.contains(name)) => { this.className = this.className.split(" ").filter(x => x && x !== name).concat(force ? [name] : []).join(" "); return force; },
            add: (...names) => names.forEach(name => this.classList.toggle(name, true)),
            remove: (...names) => names.forEach(name => this.classList.toggle(name, false)),
        };
        this.clientWidth = 600; this.offsetHeight = 300; this.width = 800; this.height = 300;
        this.scrollTop = 0; this.scrollLeft = 0; this.draws = [];
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return (this.text || "") + this.children.map(child => child.textContent).join(""); }
    get firstElementChild() { return this.children[0] || null; }
    get childElementCount() { return this.children.length; }
    append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { this.text = ""; this.children = []; this.append(...children); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key] ?? null; }
    addEventListener(type, handler) { this.listeners.set(type, [...(this.listeners.get(type) || []), handler]); }
    fire(type, props = {}) { for (const handler of this.listeners.get(type) || []) handler.call(this, {target: this, ...props}); }
    contains(target) { return target === this || this.children.some(child => child.contains(target)); }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector); }
    matches(selector) {
        if (selector === ":focus-visible") return !!this.focusVisible;
        if (selector.endsWith(":not([hidden])")) return !this.hidden && this.matches(selector.replace(":not([hidden])", ""));
        if (selector.startsWith(".")) return selector.slice(1).split(".").every(name => this.classList.contains(name));
        const attribute = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
        if (attribute) {
            const [, key, value] = attribute;
            const actual = key.startsWith("data-") ? this.dataset[camel(key.slice(5))] : this.attributes[key];
            return actual !== undefined && (value === undefined || actual === value);
        }
        return this.tagName === selector;
    }
    querySelectorAll(selector) {
        if (selector.startsWith(":scope > ")) return this.children.filter(child => child.matches(selector.slice(9)));
        return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    getBoundingClientRect() {
        const after = this.closest('[data-comparison-side="after"]');
        const left = after ? 700 : 0;
        return {left, top: 200, right: left + 600, bottom: 1000, width: 600, height: 800};
    }
    getContext() {
        return {clearRect: () => { this.draws = []; }, drawImage: (...args) => this.draws.push(args),
            save() {}, restore() {}, beginPath() {}, rect() {}, clip() {}, strokeRect() {}};
    }
}
const camel = text => text.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
function markup() {
    const expanded = template.replace(/{% include [^%]+side="(before|after)"[^%]+%}/g,
        (_, side) => partial.replace(/{{ side }}/g, side).replace(/{{ side_label }}/g, side));
    const html = expanded.replace(/{%[\s\S]*?%}/g, "").replace(/{{[\s\S]*?}}/g, "");
    const document = new Element("document"), stack = [document];
    for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)([^>]*)>/gi)) {
        if (match[0].startsWith("</")) { stack.pop(); continue; }
        const attributes = {};
        for (const attr of match[2].matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) attributes[attr[1]] = attr[2] || "";
        const element = stack.at(-1).appendChild(new Element(match[1], attributes));
        if (!["input", "img", "link", "br"].includes(match[1])) stack.push(element);
    }
    return document;
}
function finding(overrides = {}) {
    return {id: 0, category: "text", kind: "changed", label: "OCR text difference",
        before_pages: [1], after_pages: [1], before_page: 1, after_page: 1,
        before_text: "old", after_text: "new", before_raw: "old", after_raw: "new",
        before_locations: [{page: 1, box: [.1, .2, .2, .3]}], after_locations: [{page: 1, box: [.1, .2, .2, .3]}],
        before_ocr: [], after_ocr: [], issues: [], ...overrides};
}
function setup(overrides = {}) {
    const doc = markup(), root = doc.querySelector("[data-pdf-comparison]"), q = selector => root.querySelector(selector);
    const data = {coverage_status: "limited", coverage_reasons: ["OCR can miss content."], evidence_summary: {},
        findings: [finding()], text_available: true, total_findings: 1,
        before_pages: [{width: 595, height: 842}, {width: 595, height: 842}],
        after_pages: [{width: 595, height: 842}, {width: 595, height: 842}, {width: 595, height: 842}],
        page_urls: {before: "/signed-before/0.png", after: "/signed-after/0.png"}, page_map: {1: 2, 2: 3}, inverse_map: {2: 1, 3: 2}, ...overrides};
    const loads = [], reveals = [], timers = new Map(), frames = new Map(); let timerId = 0;
    const win = new Element("window");
    Object.assign(win, {innerHeight: 1000, matchMedia: () => ({matches: true}),
        CFMImageLoader: {load(page, url) {
            const image = page.querySelector("img"); image.setAttribute("src", url); image.complete = false; image.naturalWidth = 0;
            page.classList.remove("is-ready", "is-error"); page.classList.add("is-loading"); loads.push(url);
        }}, CFMContextExpansion: {reveal: target => reveals.push(target)}});
    runInNewContext(script, {document: {readyState: "complete", querySelector: selector => doc.querySelector(selector),
        getElementById: () => ({textContent: JSON.stringify(data)}), createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)},
        window: win, setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
        requestAnimationFrame: fn => { frames.set(++timerId, fn); return timerId; }, cancelAnimationFrame: id => frames.delete(id)});
    const api = {root, q, data, loads, reveals, win,
        side: name => q(`[data-comparison-side="${name}"]`),
        panel: q("[data-comparison-inspection]"),
        crop: name => q(`[data-comparison-crop="${name}"]`),
        tick() { for (const [id, fn] of timers) { timers.delete(id); fn(); } },
        frame() { for (const [id, fn] of frames) { frames.delete(id); fn(); } },
        frames,
        loaded(name) { const side = this.side(name), image = side.querySelector("img"); image.complete = true; image.naturalWidth = 1190; image.naturalHeight = 1684; side.querySelector(".cfm-comparison-page").classList.add("is-ready"); image.fire("load"); },
        marks(name) { return this.side(name).querySelector(".cfm-comparison-mark-layer").children; },
        next() { q('[data-comparison-step="1"]').fire("click"); },
        category(value) { const input = q("[data-comparison-category]"); input.value = value; input.fire("change"); },
        navigate(name, value, findingPage = false) { const input = this.side(name).querySelector(findingPage ? "[data-comparison-finding-pages]" : "[data-comparison-page]"); input.value = value; input.fire("change"); },
        hover(name, index = 0) { this.marks(name)[index].fire("pointerenter", {pointerType: "mouse", clientX: 100, clientY: 450}); },
    };
    return api;
}

test("production markup has independent shared document panels and starts document-first", () => {
    const page = setup({findings: [finding({before_page: 2, before_pages: [2]})]});
    assert.deepEqual(page.loads, ["/signed-before/1.png", "/signed-after/1.png"]);
    assert.equal(page.panel.hidden, true);
    assert.equal(page.q(".cfm-comparison-findings").attributes.open, undefined);
    assert.equal(page.q("[data-comparison-position]").textContent, "1 displayed");
    assert.doesNotMatch(template, /structure|glyph|Check text \/ formula|frozen|prototype-data/);
});
test("all eligible current-page marks appear without selecting a finding", () => {
    const page = setup({findings: [finding(), finding({id: 1, category: "uncertain", kind: "author_review"}), finding({id: 2, category: "tolerated", kind: "ignored_visual"})]});
    assert.equal(page.marks("before").length, 0);
    page.loaded("before");
    assert.deepEqual(page.marks("before").map(mark => mark.dataset.kind), ["change", "review", "info"]);
    assert.equal(page.panel.hidden, true);
});
test("hover opens both pixel crops without navigation, scrolling or selected-state changes", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    assert.equal(page.panel.hidden, false);
    assert.equal(page.panel.dataset.interaction, "hover");
    assert.deepEqual(page.loads, ["/signed-before/1.png", "/signed-after/1.png"]);
    assert.equal(page.reveals.length, 0);
    assert.equal(page.q("[data-comparison-heading]").textContent, "Document comparison");
    for (const name of ["before", "after"]) {
        assert.equal(page.crop(name).dataset.source, `/signed-${name}/1.png`);
        assert.equal(page.crop(name).draws[0][0], page.side(name).querySelector("img"));
    }
});
test("crossing into the panel cancels delayed close; leaving it closes and clears crops", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    page.marks("before")[0].fire("pointerleave"); page.panel.fire("pointerenter"); page.tick();
    assert.equal(page.panel.hidden, false);
    page.panel.fire("pointerleave"); page.tick();
    assert.equal(page.panel.hidden, true); assert.equal(page.crop("before").draws.length, 0);
    assert.equal(page.crop("before").dataset.source, undefined);
});
test("one-sided finding never fabricates a counterpart, crop or connection", () => {
    const page = setup({findings: [finding({kind: "added", before_pages: [], before_locations: [], before_page: null, before_reference_page: 1, before_text: ""})]});
    page.loaded("before"); page.loaded("after"); page.hover("after");
    assert.equal(page.marks("before").length, 0); assert.equal(page.crop("before").draws.length, 0);
    assert.equal(page.q("[data-comparison-connection-layer]").childElementCount, 0);
    assert.match(page.q("[data-comparison-pair-status]").textContent, /One-sided/);
    assert.equal(page.crop("after").dataset.source, "/signed-after/1.png");
});
test("hover never loads an off-page counterpart", () => {
    const page = setup({findings: [finding({after_pages: [2], after_page: 2, after_locations: [{page: 2, box: [.2, .4, .4, .5]}]})]});
    page.loaded("before"); page.loaded("after"); page.hover("before");
    assert.equal(page.crop("after").draws.length, 0);
    assert.match(page.q('[data-comparison-crop-status="after"]').textContent, /page 2/);
    assert.equal(page.loads.length, 2);
});
test("unlocated pointer movement changes only its own crop, and connections remain candidates", () => {
    const page = setup({findings: [finding({category: "uncertain", kind: "author_review", before_locations: [{page: 1, box: [0, 0, 1, 1]}]})]});
    page.loaded("before"); page.loaded("after"); page.hover("before");
    const before = page.crop("before").draws[0].slice(1), after = page.crop("after").draws[0].slice(1);
    page.marks("before")[0].fire("pointermove", {clientX: 500, clientY: 850});
    page.marks("before")[0].fire("pointermove", {clientX: 520, clientY: 860});
    assert.equal(page.frames.size, 1);
    assert.deepEqual(page.crop("before").draws[0].slice(1), before);
    page.frame();
    assert.notDeepEqual(page.crop("before").draws[0].slice(1), before);
    assert.deepEqual(page.crop("after").draws[0].slice(1), after);
    assert.match(page.q("[data-comparison-pair-status]").textContent, /not a verified correspondence/);
    assert.equal(page.q("[data-comparison-connection-layer]").firstElementChild.dataset.kind, "review");
});
test("new page immediately clears old marks/crops and late old source cannot draw", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    page.marks("before")[0].fire("pointermove", {clientX: 150, clientY: 500});
    page.navigate("before", 2);
    assert.equal(page.frames.size, 0); page.frame();
    assert.equal(page.panel.hidden, true); assert.equal(page.marks("before").length, 0); assert.equal(page.crop("before").draws.length, 0);
    const image = page.side("before").querySelector("img"); image.setAttribute("src", "/signed-before/1.png"); page.loaded("before");
    assert.equal(page.marks("before").length, 0);
    assert.equal(page.crop("before").dataset.source, undefined);
    assert.deepEqual(page.loads.slice(-2), ["/signed-before/2.png", "/signed-after/3.png"]);
});
test("failed image clears marks and crops; successful retry restores only the same source", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    page.side("before").querySelector("img").fire("error");
    assert.equal(page.crop("before").draws.length, 0); assert.equal(page.marks("before").length, 0);
    assert.equal(page.q("[data-comparison-image-error]").hidden, false);
    page.loaded("before"); assert.equal(page.q("[data-comparison-image-error]").hidden, true);
    assert.equal(page.crop("before").dataset.source, "/signed-before/1.png");
});
test("explicit navigation uses recorded pages and independent multipage selector", () => {
    const page = setup({findings: [finding({before_pages: [1, 2], before_locations: [{page: 1, box: [.1, .2, .2, .3]}, {page: 2, box: [.1, .2, .2, .3]}]})]});
    page.next(); page.navigate("before", 2, true);
    assert.equal(page.side("after").querySelector("[data-comparison-page]").value, 1);
    assert.equal(page.q("[data-comparison-heading]").textContent, "OCR text difference");
    page.navigate("before", 1);
    assert.equal(page.q("[data-comparison-heading]").textContent, "Document comparison");
});
test("unreliable page map never substitutes equal page numbers", () => {
    const page = setup({page_map: {}}); page.navigate("before", 2);
    assert.equal(page.side("after").querySelector("[data-comparison-page]").value, 1);
    assert.match(page.q("[data-comparison-map-status]").textContent, /other page has not been changed/);
});
test("three Info kinds stay separate, neutral and independently filterable", () => {
    const page = setup({findings: [finding({id: 0, category: "tolerated", kind: "ignored_visual", rule_id: "round", rule_risk: "Real edits may be missed"}),
        finding({id: 1, category: "formatting", kind: "ignored_spacing"}), finding({id: 2, category: "formatting", kind: "wrap_hyphen"})]});
    for (const group of ["tolerated", "ignored_spacing", "wrap_hyphen"]) assert.equal(page.q(`[data-finding-group="${group}"]`).childElementCount, 1);
    page.loaded("before"); assert.ok(page.marks("before").every(mark => mark.dataset.kind === "info"));
    page.category("ignored_spacing"); assert.equal(page.marks("before").length, 1); assert.equal(page.loads.length, 2);
    page.category("tolerated"); page.next();
    assert.match(page.q("[data-comparison-description]").textContent, /not proof/);
    assert.match(page.q("[data-comparison-evidence-reason]").textContent, /rule_risk: Real edits may be missed/);
});
test("raw OCR, scores, risks and coverage reasons are literal text", () => {
    const unsafe = "<img src=x onerror=alert(1)>";
    const page = setup({coverage_reasons: [unsafe], findings: [finding({issues: [unsafe], before_raw: "a\u200bb", before_ocr: [{text: unsafe, score: .91}]})]});
    page.next();
    assert.equal(page.q("[data-comparison-evidence-reason]").firstElementChild.childElementCount, 0);
    assert.match(page.q("[data-comparison-raw-before]").textContent, /a\[U\+200B\]b/);
    assert.match(page.q("[data-comparison-raw-before]").textContent, /0.91/);
    assert.equal(page.q("[data-comparison-coverage-reasons]").textContent, unsafe);
});
test("limited or missing coverage stays explicit at zero findings; counts are not safety scores", () => {
    for (const coverage_status of ["limited", undefined]) {
        const page = setup({findings: [], coverage_status, evidence_summary: {before: {ocr_lines: 4, unlocated_tokens: 0}}});
        assert.equal(page.q("[data-comparison-coverage-details]").open, false);
        assert.match(page.q("[data-comparison-coverage-heading]").textContent, coverage_status ? /limited/ : /not reported/);
        assert.deepEqual(page.q("[data-comparison-evidence-counts]").children.map(item => item.textContent), ["before / ocr lines", "4", "before / unlocated tokens", "0"]);
        assert.equal(page.q('[data-comparison-step="1"]').disabled, true);
    }
});
test("keyboard, touch, Escape and Ctrl preserve accessible inspection without hover requirements", () => {
    const page = setup(); page.loaded("before"); page.loaded("after");
    const mark = page.marks("before")[0]; mark.fire("pointerenter", {pointerType: "touch"}); assert.equal(page.panel.hidden, true);
    mark.fire("click", {detail: 1, pointerType: "touch", clientX: 100, clientY: 400}); assert.equal(page.panel.hidden, false);
    page.root.fire("keydown", {key: "Escape"}); assert.equal(page.panel.hidden, true);
    mark.focusVisible = true; mark.fire("focus"); assert.equal(page.panel.dataset.interaction, "keyboard");
    page.root.fire("keydown", {key: "Control"}); assert.equal(page.panel.hidden, true);
});
test("invalid locations never draw; reference findings remain navigable", () => {
    const page = setup({findings: [finding({category: "reference", kind: "reference", before_locations: [{page: 1, box: [-.1, 0, .2, .3]}]})]});
    page.loaded("before"); assert.equal(page.marks("before").length, 0);
    page.category("reference"); page.next(); assert.match(page.q("[data-comparison-description]").textContent, /Possible reference error/);
});

test("dismissed explicit selection does not block subsequent hover", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.next();
    page.root.fire("keydown", {key: "Escape"});
    page.hover("before"); assert.equal(page.panel.hidden, false);
    page.root.fire("keydown", {key: "Escape"});
    page.hover("before"); assert.equal(page.panel.hidden, true);
    page.marks("before")[0].fire("pointerleave"); page.hover("before"); assert.equal(page.panel.hidden, false);
});
