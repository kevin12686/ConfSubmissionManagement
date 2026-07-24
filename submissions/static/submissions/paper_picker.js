(function () {
    "use strict";

    const selector = "[data-cfm-paper-picker='true']";

    function escapeHtml(value) {
        const element = document.createElement("div");
        element.textContent = value == null ? "" : String(value);
        return element.innerHTML;
    }

    function masterOption(data) {
        const title = data.title
            ? `<div class="cfm-paper-picker-title">${escapeHtml(data.title)}</div>`
            : "";
        return (
            `<div class="cfm-paper-picker-option">` +
            `<strong class="cfm-paper-picker-id">${escapeHtml(data.paper_id)}</strong>` +
            title +
            `</div>`
        );
    }

    function processOption(data) {
        return (
            `<div class="cfm-paper-picker-option cfm-paper-picker-option-inline">` +
            `<strong class="cfm-paper-picker-id">${escapeHtml(data.paper_id)}</strong>` +
            `<span class="cfm-paper-picker-meta">Final ${escapeHtml(data.final_id)}</span>` +
            `</div>`
        );
    }

    function updateSummary(element, option) {
        const targetId = element.dataset.pickerSummaryTarget;
        if (!targetId) return;
        const target = document.getElementById(targetId);
        if (!target) return;
        if (!option || !option.paper_id) {
            target.hidden = true;
            target.replaceChildren();
            return;
        }
        const paperId = document.createElement("strong");
        paperId.textContent = option.paper_id;
        const title = document.createElement("span");
        title.textContent = option.title || "No Master Title";
        target.replaceChildren(paperId, title);
        target.hidden = false;
    }

    function buildUrl(element, query, selectedValue) {
        const url = new URL(element.dataset.pickerUrl, window.location.origin);
        url.searchParams.set("context", element.dataset.pickerContext || "master");
        if (selectedValue) {
            url.searchParams.set("selected", selectedValue);
            url.searchParams.set(
                "selected_field",
                element.dataset.pickerValueField === "paper_id" ? "paper_id" : "pk"
            );
        } else {
            url.searchParams.set("q", query);
        }
        return url;
    }

    function removeUnselectedOptions(picker) {
        const selectedValues = new Set(picker.items.map(String));
        Object.keys(picker.options).forEach(function (value) {
            if (!selectedValues.has(String(value))) {
                picker.removeOption(value, true);
            }
        });
    }

    function initializePicker(element) {
        if (!window.TomSelect || element.tomselect) return;

        const initialValue = String(element.value || "").trim();
        const valueField = element.dataset.pickerValueField || "paper_id";
        const display = element.dataset.pickerDisplay || "master";
        const summaryTarget = element.dataset.pickerSummaryTarget;
        let requestController = null;
        element.value = "";

        const picker = new window.TomSelect(element, {
            valueField: valueField,
            labelField: "paper_id",
            searchField: [],
            create: false,
            maxItems: 1,
            maxOptions: 20,
            preload: false,
            openOnFocus: false,
            closeAfterSelect: true,
            loadThrottle: 200,
            placeholder: element.dataset.pickerPlaceholder || "Type to search",
            shouldLoad: function (query) {
                return query.trim().length > 0;
            },
            load: function (query, callback) {
                removeUnselectedOptions(this);
                if (requestController) requestController.abort();
                requestController = new AbortController();
                fetch(buildUrl(element, query), {
                    headers: {"Accept": "application/json"},
                    signal: requestController.signal,
                })
                    .then(function (response) {
                        if (!response.ok) throw new Error("Paper search failed.");
                        return response.json();
                    })
                    .then(function (payload) {
                        callback(payload.results || []);
                    })
                    .catch(function (error) {
                        if (error.name !== "AbortError") callback();
                    });
            },
            onType: function () {
                removeUnselectedOptions(this);
            },
            render: {
                option: display === "process" ? processOption : masterOption,
                item: function (data) {
                    if (display === "process") return processOption(data);
                    return `<div class="cfm-paper-picker-selected">${escapeHtml(data.paper_id)}</div>`;
                },
                no_results: function () {
                    return `<div class="no-results">No matching papers</div>`;
                },
                not_loading: function () {
                    return `<div class="no-results">Type to search by Paper ID${display === "master" ? ", title, or author" : " or Final ID"}</div>`;
                },
            },
            onChange: function (value) {
                const option = value ? this.options[value] : null;
                if (summaryTarget) updateSummary(element, option);
                if (display === "process" && option && option.url) {
                    window.location.assign(option.url);
                }
            },
        });

        picker.wrapper.classList.add("cfm-paper-picker");
        if (initialValue) {
            fetch(buildUrl(element, "", initialValue), {
                headers: {"Accept": "application/json"},
            })
                .then(function (response) {
                    if (!response.ok) throw new Error("Selected paper could not be loaded.");
                    return response.json();
                })
                .then(function (payload) {
                    const option = (payload.results || [])[0];
                    if (!option) return;
                    picker.addOption(option);
                    picker.setValue(String(option[valueField]), true);
                    if (summaryTarget) updateSummary(element, option);
                })
                .catch(function () {
                    picker.clear(true);
                });
        }
    }

    function initializePaperPickers(root) {
        const scope = root && root.querySelectorAll ? root : document;
        if (scope.matches && scope.matches(selector)) initializePicker(scope);
        scope.querySelectorAll(selector).forEach(initializePicker);
    }

    document.addEventListener("DOMContentLoaded", function () {
        initializePaperPickers(document);
    });
    document.addEventListener("htmx:load", function (event) {
        initializePaperPickers(event.detail && event.detail.elt ? event.detail.elt : document);
    });
    document.addEventListener("htmx:beforeCleanupElement", function (event) {
        const element = event.detail && event.detail.elt ? event.detail.elt : event.target;
        if (!element || !element.querySelectorAll) return;
        if (element.matches && element.matches(selector) && element.tomselect) {
            element.tomselect.destroy();
        }
        element.querySelectorAll(selector).forEach(function (pickerElement) {
            if (pickerElement.tomselect) pickerElement.tomselect.destroy();
        });
    });
})();
