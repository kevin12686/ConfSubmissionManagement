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
    append(...children) { for (const child of children) { if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(item=>item!==child); child.parentElement = this; this.children.push(child); } }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { this.text = ""; this.children = []; this.append(...children); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key] ?? null; }
    addEventListener(type, handler) { this.listeners.set(type, [...(this.listeners.get(type) || []), handler]); }
    fire(type, props = {}) {
        const event = {target: this, ...props, currentTarget:this};
        for (const handler of this.listeners.get(type) || []) handler.call(this, event);
        if (["pointermove", "click"].includes(type)) this.parentElement?.fire(type, event);
    }
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
        if (this.bounds) return this.bounds;
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
        page_urls: {before: "/signed-before/0.png", after: "/signed-after/0.png"}, page_map: {1: 1, 2: 3}, inverse_map: {1: 1, 3: 2}, ...overrides};
    const loads = [], reveals = [], timers = new Map(), frames = new Map(); let timerId = 0;
    const win = new Element("window");
    Object.assign(win, {innerHeight: 1000, matchMedia: () => ({matches: true}),
        CFMImageLoader: {load(page, url) {
            const image = page.querySelector("img"); image.setAttribute("src", url); image.complete = false; image.naturalWidth = 0;
            page.classList.remove("is-ready", "is-error"); page.classList.add("is-loading"); loads.push(url);
        }}, CFMContextExpansion: {reveal: target => reveals.push(target)}});
    runInNewContext(script, {document: {readyState: "complete", querySelector: selector => doc.querySelector(selector),
        addEventListener: (type, handler) => doc.addEventListener(type, handler),
        getElementById: () => ({textContent: JSON.stringify(data)}), createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)},
        window: win, setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
        requestAnimationFrame: fn => { frames.set(++timerId, fn); return timerId; }, cancelAnimationFrame: id => frames.delete(id)});
    const api = {root, doc, q, data, loads, reveals, win,
        side: name => q(`[data-comparison-side="${name}"]`),
        panel: q("[data-comparison-inspection]"),
        crop: name => q(`[data-comparison-crop="${name}"]`),
        tick() { for (const [id, fn] of timers) { timers.delete(id); fn(); } },
        frame() { for (const [id, fn] of frames) { frames.delete(id); fn(); } },
        frames,
        loaded(name, width = 1190, height = 1684) { const side = this.side(name), image = side.querySelector("img"); image.complete = true; image.naturalWidth = width; image.naturalHeight = height; side.querySelector(".cfm-comparison-page").classList.add("is-ready"); image.fire("load"); },
        marks(name) { return this.side(name).querySelector(".cfm-comparison-mark-layer").children; },
        next() { q('[data-comparison-step="1"]').fire("click"); },
        category(value) { const input = q("[data-comparison-category]"); input.value = value; input.fire("change"); },
        navigate(name, value, findingPage = false) { const input = this.side(name).querySelector(findingPage ? "[data-comparison-finding-pages]" : "[data-comparison-page]"); input.value = value; input.fire("change"); },
        page(name) { return this.side(name).querySelector(".cfm-comparison-page"); },
        hover(name, index = 0) {
            const mark = this.marks(name)[index], rect = this.page(name).getBoundingClientRect();
            const box = mark.location.box;
            this.page(name).fire("pointerenter", {target: mark, pointerType: "mouse", clientX: rect.left + (box[0] + box[2]) / 2 * rect.width, clientY: rect.top + (box[1] + box[3]) / 2 * rect.height});
        },
        hoverPoint(name, x, y, props = {}) {
            const rect = this.page(name).getBoundingClientRect();
            this.page(name).fire("pointermove", {pointerType: "mouse", clientX: rect.left + x * rect.width, clientY: rect.top + y * rect.height, ...props});
        },
    };
    return api;
}

