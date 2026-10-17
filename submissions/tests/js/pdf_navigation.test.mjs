import assert from "node:assert/strict";
import {test} from "node:test";
import {installPDFNavigation} from "../../static/submissions/pdf_navigation.mjs";

globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
globalThis.getComputedStyle = () => ({lineHeight: "20px"});
globalThis.window = {addEventListener() {}, removeEventListener() {}};

function setup(t, {page = 2, height = 800, viewport = 800} = {}) {
    let now = 1000;
    let frameId = 0;
    const frames = new Map();
    const handlers = new Map();
    const windowHandlers = new Map();
    t.mock.method(window, "addEventListener", (type, handler) => windowHandlers.set(type, handler));
    t.mock.method(window, "removeEventListener", type => windowHandlers.delete(type));
    t.mock.method(performance, "now", () => now);
    t.mock.method(globalThis, "requestAnimationFrame", callback => {
        frames.set(++frameId, callback);
        return frameId;
    });
    t.mock.method(globalThis, "cancelAnimationFrame", id => frames.delete(id));
    const container = {
        scrollHeight: height, clientHeight: viewport, scrollTop: 0,
        focusCount: 0,
        focus(options) { assert.equal(options.preventScroll, true); this.focusCount++; },
        contains(node) { return node === this || node?.insidePDF === true; },
        addEventListener(type, handler, options) {
            if (type === "wheel") assert.equal(options.passive, false);
            handlers.set(type, handler);
        },
        removeEventListener(type) { handlers.delete(type); },
    };
    const viewer = {
        pdfDocument: {}, scrollMode: 3, currentPageNumber: page, pagesCount: 4,
        getPageView() { return {height: container.scrollHeight}; },
        nextPage() {
            if (this.currentPageNumber === this.pagesCount) return false;
            this.currentPageNumber++;
            return true;
        },
        previousPage() {
            if (this.currentPageNumber === 1) return false;
            this.currentPageNumber--;
            return true;
        },
    };
    const navigation = installPDFNavigation({container, viewer, singlePageMode: 3});
    function keyDown(key, extra = {}) {
        const event = {
            key, repeat: false, cancelable: true, defaultPrevented: false,
            target: {closest: () => null},
            preventDefault() { this.defaultPrevented = true; }, ...extra,
        };
        handlers.get("keydown")?.(event);
        return event.defaultPrevented;
    }
    return {
        container, viewer, navigation,
        keyDown,
        keyUp(key) { windowHandlers.get("keyup")?.({key}); },
        blur() { windowHandlers.get("blur")?.(); },
        focusOut(relatedTarget) { handlers.get("focusout")?.({relatedTarget}); },
        listenerCount() { return handlers.size + windowHandlers.size; },
        advance(ms) { now += ms; },
        flush() { for (const [id, callback] of frames) { frames.delete(id); callback(); } },
        wheel(deltaY, extra = {}) {
            const event = {
                deltaX: 0, deltaY, deltaMode: 0, cancelable: true, defaultPrevented: false,
                target: {closest: () => null},
                preventDefault() { this.defaultPrevented = true; }, ...extra,
            };
            handlers.get("wheel")?.(event);
            return event.defaultPrevented;
        },
    };
}

test("fit-page wheel turns one page, and previous enters at the bottom", t => {
    const h = setup(t);
    assert.equal(h.wheel(100), true);
    assert.equal(h.viewer.currentPageNumber, 3);
    h.flush();
    assert.equal(h.container.scrollTop, 0);
    h.advance(600);
    h.container.scrollHeight = 1800;
    assert.equal(h.wheel(-100), true);
    assert.equal(h.viewer.currentPageNumber, 2);
    h.flush();
    assert.equal(h.container.scrollTop, 1000);
});

test("long momentum stream cannot turn more than once; a new gesture can", t => {
    const h = setup(t);
    h.wheel(50);
    for (let i = 0; i < 120; i++) { h.advance(16); h.wheel(i % 2 ? 8 : 2); }
    assert.equal(h.viewer.currentPageNumber, 3);
    h.advance(300);
    h.wheel(50);
    assert.equal(h.viewer.currentPageNumber, 4);
});

