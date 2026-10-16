(function () {
    "use strict";

    function openHistory(event) {
        const panel = event.target;
        if (panel instanceof Element && panel.matches("[data-cfm-formatting-history]")) {
            window.htmx?.trigger(panel, "cfm:history-open");
        }
    }
    document.addEventListener("shown.bs.collapse", openHistory);
    document.addEventListener("cfm:worklist-expanded", openHistory);

    document.addEventListener("click", function (event) {
        const toggle = event.target.closest?.("[data-cfm-history-toggle]");
        if (!toggle) return;
        const panel = document.getElementById(toggle.getAttribute("aria-controls"));
        if (panel) panel.dataset.revealHistory = String(toggle.getAttribute("aria-expanded") === "true");
    });
    document.addEventListener("htmx:afterSettle", function (event) {
        const panel = event.target.closest?.("[data-cfm-formatting-history]")
            || event.detail?.target?.closest?.("[data-cfm-formatting-history]");
        if (panel?.dataset.revealHistory !== "true" || !panel.classList.contains("show")) return;
        delete panel.dataset.revealHistory;
        window.CFMContextExpansion?.reveal(panel);
    });

    function loadFailed(event) {
        const panel = event.detail?.elt?.closest?.("[data-cfm-formatting-history]");
        if (!panel) return;
        panel.querySelector("[data-cfm-history-loading]")?.classList.add("d-none");
        panel.querySelector("[data-cfm-history-error]")?.classList.remove("d-none");
    }
    document.addEventListener("htmx:responseError", loadFailed);
    document.addEventListener("htmx:sendError", loadFailed);
    document.addEventListener("click", function (event) {
        const button = event.target.closest?.("[data-cfm-history-retry]");
        if (!button) return;
        const panel = button.closest("[data-cfm-formatting-history]");
        panel.querySelector("[data-cfm-history-loading]")?.classList.remove("d-none");
        panel.querySelector("[data-cfm-history-error]")?.classList.add("d-none");
        window.htmx?.trigger(panel, "cfm:history-retry");
    });
})();