test("production markup has independent shared document panels and starts document-first", () => {
    const page = setup({findings: [finding({before_page: 2, before_pages: [2]})]});
    assert.deepEqual(page.loads, ["/signed-before/1.png", "/signed-after/1.png"]);
    assert.equal(page.panel.hidden, false);
    assert.equal(page.q(".cfm-comparison-findings").attributes.open, undefined);
    assert.equal(page.q("[data-comparison-position]").textContent, "1 displayed");
    assert.doesNotMatch(template, /structure|glyph|Check text \/ formula|frozen|prototype-data/);
});
test("all eligible current-page marks appear without selecting a finding", () => {
    const page = setup({findings: [finding(), finding({id: 1, category: "uncertain", kind: "author_review"}), finding({id: 2, category: "tolerated", kind: "ignored_visual"})]});
    assert.equal(page.marks("before").length, 0);
    page.loaded("before");
    assert.deepEqual(page.marks("before").map(mark => mark.dataset.kind), ["change", "review", "info"]);
    assert.equal(page.panel.hidden, false);
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
test("fixed inspection stays available across pointer crossing and document scroll", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    page.page("before").fire("pointerleave"); page.panel.fire("pointerenter"); page.tick();
    assert.equal(page.panel.hidden, false);
    page.panel.fire("pointerleave"); page.tick();
    page.win.fire("scroll"); page.side("before").querySelector(".cfm-comparison-scroll").fire("scroll");
    assert.equal(page.panel.hidden, false); assert.equal(page.crop("before").draws.length, 1);
});
test("one-sided finding magnifies both displayed pages without fabricating a counterpart mark or connection", () => {
    const page = setup({findings: [finding({kind: "added", before_pages: [], before_locations: [], before_page: null, before_reference_page: 1, before_text: ""})]});
    page.loaded("before"); page.loaded("after"); page.hover("after");
    assert.equal(page.marks("before").length, 0); assert.equal(page.crop("before").draws.length, 1);
    assert.equal(page.q("[data-comparison-connection-layer]").childElementCount, 0);
    assert.match(page.q("[data-comparison-pair-status]").textContent, /not a text correspondence/);
    assert.equal(page.crop("after").dataset.source, "/signed-after/1.png");
});
test("an off-page finding does not hide the currently displayed opposite page or navigate to its match", () => {
    const page = setup({findings: [finding({after_pages: [2], after_page: 2, after_locations: [{page: 2, box: [.2, .4, .4, .5]}]})]});
    page.loaded("before"); page.loaded("after"); page.hover("before");
    assert.equal(page.crop("after").draws.length, 1);
    assert.equal(page.crop("after").dataset.source, "/signed-after/1.png");
    assert.equal(page.q('[data-comparison-crop-page="after"]').textContent, " / page 1");
    assert.equal(page.loads.length, 2);
});
test("unlocated pointer movement updates both relative crops in one animation frame, connections remain candidates", () => {
    const page = setup({findings: [finding({category: "uncertain", kind: "author_review", before_locations: [{page: 1, box: [0, 0, 1, 1]}]})]});
    page.loaded("before"); page.loaded("after"); page.hover("before");
    const before = page.crop("before").draws[0].slice(1), after = page.crop("after").draws[0].slice(1);
    page.marks("before")[0].fire("pointermove", {clientX: 500, clientY: 850});
    page.marks("before")[0].fire("pointermove", {clientX: 520, clientY: 860});
    assert.equal(page.frames.size, 1);
    assert.deepEqual(page.crop("before").draws[0].slice(1), before);
    page.frame();
    assert.notDeepEqual(page.crop("before").draws[0].slice(1), before);
    assert.notDeepEqual(page.crop("after").draws[0].slice(1), after);
    assert.deepEqual(page.crop("before").draws[0].slice(1), page.crop("after").draws[0].slice(1));
    assert.match(page.q("[data-comparison-pair-status]").textContent, /not a text correspondence/);
    assert.equal(page.q("[data-comparison-connection-layer]").firstElementChild.dataset.kind, "review");
});
test("new page immediately clears old marks/crops and late old source cannot draw", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    page.marks("before")[0].fire("pointermove", {clientX: 150, clientY: 500});
    page.navigate("before", 2);
    assert.equal(page.frames.size, 0); page.frame();
    assert.equal(page.panel.hidden, false); assert.equal(page.marks("before").length, 0); assert.equal(page.crop("before").draws.length, 0);
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
    assert.equal(page.q("[data-comparison-raw-before]").textContent,"old");
    page.navigate("before", 1);
    assert.equal(page.q("[data-comparison-heading]").textContent, "Document comparison");
});

