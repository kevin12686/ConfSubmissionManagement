import assert from "node:assert/strict";
import {test} from "node:test";
import {installPDFThumbnails} from "../../static/submissions/pdf_thumbnails.mjs";

class Element {
    constructor(tag = "div") {
        this.tagName = tag;
        this.children = [];
        this.attributes = new Map();
        this.dataset = {};
        this.style = {};
        this.handlers = new Map();
        this.scrollTop = 0;
        this.clientHeight = 500;
        this.hidden = true;
        this.classes = new Set();
        this.classList = {toggle: (name, value) => value ? this.classes.add(name) : this.classes.delete(name)};
    }
    append(...nodes) {
        for (const node of nodes) {
            if (node.tagName === "fragment") this.append(...node.children);
            else { this.children.push(node); node.parent = this; }
        }
    }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    removeAttribute(name) { this.attributes.delete(name); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    set tabIndex(value) { this.setAttribute("tabindex", value); }
    get tabIndex() { return Number(this.getAttribute("tabindex")); }
    addEventListener(type, handler) { this.handlers.set(type, handler); }
    removeEventListener(type) { this.handlers.delete(type); }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    closest() { return this.dataset.pdfThumbnailPage ? this : this.parent?.closest() ?? null; }
    focus() { document.activeElement = this; }
    get offsetTop() { return (this.parent?.children.indexOf(this) ?? 0) * 202; }
    get offsetHeight() { return 198; }
    getContext() { return {}; }
    emit(type, event = {}) { this.handlers.get(type)?.({target: this, preventDefault() {}, ...event}); }
}

function setup({pages = 50, desktop = true, deferred = false, fail = new Set()} = {}) {
    globalThis.document = {
        activeElement: null,
        createElement: tag => new Element(tag),
        createDocumentFragment: () => new Element("fragment"),
    };
    const media = new Element();
    media.matches = desktop;
    globalThis.window = {devicePixelRatio: 2, matchMedia: () => media};
    let observer;
    globalThis.IntersectionObserver = class {
        constructor(callback) { this.callback = callback; this.observed = new Set(); observer = this; }
        observe(node) { this.observed.add(node); }
        disconnect() { this.observed.clear(); }
    };
    const sidebar = new Element();
    const list = new Element();
    sidebar.append(list);
    const toggle = new Element("button");
    const container = new Element();
    const requests = [];
    const tasks = [];
    let active = 0;
    let peak = 0;
    const pdf = {
        numPages: pages,
        async getPage(number) {
            requests.push(number);
            if (fail.has(number)) throw new Error("Unreadable page");
            return {
                rotate: 0,
                getViewport({scale, rotation}) {
                    const [width, height] = rotation % 180 ? [842, 595] : [595, 842];
                    return {width: width * scale, height: height * scale};
                },
                render(options) {
                    active++;
                    peak = Math.max(active, peak);
                    let resolve, reject;
                    const promise = new Promise((done, error) => { resolve = done; reject = error; });
                    const task = {options, promise: promise.finally(() => active--), cancel: () => reject(new Error("Canceled")), resolve};
                    tasks.push(task);
                    if (!deferred) resolve();
                    return task;
                },
            };
        },
    };
    const viewer = {currentPageNumber: 1};
    const api = installPDFThumbnails({sidebar, list, toggle, container, viewer, annotationMode: 0});
    return {
        api, pdf, viewer, sidebar, list, toggle, container, media, requests, tasks,
        peak: () => peak,
        visible(...numbers) { observer.callback(numbers.map(number => ({target: list.children[number - 1], isIntersecting: true}))); },
        allHidden() { observer.callback(list.children.map(target => ({target, isIntersecting: false}))); },
        observerCount: () => observer.observed.size,
        button: number => list.children[number - 1],
        ready: number => list.children[number - 1].children[0].children[0]?.tagName === "canvas",
    };
}

async function flush() { for (let i = 0; i < 20; i++) await new Promise(setImmediate); }

test("all pages get buttons, but only near-visible thumbnails request/render pages", async () => {
    const h = setup();
    h.api.setDocument(h.pdf);
    assert.equal(h.list.children.length, 50);
    assert.equal(h.sidebar.hidden, false);
    assert.equal(h.requests.length, 0);
    h.visible(1, 2, 3);
    await flush();
    assert.deepEqual(h.requests, [1, 2, 3]);
    assert.equal(h.peak(), 1);
    assert.equal(h.ready(1), true);
    assert.equal(h.ready(4), false);
    assert.equal(h.button(1).getAttribute("aria-current"), "page");
    assert.equal(h.tasks[0].options.annotationMode, 0);
});

test("clicking a thumbnail changes only the same viewer page and restores PDF focus", () => {
    const h = setup();
    h.api.setDocument(h.pdf);
    h.list.emit("click", {target: h.button(25)});
    assert.equal(h.viewer.currentPageNumber, 25);
    assert.equal(document.activeElement, h.container);
    assert.equal(h.button(25).tabIndex, 0);
    assert.equal(h.sidebar.hidden, false);
});

test("current marker and sidebar scrolling follow page changes without moving PDF scroll", () => {
    const h = setup();
    h.api.setDocument(h.pdf);
    h.container.scrollTop = 123;
    h.api.setCurrentPage(40);
    assert.equal(h.button(1).getAttribute("aria-current"), null);
    assert.equal(h.button(40).getAttribute("aria-current"), "page");
    assert.ok(h.list.scrollTop > 0);
    assert.equal(h.container.scrollTop, 123);
    h.api.setCurrentPage(999);
    assert.equal(h.button(40).getAttribute("aria-current"), "page");
});

test("mobile starts closed and choosing a thumbnail closes it, not the document", () => {
    const h = setup({desktop: false});
    h.api.setDocument(h.pdf);
    assert.equal(h.sidebar.hidden, true);
    h.toggle.emit("click");
    assert.equal(h.sidebar.hidden, false);
    h.list.emit("click", {target: h.button(10)});
    assert.equal(h.sidebar.hidden, true);
    assert.equal(h.viewer.currentPageNumber, 10);
    assert.equal(document.activeElement, h.container);
    assert.equal(h.toggle.getAttribute("aria-expanded"), "false");
});

test("closing cancels in-flight rendering and cannot install stale pixels", async () => {
    const h = setup({deferred: true});
    h.api.setDocument(h.pdf);
    h.visible(1, 2);
    await flush();
    h.toggle.emit("click");
    await flush();
    assert.equal(h.ready(1), false);
    assert.deepEqual(h.requests, [1]);
    assert.equal(h.observerCount(), 0);
    h.toggle.emit("click");
    h.visible(1);
    await flush();
    h.tasks.at(-1).resolve();
    await flush();
    assert.equal(h.ready(1), true);
});

test("rotation invalidates cached/in-flight thumbnails and uses the same PDF document", async () => {
    const h = setup({deferred: true});
    h.api.setDocument(h.pdf);
    h.visible(1);
    await flush();
    h.api.setRotation(90);
    await flush();
    h.tasks.at(-1).resolve();
    await flush();
    assert.equal(h.ready(1), true);
    assert.ok(h.tasks.at(-1).options.viewport.width > h.tasks.at(-1).options.viewport.height);
    assert.deepEqual(h.requests, [1, 1]);
    assert.equal(h.peak(), 1);
});

test("thumbnail failure stays local, preserves navigation and can retry after reopening", async () => {
    const fail = new Set([2]);
    const h = setup({fail});
    h.api.setDocument(h.pdf);
    h.visible(2, 3);
    await flush();
    assert.equal(h.button(2).children[0].children[0].textContent, "Preview unavailable");
    assert.equal(h.ready(3), true);
    h.list.emit("click", {target: h.button(2)});
    assert.equal(h.viewer.currentPageNumber, 2);
    h.toggle.emit("click");
    fail.clear();
    h.toggle.emit("click");
    h.visible(2);
    await flush();
    assert.equal(h.ready(2), true);
});

test("off-screen canvas cache is bounded and evicted pages render again", async () => {
    const h = setup();
    h.api.setDocument(h.pdf);
    for (let number = 1; number <= 35; number++) {
        h.allHidden();
        h.visible(number);
        await flush();
    }
    assert.ok(h.list.children.filter((_, index) => h.ready(index + 1)).length <= 24);
    assert.equal(h.ready(2), false);
    h.allHidden();
    h.visible(2);
    await flush();
    assert.equal(h.ready(2), true);
});

test("keyboard focus moves inside the sidebar without changing the selected PDF page", () => {
    const h = setup();
    h.api.setDocument(h.pdf);
    h.button(1).focus();
    h.sidebar.emit("keydown", {target: h.button(1), key: "End"});
    assert.equal(document.activeElement, h.button(50));
    assert.equal(h.viewer.currentPageNumber, 1);
    h.api.setCurrentPage(2);
    assert.equal(document.activeElement, h.button(50));
    h.sidebar.emit("keydown", {target: h.button(50), key: "Escape"});
    assert.equal(h.sidebar.hidden, true);
    assert.equal(document.activeElement, h.toggle);
});

test("breakpoint transitions update the panel and never navigate the PDF", () => {
    const h = setup();
    h.api.setDocument(h.pdf);
    h.media.emit("change", {matches: false});
    assert.equal(h.sidebar.hidden, true);
    h.media.emit("change", {matches: true});
    assert.equal(h.sidebar.hidden, false);
    assert.equal(h.viewer.currentPageNumber, 1);
});

test("one-page PDFs and repeated current-page clicks remain valid", () => {
    const h = setup({pages: 1});
    h.api.setDocument(h.pdf);
    h.list.emit("click", {target: h.button(1)});
    assert.equal(h.viewer.currentPageNumber, 1);
    assert.equal(h.list.children.length, 1);
    assert.equal(h.button(1).getAttribute("aria-current"), "page");
});

test("the component cannot switch to another document and teardown blocks late work", async () => {
    const h = setup({deferred: true});
    h.api.setDocument(h.pdf);
    h.api.setDocument({numPages: 2, getPage() { throw new Error("Wrong PDF"); }});
    assert.equal(h.list.children.length, 50);
    h.visible(1);
    await flush();
    h.api.destroy();
    await flush();
    h.api.setDocument(h.pdf);
    assert.equal(h.list.children.length, 0);
    assert.equal(h.toggle.handlers.size, 0);
    assert.equal(h.media.handlers.size, 0);
    assert.equal(h.observerCount(), 0);
});

test("a page proxy resolving after teardown cannot start rendering or install a preview", async () => {
    const h = setup();
    let resolve;
    h.pdf.getPage = () => new Promise(done => { resolve = done; });
    h.api.setDocument(h.pdf);
    h.visible(1);
    h.api.destroy();
    resolve({render() { throw new Error("Late page must not render"); }});
    await flush();
    assert.equal(h.list.children.length, 0);
    assert.equal(h.tasks.length, 0);
});
