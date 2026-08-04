(function () {
    "use strict";

    function controlValue(control) {
        if (control.type === "checkbox" || control.type === "radio") {
            return control.checked ? "checked" : "unchecked";
        }
        if (control.type === "file") {
            return Array.from(control.files || []).map(function (file) {
                return [file.name, file.size, file.lastModified].join(":");
            }).join("|");
        }
        return control.value;
    }

    function eligibleControls(form) {
        return Array.from(form.querySelectorAll("input, select, textarea")).filter(function (control) {
            return !["hidden", "submit", "button"].includes(control.type) && !control.disabled;
        });
    }

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
        const controls = eligibleControls(form);
        const initial = new Map(controls.map(function (control) {
            return [control, controlValue(control)];
        }));
        const summary = form.querySelector("[data-cfm-change-summary]");
        let submitting = false;

        function refresh() {
            let changedCount = 0;
            controls.forEach(function (control) {
                const changed = controlValue(control) !== initial.get(control);
                setFieldState(control, changed);
                if (changed) {
                    changedCount += 1;
                }
            });
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
        window.addEventListener("beforeunload", function (event) {
            if (submitting || form.dataset.cfmHasUnsavedChanges !== "true") {
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