test("manual after-page navigation drives relinking and explicit close stays collapsed on hover",()=>{
    const p=setup();p.loaded('before');p.loaded('after');
    const link=p.q('[data-comparison-link]');link.checked=false;link.fire('change');
    p.navigate('after',3);link.checked=true;link.fire('change');
    assert.equal(p.side('before').querySelector('[data-comparison-page]').value,2);
    assert.equal(p.q('[data-comparison-raw-before]').textContent,'No recognized passage.');
    p.loaded('before');p.loaded('after');
    p.q('[data-comparison-close]').fire('click');p.hoverPoint('before',.4,.4);
    assert.equal(p.panel.hidden,true);
    p.q('[data-comparison-inspector-toggle]').fire('click');p.hoverPoint('before',.4,.4);
    assert.equal(p.panel.hidden,false);assert.equal(p.crop('after').dataset.state,'ready');
});
test("unreliable page map never substitutes equal page numbers", () => {
    const page = setup({page_map: {}}); page.navigate("before", 2);
    assert.equal(page.side("after").querySelector("[data-comparison-page]").value, "");
    assert.match(page.q("[data-comparison-map-status]").textContent, /No corresponding page/);
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
    const mark = page.marks("before")[0]; mark.fire("pointerenter", {pointerType: "touch"}); assert.equal(page.crop("before").draws.length, 0);
    mark.fire("click", {detail: 1, pointerType: "touch", clientX: 100, clientY: 400}); assert.equal(page.panel.hidden, false);
    page.doc.fire("keydown", {key: "Escape"}); assert.equal(page.panel.hidden, true);
    mark.focusVisible = true; mark.fire("focus"); assert.equal(page.panel.dataset.interaction, "keyboard");
    page.doc.fire("keydown", {key: "Control"}); assert.equal(page.panel.hidden, true);
});
test("invalid locations never draw; reference findings remain navigable", () => {
    const page = setup({findings: [finding({category: "reference", kind: "reference", before_locations: [{page: 1, box: [-.1, 0, .2, .3]}]})]});
    page.loaded("before"); assert.equal(page.marks("before").length, 0);
    page.category("reference"); page.next(); assert.match(page.q("[data-comparison-description]").textContent, /Possible reference error/);
});

test("dismissed explicit selection does not block subsequent hover", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.next();
    page.doc.fire("keydown", {key: "Escape"});
    page.hover("before"); assert.equal(page.panel.hidden, false);
    page.doc.fire("keydown", {key: "Escape"});
    page.hover("before"); assert.equal(page.panel.hidden, true);
    page.page("before").fire("pointerleave"); page.hover("before"); assert.equal(page.panel.hidden, false);
});

test("zero findings and hidden marks still allow whole-page relative magnification with no fabricated OCR", () => {
    const page = setup({findings: []}); page.loaded("before"); page.loaded("after");
    page.hoverPoint("before", .4, .6);
    assert.equal(page.panel.hidden, false);
    assert.equal(page.q("[data-comparison-inspection-title]").textContent, "Page magnification");
    assert.equal(page.q("[data-comparison-evidence]").hidden, true);
    assert.equal(page.q("[data-comparison-description]").hidden, true);
    for (const side of ["before", "after"]) {
        assert.equal(page.crop(side).dataset.state, "ready");
        assert.equal(page.crop(side).dataset.findingId, undefined);
        assert.equal(page.marks(side).length, 0);
    }
    const input = page.q("[data-comparison-highlights]"); input.checked = false; input.fire("change");
    page.hoverPoint("after", .6, .4);
    assert.equal(page.panel.hidden, false);
    assert.equal(page.crop("before").dataset.state, "ready");
    assert.deepEqual(page.crop("before").draws[0].slice(1), page.crop("after").draws[0].slice(1));
});

test("leaving a finding retains its evidence and anchor while pointers move beyond its box", () => {
    const page = setup(); page.loaded("before"); page.loaded("after"); page.hover("before");
    const raw = page.q("[data-comparison-raw-before]").textContent;
    page.hoverPoint("before", .6, .6); page.frame();
    assert.equal(page.q("[data-comparison-evidence]").hidden, true);
    assert.equal(page.q("[data-comparison-overlap-label]").hidden, true);
    assert.equal(page.q("[data-comparison-raw-before]").textContent, raw);
    assert.deepEqual(page.crop("before").draws[0].slice(1), page.crop("after").draws[0].slice(1));
    const [, sx, sy, cw, ch] = page.crop("before").draws[0];
    assert.equal((sx + cw / 2) / 1190, .6);
    assert.equal((sy + ch / 2) / 1684, .6);
});