test("minimum interval also blocks short separate bursts", t => {
    const h = setup(t);
    h.wheel(100);
    h.advance(300);
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 3);
    h.advance(300);
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 4);
});

test("zoomed-page scroll remains native and its edge inertia does not flip", t => {
    const h = setup(t, {height: 1600});
    assert.equal(h.wheel(100), false);
    h.container.scrollTop = 800;
    for (let i = 0; i < 20; i++) { h.advance(16); assert.equal(h.wheel(100), true); }
    assert.equal(h.viewer.currentPageNumber, 2);
    h.advance(300);
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 3);
});

test("upward panning must reach the top before a fresh gesture can go back", t => {
    const h = setup(t, {height: 1600});
    h.container.scrollTop = 400;
    assert.equal(h.wheel(-100), false);
    h.container.scrollTop = 0;
    h.advance(16);
    assert.equal(h.wheel(-100), true);
    assert.equal(h.viewer.currentPageNumber, 2);
    h.advance(300);
    h.wheel(-100);
    assert.equal(h.viewer.currentPageNumber, 1);
});

test("fractional trackpad deltas accumulate; reversal clears pending distance", t => {
    const h = setup(t);
    h.wheel(20);
    h.advance(16);
    h.wheel(-20);
    assert.equal(h.viewer.currentPageNumber, 2);
    for (let i = 0; i < 4; i++) { h.advance(16); h.wheel(-5); }
    assert.equal(h.viewer.currentPageNumber, 1);
});

test("pending tiny deltas do not accumulate across idle gestures", t => {
    const h = setup(t);
    h.wheel(30);
    h.advance(300);
    h.wheel(30);
    assert.equal(h.viewer.currentPageNumber, 2);
});

for (const [mode, delta] of [[1, 2], [2, 1]]) {
    test(`deltaMode ${mode} uses line or viewport units`, t => {
        const h = setup(t);
        h.wheel(delta, {deltaMode: mode});
        assert.equal(h.viewer.currentPageNumber, 3);
    });
}

test("horizontal, modified, noncancelable and editable events are untouched", t => {
    const h = setup(t);
    for (const extra of [
        {deltaX: 200}, {ctrlKey: true}, {metaKey: true}, {altKey: true},
        {shiftKey: true}, {cancelable: false}, {target: {closest: () => ({})}},
    ]) {
        assert.equal(h.wheel(100, extra), false);
        assert.equal(h.viewer.currentPageNumber, 2);
    }
    h.wheel(100, {defaultPrevented: true});
    assert.equal(h.viewer.currentPageNumber, 2);
});

test("horizontal tail events do not unlock a gesture that already turned", t => {
    const h = setup(t);
    h.wheel(100);
    h.advance(16);
    h.wheel(2, {deltaX: 100});
    h.advance(16);
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 3);
});

test("continuous mode and missing documents retain native wheel behavior", t => {
    const h = setup(t);
    h.viewer.scrollMode = 0;
    assert.equal(h.wheel(100), false);
    h.viewer.scrollMode = 3;
    h.viewer.pdfDocument = null;
    assert.equal(h.wheel(100), false);
    assert.equal(h.viewer.currentPageNumber, 2);
});

test("first/last page never wrap or create an invalid page", t => {
    const h = setup(t, {page: 1});
    h.wheel(-100);
    assert.equal(h.viewer.currentPageNumber, 1);
    h.advance(600);
    h.viewer.currentPageNumber = 4;
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 4);
});

test("fractional edge positions allow normal browser rounding", t => {
    const h = setup(t, {height: 1600});
    h.container.scrollTop = 799.5;
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 3);
});

test("page decorations do not consume a wheel gesture when the PDF content fits", t => {
    const h = setup(t, {height: 814});
    h.viewer.getPageView = () => ({height: 795});
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 3);
});

test("native fit updates keep the wheel turn locked without canceling entry positioning", t => {
    const h = setup(t, {height: 1600});
    h.wheel(-100);
    h.navigation.suspend();
    h.flush();
    assert.equal(h.container.scrollTop, 800);
    h.advance(16);
    h.wheel(-100);
    assert.equal(h.viewer.currentPageNumber, 1);
});

test("an idle scale/layout update does not block the first wheel gesture", t => {
    const h = setup(t);
    h.navigation.suspend();
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 3);
});

