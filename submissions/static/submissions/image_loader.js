(function () {
    "use strict";

    const selector = "[data-cfm-image-loader]";
    const initializedContainers = new WeakSet();
    const loaderState = new WeakMap();

    function buildState(container) {
        const state = document.createElement("div");
        state.className = "cfm-image-loader-state";
        state.setAttribute("role", "status");
        state.setAttribute("aria-live", "polite");

        const spinner = document.createElement("span");
        spinner.className = "spinner-border spinner-border-sm cfm-image-loader-spinner";
        spinner.setAttribute("aria-hidden", "true");

        const loading = document.createElement("span");
        loading.className = "cfm-image-loader-loading";
        loading.textContent = container.dataset.cfmImageLoadingLabel || "Loading preview...";

        const error = document.createElement("span");
        error.className = "cfm-image-loader-error";
        error.textContent = container.dataset.cfmImageErrorLabel || "Preview unavailable.";

        const retry = document.createElement("button");
        retry.className = "btn btn-sm btn-outline-secondary cfm-image-loader-retry";
        retry.type = "button";
        retry.textContent = "Retry";

        state.append(spinner, loading, error, retry);
        container.appendChild(state);
        return {state, retry};
    }

    function setState(container, state) {
        container.classList.remove("is-idle", "is-loading", "is-ready", "is-error");
        container.classList.add(`is-${state}`);
        container.setAttribute("aria-busy", state === "loading" ? "true" : "false");
    }

    function initialize(container) {
        if (initializedContainers.has(container)) {
            return loaderState.get(container);
        }
        const image = container.querySelector(":scope > img");
        if (!image) {
            return null;
        }

        const controls = buildState(container);
        const state = {
            image,
            source: container.dataset.cfmImageSrc || "",
            retry: controls.retry,
        };
        initializedContainers.add(container);
        loaderState.set(container, state);
        setState(container, image.complete && image.naturalWidth ? "ready" : "idle");

        image.addEventListener("load", function () {
            setState(container, "ready");
        });
        image.addEventListener("error", function () {
            setState(container, "error");
        });
        controls.retry.addEventListener("click", function () {
            load(container, state.source);
        });
        return state;
    }

    function load(container, source) {
        const state = initialize(container);
        if (!state) {
            return;
        }
        const nextSource = source || container.dataset.cfmImageSrc || state.source;
        state.source = nextSource;
        if (!nextSource) {
            setState(container, "error");
            return;
        }

        setState(container, "loading");
        state.image.src = nextSource;
    }

    function reset(container) {
        const state = initialize(container);
        if (!state) {
            return;
        }
        setState(container, "idle");
    }

    function initializeWithin(root) {
        if (!root) {
            return;
        }
        if (root.matches && root.matches(selector)) {
            initialize(root);
        }
        if (root.querySelectorAll) {
            root.querySelectorAll(selector).forEach(initialize);
        }
    }

    function loadDeferredWithin(root) {
        if (!root) {
            return;
        }
        const containers = [];
        if (
            root.matches
            && root.matches(`${selector}[data-cfm-image-src]`)
        ) {
            containers.push(root);
        }
        if (root.querySelectorAll) {
            containers.push(
                ...root.querySelectorAll(`${selector}[data-cfm-image-src]`)
            );
        }
        containers.forEach(function (container) {
            const state = initialize(container);
            if (state && !state.image.getAttribute("src")) {
                load(container, container.dataset.cfmImageSrc);
            }
        });
    }

    function initializePage() {
        initializeWithin(document);
    }

    window.CFMImageLoader = {
        initializeWithin,
        load,
        loadDeferredWithin,
        reset,
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializePage, {once: true});
    } else {
        initializePage();
    }
    document.addEventListener("shown.bs.collapse", function (event) {
        loadDeferredWithin(event.target);
    });
    document.addEventListener("cfm:worklist-expanded", function (event) {
        loadDeferredWithin(event.target);
    });
    document.addEventListener("htmx:load", function (event) {
        initializeWithin(event.detail && event.detail.elt);
    });
}());
