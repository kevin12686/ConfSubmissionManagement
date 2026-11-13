(function () {
    "use strict";

    function createGuard(state, confirmLeave) {
        let departing = false;
        return {
            dirty: () => state.changed().length > 0,
            confirm() {
                if (departing || !this.dirty()) return true;
                if (!confirmLeave()) return false;
                departing = true;
                return true;
            },
            submitting() { departing = true; },
            resume() { departing = false; },
            shouldBlockUnload() { return !departing && this.dirty(); },
        };
    }

    if (typeof module !== "undefined") module.exports = {createGuard};
    if (typeof document === "undefined") return;
    document.addEventListener("DOMContentLoaded", function () {
        const form = document.querySelector("[data-cfm-unsaved-guard]");
        if (!form) return;
        const guard = createGuard(window.CFMFormState.capture(form), () => window.confirm(form.dataset.cfmUnsavedGuard));
        document.addEventListener("click", function (event) {
            const link = event.target.closest?.("a[data-unsaved-check='true']");
            if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target === "_blank") return;
            if (!guard.confirm()) event.preventDefault();
        });
        // Capture before the common submit/disabling handler; a rejected departure
        // must not lock its button or be mistaken for a submitted operation.
        document.addEventListener("submit", function (event) {
            if (event.defaultPrevented) return;
            if (event.target === form) {
                guard.submitting();
            } else if (!guard.confirm()) {
                event.preventDefault();
            }
        }, true);
        window.addEventListener("beforeunload", function (event) {
            if (!guard.shouldBlockUnload()) return;
            event.preventDefault();
            event.returnValue = "";
        });
        window.addEventListener("pageshow", () => guard.resume());
        form.addEventListener("input", () => guard.resume());
        form.addEventListener("change", () => guard.resume());
    });
})();
