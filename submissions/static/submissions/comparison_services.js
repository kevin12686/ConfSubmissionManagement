(function () {
    "use strict";
    const root = document.querySelector("[data-comparison-services]");
    if (!root) return;
    const query = selector => root.querySelector(selector);
    const refresh = query("[data-services-refresh]");
    const checked = query("[data-services-checked]");
    const labels = {ready: "Ready", busy: "Busy", unreachable: "Unreachable", not_ready: "Not ready",
        incompatible: "Incompatible", error: "Access / response error", configuration_error: "Configuration error",
        not_configured: "Not configured", embedded: "Embedded mode", unused: "Not used in embedded mode"};
    let timer = null, controller = null, running = false, stopped = false;
    let observations = {};
    function text(element, value) { if (element.textContent !== value) element.textContent = value; }
    function renderMetadata(name, service, appVersion) {
        const current = service.worker_version || Object.keys(service.settings || {}).length;
        const metadata = current ? service : service.last_metadata || {};
        const stale = !current && Boolean(metadata.worker_version || Object.keys(metadata.settings || {}).length);
        const row = query(`[data-service="${name}"]`);
        const version = metadata.worker_version;
        text(row.querySelector("[data-service-version]"), ["not_configured", "unused"].includes(service.state) ? "" : version ? `v${version}${stale ? " · last seen" : ""}` : "Version unavailable");
        const detail = (key, value) => text(query(`[data-services-${name}-${key}]`), value);
        detail("version", version ? `v${version}` : "Unavailable");
        const notes = [];
        if (stale) notes.push("Last seen settings; current worker could not be checked.");
        else if (service.backend_configured) notes.push("Configured embedded settings; not an independently validated worker.");
        else if (current) notes.push("Effective settings from the worker's last successful runtime validation.");
        else notes.push("Version or settings were not reported by this service.");
        if (!stale && version && appVersion && version !== appVersion) notes.push("Release differs from CFM; compatibility is checked separately.");
        detail("metadata-note", notes.join(" "));
        const settings = metadata.settings || {};
        for (const key of ["det", "rec", "parallel_pages", "threads", "timeout_seconds", "gpu_idle_seconds"]) {
            let value = settings[key];
            if (value === undefined) value = "Unavailable";
            else if (key === "det" || key === "rec") value = value === "medium" ? "Medium" : "Small";
            else if (key === "timeout_seconds") value = `${value} seconds`;
            else if (key === "gpu_idle_seconds") value = (service.backend || service.last_backend) === "cpu" ? "Not applicable (CPU)" : value === 0 ? "Disabled (per-job release)" : `${value} seconds`;
            detail(key, String(value));
        }
    }
    function render(result) {
        if (result.checking) { checked.textContent = "Service check in progress…"; return; }
        for (const name of ["remote", "local"]) {
            const service = result[name];
            if (!service || !(service.state in labels)) throw new Error("Invalid service state");
            const row = query(`[data-service="${name}"]`);
            const backend = service.backend === "cuda" ? "GPU" : service.backend === "cpu" ? "CPU" : "";
            const previous = service.last_backend === "cuda" ? "GPU" : service.last_backend === "cpu" ? "CPU" : "";
            text(row.querySelector("[data-service-backend]"), backend ? backend + (service.backend_configured ? " · configured" : "") : previous ? `${previous} · last seen` : "Unknown");
            const state = row.querySelector("[data-service-state]");
            state.dataset.serviceState = service.state;
            text(state, labels[service.state]);
            query(`[data-services-${name}-message]`).textContent = service.message || labels[service.state];
            renderMetadata(name, service, result.app_version);
            observations[name] = service;
        }
        text(query("[data-services-app-version]"), result.app_version ? `v${result.app_version}` : "version unavailable");
        const time = new Date(result.checked_at);
        checked.textContent = Number.isNaN(time.getTime()) ? "Check time unavailable" : `Checked ${time.toLocaleTimeString()}`;
    }
    async function update() {
        window.clearTimeout(timer);
        if (running || stopped || document.hidden || !root.isConnected) return;
        running = true; refresh.disabled = true;
        controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 7000);
        try {
            const response = await fetch(root.dataset.servicesUrl, {headers: {Accept: "application/json"}, credentials: "same-origin", signal: controller.signal});
            if (!response.ok) throw new Error("Service status unavailable");
            const result = await response.json();
            if (!document.hidden && !stopped) render(result);
        } catch (error) {
            if (!document.hidden && !stopped) {
                checked.textContent = "Status update unavailable · retrying";
                root.querySelectorAll("[data-service-state]").forEach(state => {state.dataset.serviceState = "error"; state.textContent = "Status unavailable";});
                for (const name of ["remote", "local"]) query(`[data-services-${name}-message]`).textContent = "The latest service status could not be retrieved. Recheck before starting a comparison.";
                root.querySelectorAll("[data-service-backend]").forEach(backend => {
                    if (["GPU", "CPU"].includes(backend.textContent)) backend.textContent += " · last seen";
                });
                for (const name of ["remote", "local"]) {
                    const previous = observations[name] || {};
                    renderMetadata(name, {last_backend: previous.backend || previous.last_backend,
                        last_metadata: previous.worker_version || Object.keys(previous.settings || {}).length ? previous : previous.last_metadata});
                }
            }
        } finally {
            window.clearTimeout(timeout); running = false; refresh.disabled = false; controller = null;
            if (!document.hidden && !stopped && root.isConnected) timer = window.setTimeout(update, 10000);
        }
    }
    refresh.addEventListener("click", update);
    query("[data-services-details-toggle]").addEventListener("click", event => {
        const details = query("[data-services-details]"); details.hidden = !details.hidden;
        event.currentTarget.setAttribute("aria-expanded", String(!details.hidden));
    });
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) { window.clearTimeout(timer); controller?.abort(); }
        else update();
    });
    window.addEventListener("pagehide", () => { stopped = true; window.clearTimeout(timer); controller?.abort(); });
    window.addEventListener("pageshow", () => { stopped = false; update(); });
    update();
})();