test("relative position survives unequal image dimensions, display scale, scroll offset and reversed hover side", () => {
    const page = setup({findings: []}); page.loaded("before", 1200, 1800); page.loaded("after", 1800, 1200);
    page.page("after").bounds = {left: -320, top: -500, width: 900, height: 600};
    page.hoverPoint("after", .6, .7);
    for (const [side, w, h] of [["before", 1200, 1800], ["after", 1800, 1200]]) {
        const [, sx, sy, cw, ch] = page.crop(side).draws[0];
        assert.equal((sx + cw / 2) / w, .6);
        assert.equal((sy + ch / 2) / h, .7);
        assert.equal(cw / ch, 8 / 3);
    }
});

test("magnification remains two times the displayed page scale on narrow and wide crop panels", () => {
    const page = setup({findings: []}); page.loaded("before"); page.loaded("after");
    for (const cropWidth of [150, 400]) {
        page.crop("before").clientWidth = cropWidth;
        page.hoverPoint("before", .5, .5); page.frame();
        const cw = page.crop("before").draws[0][3];
        assert.equal((cropWidth / cw) / (600 / 1190), 2);
    }
});

test("relative positioning explicitly bypasses region anchors for keyboard magnification", () => {
    const page = setup({findings: [finding({before_locations: [{page: 1, box: [.3, .4, .5, .6]}], after_locations: [{page: 1, box: [.6, .7, .8, .9]}]})]});
    page.q("[data-inspection-position]").value="relative";
    page.loaded("before"); page.loaded("after");
    const mark = page.marks("before")[0]; mark.focusVisible = true; mark.fire("focus");
    for (const side of ["before", "after"]) {
        const [, sx, sy, cw, ch] = page.crop(side).draws[0];
        assert.equal((sx + cw / 2) / 1190, .4);
        assert.equal((sy + ch / 2) / 1684, .5);
    }
});

test("hidden or zero-width page geometry clears hover instead of leaving stale crops", () => {
    const page = setup({findings: []}); page.loaded("before"); page.loaded("after");
    page.hoverPoint("before", .5, .5);
    page.page("before").bounds = {left: 0, top: 0, width: 0, height: 0};
    page.hoverPoint("before", .5, .5);
    assert.equal(page.panel.hidden, true);
    assert.equal(page.crop("after").dataset.source, undefined);
});

test("corners clamp inside each actual image, including small and very wide pages", () => {
    const page = setup({findings: []}); page.loaded("before", 12, 4); page.loaded("after", 2000, 100);
    for (const [x, y] of [[0, 0], [1, 1], [-.1, 1.1]]) {
        page.hoverPoint("before", x, y); page.frame();
        for (const [side, w, h] of [["before", 12, 4], ["after", 2000, 100]]) {
            const [, sx, sy, cw, ch] = page.crop(side).draws[0];
            assert.ok(sx >= 0 && sy >= 0 && cw > 0 && ch > 0);
            assert.ok(sx + cw <= w && sy + ch <= h);
        }
    }
});

test("loading or failed opposite image has an explicit status, then uses only the loaded current image", () => {
    const page = setup({findings: []}); page.loaded("before"); page.hoverPoint("before", .5, .5);
    assert.equal(page.crop("after").hidden, true);
    assert.match(page.q('[data-comparison-crop-status="after"]').textContent, /loading/);
    page.side("after").querySelector("img").fire("error");
    assert.equal(page.crop("after").draws.length, 0);
    page.loaded("after");
    assert.equal(page.crop("after").hidden, false);
    assert.equal(page.crop("after").dataset.source, "/signed-after/1.png");
    assert.equal(page.loads.length, 2);
});

