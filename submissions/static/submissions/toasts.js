(function () {
    "use strict";

    const titles = {success: "Updated", info: "Information", warning: "Attention", error: "Action failed"};
    function normalizeLevel(level) {
        if (level === "danger") return "error";
        return Object.hasOwn(titles, level) ? level : "info";
    }
    if (typeof module !== "undefined") module.exports = {normalizeLevel};
    if (typeof document === "undefined") return;
    const container = document.querySelector("[data-cfm-toast-container]");
    const template = document.getElementById("cfm-toast-template");
    if (!container || !template) return;
    const initialized = new WeakSet();
    let positionFrame = null;
    function position() {
        positionFrame = null;
        const headerBottom = document.querySelector(".cfm-app-header")?.getBoundingClientRect().bottom || 0;
        container.style.top = `${Math.max(12, headerBottom + 12)}px`;
    }
    function schedulePosition() {
        if (positionFrame === null) positionFrame = window.requestAnimationFrame(position);
    }
    function dismiss(element) {
        element.classList.remove("show");
        window.setTimeout(() => element.remove(), 150);
    }
    function initialize(element) {
        if (initialized.has(element)) return;
        initialized.add(element);
        element.querySelector("[data-cfm-toast-close]").addEventListener("click", () => dismiss(element));
        if (element.dataset.bsAutohide === "true") {
            window.setTimeout(() => dismiss(element), Number(element.dataset.bsDelay || 5000));
        }
    }
    function show({level, message, record = ""}) {
        if (!message) return;
        level = normalizeLevel(level);
        const element = template.content.firstElementChild.cloneNode(true);
        element.className = `toast show cfm-toast cfm-toast-${level}`;
        element.setAttribute("role", ["error", "warning"].includes(level) ? "alert" : "status");
        element.setAttribute("aria-live", ["error", "warning"].includes(level) ? "assertive" : "polite");
        element.dataset.cfmToastLevel = level;
        element.dataset.bsAutohide = ["success", "info"].includes(level) ? "true" : "false";
        element.dataset.bsDelay = level === "success" ? "5000" : "7000";
        element.querySelector("[data-cfm-toast-title]").textContent = titles[level];
        element.querySelector("[data-cfm-toast-message]").textContent = message;
        const identity = element.querySelector("[data-cfm-toast-record]");
        identity.textContent = record;
        identity.hidden = !record;
        container.appendChild(element);
        initialize(element);
        position();
    }
    function presentFeedback(root) {
        const nodes = [...root.querySelectorAll("[data-cfm-feedback]")];
        if (root.matches?.("[data-cfm-feedback]")) nodes.unshift(root);
        for (const node of nodes) {
            if (node.dataset.cfmFeedbackPresented === "true") continue;
            show({level: node.dataset.cfmFeedbackLevel, message: node.textContent.trim(), record: node.dataset.cfmFeedbackRecord});
            node.dataset.cfmFeedbackPresented = "true";
            // Validation remains beside its evidence; successful saves use one
            // consistent transient surface, even if the row left its filter.
            if (["success", "info"].includes(normalizeLevel(node.dataset.cfmFeedbackLevel))) node.hidden = true;
        }
    }
    container.querySelectorAll("[data-cfm-toast]").forEach(initialize);
    position();
    document.addEventListener("DOMContentLoaded", () => presentFeedback(document));
    document.addEventListener("htmx:afterSwap", function (event) {
        // outerHTML swaps can report the detached old target, not its replacement.
        const target = event.detail?.target;
        const current = target?.id ? document.getElementById(target.id) : target;
        presentFeedback(!current || current.isConnected === false ? document : current);
    });
    window.addEventListener("scroll", schedulePosition, {passive: true});
    window.addEventListener("resize", schedulePosition);
    document.addEventListener("shown.bs.collapse", schedulePosition);
    document.addEventListener("hidden.bs.collapse", schedulePosition);
    window.addEventListener("pageshow", schedulePosition);
    window.addEventListener("pagehide", () => {
        if (positionFrame !== null) window.cancelAnimationFrame(positionFrame);
        positionFrame = null;
    });
    window.CFMToast = {show};
})();
