import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {runInNewContext} from "node:vm";

const script = readFileSync(new URL("../../static/submissions/image_loader.js", import.meta.url), "utf8");

function fixture() {
    class Element extends EventTarget {
        dataset = {};
        attributes = new Map();
        classList = {add() {}, remove() {}};
        append() {}
        appendChild() {}
        setAttribute(name, value) { this.attributes.set(name, value); }
        getAttribute(name) { return this.attributes.get(name) || null; }
        removeAttribute(name) { this.attributes.delete(name); }
        set src(value) { this.setAttribute("src", value); }
    }
    const container = new Element(), image = new Element(), revoked = [], window = {};
    container.querySelector = () => image;
    runInNewContext(script, {
        window, AbortController, Event,
        document: {readyState: "loading", addEventListener() {}, createElement: () => new Element()},
        URL: {createObjectURL: blob => `blob:${blob}`, revokeObjectURL: url => revoked.push(url)},
    });
    return {container, image, revoked, loader: window.CFMImageLoader};
}

test("server-backed images still replace their URL directly", () => {
    const {container, image, loader} = fixture();
    loader.load(container, "/original.png");
    loader.load(container, "/corrected.png");
    assert.equal(image.getAttribute("src"), "/corrected.png");
});

test("a superseded memory render cannot restore old pixels and reset releases the URL", async () => {
    const {container, image, revoked, loader} = fixture();
    let resolveOld, signal;
    loader.load(container, active => {
        signal = active;
        return new Promise(resolve => { resolveOld = resolve; });
    });
    loader.load(container, async () => "current");
    assert.ok(signal.aborted);
    resolveOld("obsolete");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(image.getAttribute("src"), "blob:current");
    loader.reset(container);
    assert.equal(image.getAttribute("src"), null);
    assert.deepEqual(revoked, ["blob:current"]);
});
