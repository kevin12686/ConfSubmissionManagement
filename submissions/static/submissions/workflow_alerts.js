(function () {
    "use strict";

    // Advisory reads only. Neither invalidation nor a successful read grants approval.
    function createReader({fetchStatus, show, unavailable, timeout = 8000}) {
        let generation = 0;
        let controller = null;
        let lastChecked = null;
        return {
            async refresh(replace = false) {
                if (controller && !replace) return;
                controller?.abort();
                const current = new AbortController();
                controller = current;
                const ticket = ++generation;
                const deadline = setTimeout(() => current.abort(), timeout);
                try {
                    const response = await fetchStatus(current.signal);
                    if (!response.ok || response.redirected) throw new Error("Status unavailable");
                    const html = await response.text();
                    if (ticket !== generation) return;
                    lastChecked = new Date();
                    show(html);
                } catch (error) {
                    if (ticket === generation) unavailable(lastChecked);
                } finally {
                    clearTimeout(deadline);
                    if (ticket === generation) controller = null;
                }
            },
            pause() {
                ++generation;
                controller?.abort();
                controller = null;
            },
        };
    }

    if (typeof module !== "undefined") module.exports = {createReader};
    if (typeof document === "undefined") return;
    const host = document.querySelector("[data-cfm-workflow-alerts]");
    if (!host) return; // Full-window PDF reader deliberately has no workflow chrome.
    const content = host.querySelector("[data-workflow-alert-content]");
    const error = host.querySelector("[data-workflow-alert-error]");
    const errorMessage = host.querySelector("[data-workflow-alert-error-message]");
    const reader = createReader({
        fetchStatus: (signal) => {
            host.setAttribute("aria-busy", "true");
            return fetch(host.dataset.cfmWorkflowAlerts, {
                signal, cache: "no-store", credentials: "same-origin",
                headers: {"Accept": "text/html"},
            });
        },
        show(html) {
            // Avoid replacing identical banners: keyboard focus must survive polling.
            if (content.innerHTML !== html) content.innerHTML = html;
            error.classList.add("d-none");
            host.removeAttribute("data-cfm-status-stale");
            host.setAttribute("aria-busy", "false");
        },
        unavailable(lastChecked) {
            if (!lastChecked) content.innerHTML = "";
            host.setAttribute("aria-busy", "false");
            host.setAttribute("data-cfm-status-stale", "true");
            errorMessage.textContent = lastChecked
                ? `Workflow status is out of date (last checked ${lastChecked.toLocaleTimeString()}). Displayed warnings may have changed.`
                : "Workflow status is unavailable. This does not mean there are no pending checks.";
            error.classList.remove("d-none");
        },
    });
    const CHANGE_KEY = "cfm:workflow-changed";
    const PENDING_KEY = "cfm:workflow-post-pending";
    let timer = null;
    let debounce = null;
    function refresh() {
        if (!document.hidden) reader.refresh(true);
    }
    function changed(broadcast = true) {
        // Obsolete in-flight reads cannot restore a warning after a newer mutation.
        reader.pause();
        clearTimeout(debounce);
        debounce = setTimeout(refresh, 80);
        if (broadcast) {
            try { localStorage.setItem(CHANGE_KEY, `${Date.now()}:${Math.random()}`); }
            catch (error) { /* Polling remains available when storage is disabled. */ }
        }
    }
    function resume() {
        clearInterval(timer);
        timer = null;
        if (document.hidden) {
            clearTimeout(debounce);
            reader.pause();
            return;
        }
        refresh();
        timer = setInterval(() => reader.refresh(), 15000);
    }
    host.querySelector("[data-workflow-alert-retry]").addEventListener("click", refresh);
    document.addEventListener("submit", (event) => {
        if (event.target.method?.toLowerCase() !== "post") return;
        try { sessionStorage.setItem(PENDING_KEY, "1"); } catch (error) { /* Optional. */ }
    });
    document.addEventListener("htmx:afterRequest", (event) => {
        if (event.detail?.requestConfig?.verb?.toLowerCase() !== "post") return;
        try { sessionStorage.removeItem(PENDING_KEY); } catch (error) { /* Optional. */ }
        // Even an uncertain response needs a fresh read, not an assumption of rollback.
        changed();
    });
    document.addEventListener("htmx:historyRestore", refresh);
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("focus", () => changed(false));
    window.addEventListener("pageshow", (event) => { if (event.persisted) resume(); });
    window.addEventListener("pagehide", () => {
        clearInterval(timer);
        clearTimeout(debounce);
        reader.pause();
    });
    window.addEventListener("storage", (event) => { if (event.key === CHANGE_KEY) changed(false); });
    try {
        if (sessionStorage.getItem(PENDING_KEY)) {
            sessionStorage.removeItem(PENDING_KEY);
            changed();
        }
    } catch (error) { /* Initial refresh does not require browser storage. */ }
    resume();
})();