test("delayed entry positioning cannot affect another page or continuous mode", t => {
    const h = setup(t, {height: 1600});
    h.wheel(-100);
    h.viewer.currentPageNumber = 3;
    h.flush();
    assert.equal(h.container.scrollTop, 0);
    h.advance(600);
    h.wheel(-100);
    h.viewer.scrollMode = 0;
    h.flush();
    assert.equal(h.container.scrollTop, 0);
});

test("invalid deltas do not navigate", t => {
    const h = setup(t);
    for (const delta of [0, NaN, Infinity, -Infinity]) assert.equal(h.wheel(delta), false);
    assert.equal(h.viewer.currentPageNumber, 2);
});

test("destroy removes the listener and cancels queued positioning", t => {
    const h = setup(t, {height: 1600});
    h.wheel(-100);
    h.navigation.destroy();
    h.flush();
    assert.equal(h.container.scrollTop, 0);
    h.advance(600);
    assert.equal(h.wheel(100), false);
    assert.equal(h.viewer.currentPageNumber, 1);
    assert.equal(h.listenerCount(), 0);
});

test("left/right turn a page and preserve document focus", t => {
    const h = setup(t);
    assert.equal(h.keyDown("ArrowRight"), true);
    assert.equal(h.viewer.currentPageNumber, 3);
    h.keyUp("ArrowRight");
    h.keyDown("ArrowLeft");
    assert.equal(h.viewer.currentPageNumber, 2);
    assert.equal(h.container.focusCount, 2);
});

for (const key of ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "PageUp", "PageDown"]) {
    test(`${key} cannot repeatedly flip while held; releasing permits the next turn`, t => {
        const h = setup(t, {page: key.endsWith("Left") || key.endsWith("Up") ? 4 : 1});
        h.keyDown(key);
        const after = h.viewer.currentPageNumber;
        for (let i = 0; i < 20; i++) {
            h.advance(100);
            assert.equal(h.keyDown(key, {repeat: true}), true);
        }
        assert.equal(h.viewer.currentPageNumber, after);
        h.keyDown(key);
        assert.equal(h.viewer.currentPageNumber, after);
        h.keyUp(key);
        h.keyDown(key);
        assert.notEqual(h.viewer.currentPageNumber, after);
    });
}

for (const key of ["ArrowUp", "ArrowDown", "PageUp", "PageDown"]) {
    test(`${key} permits native panning, but needs a fresh press at the edge`, t => {
        const h = setup(t, {height: 1800});
        h.container.scrollTop = 400;
        assert.equal(h.keyDown(key), false);
        const up = key.endsWith("Up");
        h.container.scrollTop = up ? 0 : 1000;
        assert.equal(h.keyDown(key, {repeat: true}), true);
        assert.equal(h.viewer.currentPageNumber, 2);
        h.keyUp(key);
        h.keyDown(key);
        assert.equal(h.viewer.currentPageNumber, up ? 1 : 3);
        h.flush();
        assert.equal(h.container.scrollTop, up ? 1000 : 0);
    });
}

test("Home/End use document bounds and align to top/bottom even on the same page", t => {
    const h = setup(t, {height: 1800});
    h.keyDown("Home");
    h.flush();
    assert.equal(h.viewer.currentPageNumber, 1);
    assert.equal(h.container.scrollTop, 0);
    h.container.scrollTop = 400;
    h.keyUp("Home");
    h.keyDown("Home");
    h.flush();
    assert.equal(h.container.scrollTop, 0);
    h.keyDown("End");
    h.flush();
    assert.equal(h.viewer.currentPageNumber, 4);
    assert.equal(h.container.scrollTop, 1000);
    h.container.scrollTop = 400;
    h.keyUp("End");
    h.keyDown("End");
    h.flush();
    assert.equal(h.container.scrollTop, 1000);
});

for (const key of ["Home", "End"]) {
    test(`${key} repeats do not override navigation until the key is released`, t => {
        const h = setup(t);
        h.keyDown(key);
        h.viewer.currentPageNumber = 2;
        assert.equal(h.keyDown(key, {repeat: true}), true);
        assert.equal(h.viewer.currentPageNumber, 2);
        h.keyUp(key);
        h.keyDown(key);
        assert.equal(h.viewer.currentPageNumber, key === "Home" ? 1 : 4);
    });
}