test("generic inspection dismisses with Escape, resumes after leave and supports touch without hover", () => {
    const page = setup({findings: []}); page.loaded("before"); page.loaded("after");
    page.hoverPoint("before", .5, .5, {ctrlKey: true}); assert.equal(page.crop("before").draws.length, 0);
    page.hoverPoint("before", .5, .5, {pointerType: "touch"}); assert.equal(page.crop("before").draws.length, 0);
    page.page("before").fire("click", {detail: 1, clientX: 300, clientY: 600});
    assert.equal(page.panel.dataset.interaction, "touch");
    page.doc.fire("keydown", {key: "Escape"}); assert.equal(page.panel.hidden, true);
    page.hoverPoint("before", .5, .5); page.doc.fire("keydown", {key: "Escape"});
    page.hoverPoint("before", .6, .6); assert.equal(page.panel.hidden, true);
    page.page("before").fire("pointerleave"); page.hoverPoint("before", .6, .6);
    assert.equal(page.panel.hidden, false);
});

test("Escape closes hover inspection without a focused control inside the comparison", () => {
    const page = setup(); page.loaded("before"); page.loaded("after");
    page.hover("before");
    assert.equal(page.panel.hidden, false);
    page.doc.fire("keydown", {key: "Escape", target: new Element("body")});
    assert.equal(page.panel.hidden, true);
    assert.equal(page.crop("before").draws.length, 0);
    page.doc.fire("keydown", {key: "Escape", target: new Element("input")});
    assert.equal(page.panel.hidden, true);
});

test("four leading unmatched pages stay empty until the fifth maps to after page one", () => {
    const p=setup({before_pages:Array(6).fill({width:595,height:842}),page_map:{5:1,6:2},inverse_map:{1:5,2:6}});
    for(let n=1;n<=4;n++) {
        p.navigate('before',n);p.loaded('before');p.hoverPoint('before',.5,.5);
        assert.equal(p.side('after').querySelector('[data-comparison-page-empty]').hidden,false);
        assert.equal(p.crop('after').hidden,true);
        assert.equal(p.marks('after').length,0);
        assert.ok(!p.loads.includes('/signed-after/1.png'));
    }
    p.navigate('before',5);p.loaded('before');p.loaded('after');
    assert.equal(p.side('after').querySelector('[data-comparison-page]').value,1);
    assert.equal(p.side('after').querySelector('[data-comparison-page-empty]').hidden,true);
    p.navigate('after',3);
    assert.equal(p.side('before').querySelector('[data-comparison-page-empty]').hidden,false);
});

test("paired anchor survives all box edges and follows pointer offset instead of snapping to relative position", () => {
    const p=setup({findings:[finding({before_locations:[{page:1,box:[.3,.3,.4,.4]}],after_locations:[{page:1,box:[.5,.5,.6,.6]}]})]});
    p.loaded('before');p.loaded('after');p.hover('before');
    for(const [x,y] of [[.29,.35],[.41,.35],[.35,.29],[.35,.41],[.41,.41]]) {
        p.hoverPoint('before',x,y);p.frame();
        const [,sx,sy,cw,ch]=p.crop('after').draws[0];
        assert.ok(Math.abs((sx+cw/2)/1190-(x+.2))<.0001);
        assert.ok(Math.abs((sy+ch/2)/1684-(y+.2))<.0001);
    }
    p.q('[data-inspection-position]').value='relative';p.q('[data-inspection-position]').fire('change');
    const [,sx,,cw]=p.crop('after').draws[0];assert.ok(Math.abs((sx+cw/2)/1190-.41)<.0001);
});

test("selected evidence survives other hover, panel scrolling, and multiple ambiguous regions use relative fallback",()=>{
    const p=setup({findings:[finding(),finding({id:1,before_raw:'other',before_locations:[{page:1,box:[.4,.4,.5,.5]},{page:1,box:[.6,.6,.7,.7]}]})]});
    p.loaded('before');p.loaded('after');p.marks('before')[0].fire('click',{detail:1});
    const raw=p.q('[data-comparison-raw-before]').textContent;
    p.hover('before',1);p.tick();p.panel.fire('pointerenter');p.panel.fire('scroll');p.win.fire('scroll');
    assert.equal(p.q('[data-comparison-raw-before]').textContent,raw);
    assert.equal(p.panel.hidden,false);
    assert.match(p.q('[data-comparison-pair-status]').textContent,/Relative position/);
});
