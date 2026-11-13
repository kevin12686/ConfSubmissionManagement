(function () {
    "use strict";

    function fieldShell(control) {
        return control.closest("[data-cfm-field-shell]") || control.closest(".mb-3, .form-check");
    }

    function setFieldState(control, changed) {
        const shell = fieldShell(control);
        if (!shell) {
            return;
        }
        shell.classList.toggle("cfm-edit-field-changed", changed);
        let marker = shell.querySelector(":scope > .cfm-edit-field-marker");
        if (changed && !marker) {
            marker = document.createElement("span");
            marker.className = "cfm-edit-field-marker";
            marker.textContent = control.type === "file" ? "New file selected" : "Changed";
            shell.appendChild(marker);
        } else if (!changed && marker) {
            marker.remove();
        }
    }

    function initialize(form) {
        const state = window.CFMFormState.capture(form);
        const summary = form.querySelector("[data-cfm-change-summary]");
        let submitting = false;

        function refresh() {
            const changedControls = new Set(state.changed());
            state.controls.forEach(function (control) {
                const changed = changedControls.has(control);
                setFieldState(control, changed);
            });
            const changedCount = changedControls.size;
            form.dataset.cfmHasUnsavedChanges = changedCount ? "true" : "false";
            if (summary) {
                summary.textContent = changedCount
                    ? changedCount + " unsaved change" + (changedCount === 1 ? "" : "s")
                    : "No unsaved changes";
                summary.classList.toggle("text-primary", Boolean(changedCount));
                summary.classList.toggle("text-muted", !changedCount);
            }
        }

        form.addEventListener("input", refresh);
        form.addEventListener("change", refresh);
        form.addEventListener("reset", function () {
            window.setTimeout(refresh, 0);
        });
        form.addEventListener("submit", function () {
            submitting = true;
        });
        window.addEventListener("pageshow", function () {
            submitting = false;
            refresh();
        });
        window.addEventListener("beforeunload", function (event) {
            if (submitting || state.changed().length === 0) {
                return;
            }
            event.preventDefault();
            event.returnValue = "";
        });
        refresh();
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("[data-cfm-record-edit-form]").forEach(initialize);
    });
})();