test("continuous mode keeps native vertical keys but supports explicit page navigation", t => {
    const h = setup(t);
    h.viewer.scrollMode = 0;
    for (const key of ["ArrowUp", "ArrowDown", "PageUp", "PageDown"]) assert.equal(h.keyDown(key), false);
    h.keyDown("ArrowRight");
    assert.equal(h.viewer.currentPageNumber, 3);
    h.keyDown("Home");
    h.flush();
    assert.equal(h.viewer.currentPageNumber, 1);
    assert.equal(h.container.scrollTop, 0);
    h.keyDown("End");
    assert.equal(h.viewer.currentPageNumber, 4);
});

test("keyboard does not intercept controls, selection modifiers, composition or unrelated keys", t => {
    const h = setup(t);
    for (const extra of [
        {ctrlKey: true}, {metaKey: true}, {altKey: true}, {shiftKey: true},
        {isComposing: true}, {cancelable: false}, {target: {closest: () => ({})}},
    ]) assert.equal(h.keyDown("ArrowRight", extra), false);
    h.keyDown("ArrowRight", {defaultPrevented: true});
    for (const key of ["Tab", "Enter", "Escape", " ", "f", "+", "-"]) assert.equal(h.keyDown(key), false);
    assert.equal(h.viewer.currentPageNumber, 2);
    assert.equal(h.container.focusCount, 0);
});

test("keyboard cannot navigate while a document is absent or pages are uninitialized", t => {
    const h = setup(t);
    h.viewer.pdfDocument = null;
    assert.equal(h.keyDown("End"), false);
    h.viewer.pdfDocument = {};
    h.viewer.pagesCount = 0;
    assert.equal(h.keyDown("Home"), false);
    assert.equal(h.viewer.currentPageNumber, 2);
});

test("keyboard stays within first/last-page bounds", t => {
    const h = setup(t, {page: 1});
    h.keyDown("ArrowLeft");
    assert.equal(h.viewer.currentPageNumber, 1);
    h.viewer.currentPageNumber = 4;
    h.keyDown("ArrowRight");
    assert.equal(h.viewer.currentPageNumber, 4);
});

test("focus leaving the PDF or window blur releases held-key state", t => {
    const h = setup(t);
    h.keyDown("ArrowRight");
    h.focusOut({insidePDF: true});
    h.keyDown("ArrowRight");
    assert.equal(h.viewer.currentPageNumber, 3);
    h.focusOut(null);
    h.keyDown("ArrowRight");
    assert.equal(h.viewer.currentPageNumber, 4);
    h.keyDown("ArrowLeft");
    h.blur();
    h.keyDown("ArrowLeft");
    assert.equal(h.viewer.currentPageNumber, 2);
});

test("keyboard navigation suppresses an existing wheel tail and replaces stale positioning", t => {
    const h = setup(t, {height: 1600});
    h.wheel(-100);
    h.keyDown("ArrowRight");
    h.flush();
    assert.equal(h.viewer.currentPageNumber, 2);
    assert.equal(h.container.scrollTop, 0);
    h.advance(16);
    h.wheel(100);
    assert.equal(h.viewer.currentPageNumber, 2);
});

test("rapid opposite input uses the new page's entry, not the old page's scroll offset", t => {
    const h = setup(t, {height: 1600});
    h.container.scrollTop = 800;
    h.keyDown("ArrowRight");
    assert.equal(h.viewer.currentPageNumber, 3);
    assert.equal(h.keyDown("ArrowUp"), true);
    assert.equal(h.viewer.currentPageNumber, 2);
    h.flush();
    assert.equal(h.container.scrollTop, 800);
});

test("destroy also removes keyboard/window listeners and pending keyboard positioning", t => {
    const h = setup(t, {height: 1600});
    h.keyDown("End");
    h.navigation.destroy();
    h.flush();
    assert.equal(h.container.scrollTop, 0);
    assert.equal(h.keyDown("Home"), false);
    assert.equal(h.viewer.currentPageNumber, 4);
    assert.equal(h.listenerCount(), 0);
});
