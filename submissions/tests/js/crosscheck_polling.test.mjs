import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script = readFileSync(new URL("../../static/submissions/crosscheck_service.js", import.meta.url), "utf8");

function setup(fetchResult = async () => ({ok: true, json: async () => ({status: "running", poll_enabled: true, progress: {}})}), withPanel = false) {
    let now = 0;
    let sequence = 0;
    let requests = 0;
    let reloads = 0;
    const timers = new Map();
    const listeners = new Map();
    const formListeners = new Map();
    const countdown = {hidden: true, textContent: ""};
    const message = {textContent: ""};
    const details = [{id: "history", open: true}, {id: "technical", open: false}];
    const panel = {innerHTML: "old", querySelectorAll: selector => selector.includes('[open]') ? details.filter(el => el.open) : details};
    const retryInput = {value: "23"};
    const scrolls = [];
    let processed = 0;
    const form = {addEventListener: (name, handler) => formListeners.set(name, handler)};
    const job = {
        dataset: {status: "running"},
        querySelector: selector => ({
            "[data-job-refresh]": form,
            "[data-job-countdown]": countdown,
            "[data-job-message]": message,
            "[data-job-panel]": withPanel ? panel : null,
        })[selector],
    };
    const document = {
        hidden: false,
        querySelector: selector => selector === "[data-crosscheck-job]" ? job : null,
        addEventListener: (name, handler) => listeners.set(name, handler),
    };
    runInNewContext(script, {
        document,
        window: {scrollX: 0, scrollY: 410, scrollTo: position => scrolls.push(position), htmx: {process: () => processed++}},
        Date: {now: () => now},
        FormData: class {set() {}},
        location: {href: "http://localhost/batch/", reload: () => reloads++},
        fetch: (...args) => { requests++; return fetchResult(...args); },
        setTimeout: (callback, delay) => {
            timers.set(++sequence, {callback, due: now + delay});
            return sequence;
        },
        clearTimeout: id => timers.delete(id),
    });
    return {
        countdown, message, panel, details, retryInput, scrolls,
        processed: () => processed,
        requests: () => requests,
        reloads: () => reloads,
        async advance(ms) {
            const end = now + ms;
            while (true) {
                const next = [...timers].sort((a, b) => a[1].due - b[1].due)[0];
                if (!next || next[1].due > end) break;
                now = next[1].due;
                timers.delete(next[0]);
                next[1].callback();
                await new Promise(setImmediate);
            }
            now = end;
        },
        visibility(hidden) {
            document.hidden = hidden;
            listeners.get("visibilitychange")();
        },
        submit() { formListeners.get("submit")(); },
    };
}

test("counts down and schedules the next check only after a successful response", async () => {
    const page = setup();
    assert.equal(page.countdown.hidden, false);
    assert.equal(page.countdown.textContent, "Next update in 15s");
    await page.advance(14000);
    assert.equal(page.countdown.textContent, "Next update in 1s");
    assert.equal(page.requests(), 0);
    await page.advance(1000);
    assert.equal(page.requests(), 1);
    assert.equal(page.countdown.textContent, "Next update in 15s");
});

test("progress replaces only the results panel and retains details, scroll and typed retry inputs", async () => {
    const page = setup(async () => ({ok: true, json: async () => ({status: "partial", poll_enabled: false,
        panel_html: "new results", message: "One new report is ready."})}), true);
    await page.advance(15000);
    assert.equal(page.panel.innerHTML, "new results");
    assert.equal(page.details[0].open, true);
    assert.equal(page.details[1].open, false);
    assert.equal(page.retryInput.value, "23");
    assert.equal(page.scrolls[0].top, 410);
    assert.equal(page.processed(), 1);
    assert.equal(page.reloads(), 0);
    assert.equal(page.message.textContent, "One new report is ready.");
    await page.advance(60000);
    assert.equal(page.requests(), 1);
});

test("hidden tabs do not poll and resume with one fresh countdown", async () => {
    const page = setup();
    page.visibility(true);
    await page.advance(60000);
    assert.equal(page.requests(), 0);
    assert.match(page.countdown.textContent, /paused/);
    page.visibility(false);
    assert.equal(page.countdown.textContent, "Next update in 15s");
    await page.advance(15000);
    assert.equal(page.requests(), 1);
});

test("long requests show checking without launching overlapping requests", async () => {
    const page = setup(() => new Promise(() => {}));
    await page.advance(15000);
    assert.equal(page.countdown.textContent, "Checking progress…");
    page.visibility(true);
    page.visibility(false);
    await page.advance(60000);
    assert.equal(page.requests(), 1);
});

test("errors stop automatic updates, including after visibility changes", async () => {
    const page = setup(async () => { throw new Error("Connection lost"); });
    await page.advance(15000);
    assert.match(page.countdown.textContent, /paused/);
    assert.match(page.message.textContent, /Connection lost/);
    page.visibility(true);
    page.visibility(false);
    await page.advance(60000);
    assert.equal(page.requests(), 1);
});

test("completed jobs stop polling without reloading or losing the retry form", async () => {
    const page = setup(async () => ({ok: true, json: async () => ({status: "ready"})}));
    await page.advance(60000);
    assert.equal(page.reloads(), 0);
    assert.match(page.countdown.textContent, /Review available/);
    assert.equal(page.requests(), 1);
    const manual = setup();
    manual.submit();
    await manual.advance(30000);
    assert.equal(manual.requests(), 0);
    assert.equal(manual.countdown.textContent, "Checking progress…");
});
