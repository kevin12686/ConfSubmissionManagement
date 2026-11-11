(function () {
    "use strict";
    const requests = new Map();
    function render(panel, result) {
        const query = selector => panel.querySelector(selector);
        const complete = result.state === "complete";
        query("[data-upload-comparison-spinner]").hidden = result.state !== "loading";
        query("[data-upload-comparison-retry]").hidden = !result.retryable;
        query("[data-upload-comparison-retry]").textContent = result.state === "awaiting_choice"
            ? result.existing_local ? "Check existing comparison" : "Recheck remote and start" : "Retry comparison";
        const local = query("[data-upload-comparison-local]");
        local.hidden = result.state !== "awaiting_choice" || !result.local_confirmation;
        local.dataset.confirmation = result.local_confirmation || "";
        const execution = query("[data-upload-comparison-execution]");
        execution.hidden = !complete || !result.execution;
        execution.textContent = result.execution ? `Compared by ${result.execution.label}` : "";
        query("[data-upload-comparison-open]").hidden = !complete;
        for (const kind of ["warning", "info"]) {
            const badge = query(`[data-upload-comparison-${kind}]`);
            badge.hidden = !complete || (kind === "info" && !result.info_count);
            badge.textContent = `${kind === "warning" ? "Warning" : "Info"} ${result[`${kind}_count`] || 0}`;
        }
        query("[data-upload-comparison-message]").textContent = complete
            ? (result.limited ? "Comparison limited; review the full PDFs." : !result.text_available ? "Text comparison unavailable; inspect both PDFs." : result.uncertain_count ? `${result.uncertain_count} text/formula region(s) could not be read reliably. Open comparison to check both PDFs.` : "Text comparison completed.")
            : result.state === "loading" ? "Files saved. Comparing Original → Corrected…"
                : `Files saved. ${result.message}`;
        if (complete) {
            const url = new URL(result.url, window.location.origin);
            url.searchParams.set("next", panel.dataset.comparisonReturn);
            query("[data-upload-comparison-open]").href = url.href;
        }
    }
    function update(token, result) {
        const entry = requests.get(token);
        entry.result = result;
        document.querySelectorAll("[data-upload-comparison]").forEach(panel => {
            if (panel.dataset.comparisonToken === token) render(panel, result);
        });
    }
    async function fetchResult(panel, method, retry, localConfirmation = "") {
        const params = new URLSearchParams({token: panel.dataset.comparisonToken, next: panel.dataset.comparisonReturn});
        if (retry) params.set("retry", "1");
        if (method === "POST" && localConfirmation) params.set("local_confirmation", localConfirmation);
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 120000);
        try {
            const response = await fetch(panel.dataset.comparisonUrl + (method === "GET" ? `?${params}` : ""), {
                method, credentials: "same-origin", signal: controller.signal,
                headers: {"Accept": "application/json", ...(method === "POST" ? {"X-CSRFToken": panel.dataset.comparisonCsrf, "Content-Type": "application/x-www-form-urlencoded"} : {})},
                ...(method === "POST" ? {body: params} : {}),
            });
            const result = await response.json();
            if (!result.state) throw new Error("Invalid comparison response");
            return result;
        } finally {
            window.clearTimeout(timeout);
        }
    }
    async function start(panel, retry = false, localConfirmation = "") {
        const token = panel.dataset.comparisonToken;
        const previous = requests.get(token);
        if (previous?.running || (previous && !retry)) {
            render(panel, previous.result);
            return;
        }
        requests.set(token, {running: true, result: {state: "loading"}});
        update(token, {state: "loading"});
        try {
            let result = await fetchResult(panel, retry ? "POST" : "GET", retry, localConfirmation);
            if (!retry && result.state === "pending" && panel.dataset.comparisonAutostart === "true") {
                result = await fetchResult(panel, "POST", false);
            }
            update(token, result);
        } catch (error) {
            update(token, {state: error.name === "AbortError" ? "busy" : "failed", retryable: true, message: error.name === "AbortError"
                ? "Comparison timed out or is still running. Retry to check it; no duplicate build will start."
                : "Comparison could not be loaded. Retry comparison; the saved files are unchanged."});
        } finally {
            requests.get(token).running = false;
        }
    }
    function initialize() {
        document.querySelectorAll("[data-upload-comparison]").forEach(panel => start(panel));
    }
    document.addEventListener("click", event => {
        const button = event.target.closest?.("[data-upload-comparison-retry]");
        if (button) start(button.closest("[data-upload-comparison]"), true);
        const local = event.target.closest?.("[data-upload-comparison-local]");
        if (local && local.dataset.confirmation) start(local.closest("[data-upload-comparison]"), true, local.dataset.confirmation);
    });
    document.addEventListener("htmx:load", initialize);
    window.addEventListener("pageshow", initialize);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true});
    else initialize();
})();
