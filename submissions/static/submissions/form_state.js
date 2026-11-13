(function () {
    "use strict";

    function controlValue(control) {
        if (control.type === "checkbox" || control.type === "radio") {
            return control.checked ? "checked" : "unchecked";
        }
        if (control.type === "file") {
            return Array.from(control.files || [], file => [file.name, file.size, file.lastModified].join(":")).join("|");
        }
        return control.value;
    }

    function eligibleControls(form) {
        return Array.from(form.querySelectorAll("input, select, textarea")).filter(control =>
            !["hidden", "submit", "button", "reset"].includes(control.type) && !control.disabled);
    }

    function capture(form) {
        const controls = eligibleControls(form);
        const initial = new Map(controls.map(control => [control, controlValue(control)]));
        return {
            controls,
            changed: () => controls.filter(control => controlValue(control) !== initial.get(control)),
        };
    }

    const api = {controlValue, eligibleControls, capture};
    if (typeof module !== "undefined") module.exports = api;
    if (typeof window !== "undefined") window.CFMFormState = api;
})();
