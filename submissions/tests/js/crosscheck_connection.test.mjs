import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script = readFileSync(new URL("../../static/submissions/crosscheck_service.js", import.meta.url), "utf8");

function control(value = "") {
    const listeners = new Map();
    return {value, disabled: false, textContent: "", className: "", listeners,
        addEventListener: (name, handler) => listeners.set(name, handler)};
}

function setup(transport = async () => ({ok: true, json: async () => ({available: true, folders: ["Conference folder"], username: "Service account"})})) {
    const url = control("http://service:18765");
    const status = control();
    const credentialsStatus = control();
    const accountSummary = control();
    const button = control();
    const save = control();
    const remove = control();
    const folder = {...control("Conference folder"), replaceChildren(...options) { this.options = options; }};
    const ids = {id_crosscheck_service_url: url, id_crosscheck_service_folder: folder,
        "cc-web-user": control("editor"), "cc-web-password": control("private password"),
        "cc-ieee-user": control(), "cc-ieee-password": control()};
    const form = {...control(), action: "/credentials/", elements: {url: control(url.value)}};
    ids["crosscheck-secrets"] = form;
    const connection = {dataset: {crosscheckConnection: "/connection/"},
        querySelector: name => name === "button" ? button : status};
    class FormData {
        constructor() {
            this.values = new Map([["csrfmiddlewaretoken", "csrf"], ["evidence_token", "signed-settings"],
                ["url", form.elements.url.value], ["web_username", ids["cc-web-user"].value],
                ["web_password", ids["cc-web-password"].value], ["ieee_username", ids["cc-ieee-user"].value],
                ["ieee_password", ids["cc-ieee-password"].value]]);
        }
        set(key, value) { this.values.set(key, value); }
        get(key) { return this.values.get(key); }
    }
    const requests = [];
    runInNewContext(script, {
        document: {
            querySelector: selector => ({"[data-crosscheck-connection]": connection,
                "[data-crosscheck-credentials-status]": credentialsStatus,
                "[data-crosscheck-account-summary]": accountSummary})[selector] || null,
            querySelectorAll: () => [save, remove],
            getElementById: id => ids[id],
        },
        window: {}, FormData,
        Option: class {constructor(text, value) { this.text = text; this.value = value; }},
        fetch: (endpoint, options) => { requests.push({endpoint, ...options}); return transport(endpoint, options); },
    });
    return {url, status, credentialsStatus, accountSummary, button, save, remove, folder, ids, form, requests,
        testConnection: () => button.listeners.get("click")(),
        input(id, value) { ids[id].value = value; ids[id].listeners.get("input")(); },
        saveCredentials(remove = false) {
            return form.listeners.get("submit")({preventDefault() {}, submitter: remove ? {name: "remove"} : {}});
        }};
}

test("connection test sends entered credentials in POST, with no automatic request or save", async () => {
    const page = setup();
    assert.equal(page.requests.length, 0);
    await page.testConnection();
    assert.equal(page.requests.length, 1);
    assert.equal(page.requests[0].endpoint, "/connection/");
    assert.equal(page.requests[0].method, "POST");
    assert.equal(page.requests[0].body.get("url"), "http://service:18765");
    assert.equal(page.requests[0].body.get("web_password"), "private password");
    assert.equal(page.requests[0].body.get("csrfmiddlewaretoken"), "csrf");
    assert.match(page.status.textContent, /^Ready/);
    assert.equal(page.ids["cc-web-password"].value, "private password");
    assert.equal(page.button.disabled, false);
});

test("blank address never contacts a default service", async () => {
    const page = setup();
    page.input("id_crosscheck_service_url", "  ");
    await page.testConnection();
    assert.equal(page.requests.length, 0);
    assert.match(page.status.textContent, /Enter the CrossCheck service URL/);
});

test("changing address or login invalidates an in-flight folder result", async () => {
    for (const [id, value] of [["id_crosscheck_service_url", "http://other-host"], ["cc-web-password", "new password"]]) {
        let resolve;
        const page = setup(() => new Promise(done => { resolve = done; }));
        const pending = page.testConnection();
        page.input(id, value);
        resolve({ok: true, json: async () => ({available: true, folders: ["Wrong old folder"], username: "Old account"})});
        await pending;
        assert.equal(page.status.textContent, "Not checked");
        assert.equal(page.folder.options, undefined);
        assert.equal(page.button.disabled, false);
    }
});

test("credential save preserves the unsaved address and folder without navigating", async () => {
    const page = setup(async () => ({ok: true, json: async () => ({message: "Saved for this service URL.",
        account_label: "Using service default IEEE account", login_label: "Service login configured"})}));
    page.input("id_crosscheck_service_url", "http://new-service:18765");
    await page.saveCredentials();
    assert.equal(page.requests[0].endpoint, "/credentials/");
    assert.equal(page.requests[0].headers.Accept, "application/json");
    assert.equal(page.requests[0].body.get("url"), "http://new-service:18765");
    assert.equal(page.requests[0].body.get("evidence_token"), "signed-settings");
    assert.equal(page.url.value, "http://new-service:18765");
    assert.equal(page.folder.value, "Conference folder");
    assert.equal(page.ids["cc-web-password"].value, "");
    assert.match(page.accountSummary.textContent, /Service login configured/);
    assert.equal(page.status.textContent, "Not checked");
    assert.equal(page.save.disabled, false);
});

test("failed test/save keeps entered credentials for retry and restores controls", async () => {
    const page = setup(async () => ({ok: false, json: async () => ({message: "Service login required or rejected."})}));
    await page.testConnection();
    assert.match(page.status.textContent, /login required or rejected/);
    await page.saveCredentials();
    assert.match(page.credentialsStatus.textContent, /login required or rejected/);
    assert.equal(page.ids["cc-web-password"].value, "private password");
    assert.equal(page.button.disabled, false);
    assert.equal(page.save.disabled, false);
    assert.equal(page.remove.disabled, false);
});

test("remove action is explicit and clears entered credentials only after success", async () => {
    const page = setup(async () => ({ok: true, json: async () => ({message: "Removed.",
        account_label: "Using service default IEEE account", login_label: "Service login not configured"})}));
    await page.saveCredentials(true);
    assert.equal(page.requests[0].body.get("remove"), "1");
    assert.equal(page.ids["cc-web-user"].value, "");
    assert.equal(page.ids["cc-web-password"].value, "");
    assert.equal(page.url.value, "http://service:18765");
});

test("a late save response cannot erase newer credential edits", async () => {
    let resolve;
    const page = setup(() => new Promise(done => { resolve = done; }));
    const pending = page.saveCredentials();
    page.input("cc-web-password", "newer password");
    resolve({ok: true, json: async () => ({message: "Saved."})});
    await pending;
    assert.equal(page.ids["cc-web-password"].value, "newer password");
    assert.match(page.credentialsStatus.textContent, /newer edits are not saved/);
    assert.equal(page.save.disabled, false);
});
