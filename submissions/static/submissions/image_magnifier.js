(function () {
    "use strict";

    const containerSelector = "[data-cfm-image-magnifier]";
    const initializedContainers = new WeakSet();
    const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
    let controlPressed = false;

    function clamp(value, minimum, maximum) {
        return Math.min(Math.max(value, minimum), maximum);
    }

    function initializeContainer(container) {
        if (initializedContainers.has(container)) {
            return;
        }
        const image = container.querySelector(":scope > img");
        if (!image) {
            return;
        }
        initializedContainers.add(container);
        const hintText = container.dataset.cfmImageMagnifierHint || "";

        const lens = document.createElement("div");
        lens.className = "cfm-image-magnifier-lens";
        lens.setAttribute("aria-hidden", "true");
        const magnifiedImage = document.createElement("img");
        magnifiedImage.className = "cfm-image-magnifier-lens-image";
        magnifiedImage.alt = "";
        magnifiedImage.draggable = false;
        lens.appendChild(magnifiedImage);
        container.appendChild(lens);

        let hint = null;
        if (hintText) {
            hint = document.createElement("div");
            hint.className = "cfm-image-magnifier-hint";
            hint.setAttribute("aria-hidden", "true");
            hint.textContent = hintText;
            container.appendChild(hint);
        }

        let active = false;
        let pointerInside = false;
        let pointerX = 0;
        let pointerY = 0;
        let animationFrame = null;

        function updateHint() {
            if (!hint) {
                return;
            }
            hint.classList.toggle(
                "is-visible",
                pointerInside && !active && !controlPressed && finePointer.matches,
            );
        }

        function hideLens() {
            active = false;
            container.classList.remove("is-magnifier-armed");
            lens.classList.remove("is-visible");
            updateHint();
            if (animationFrame !== null) {
                window.cancelAnimationFrame(animationFrame);
                animationFrame = null;
            }
        }

        function synchronizeSource() {
            const source = image.currentSrc || image.src || "";
            if (source && magnifiedImage.src !== source) {
                magnifiedImage.src = source;
            }
        }

        function renderLens() {
            animationFrame = null;
            if (
                !active
                || !controlPressed
                || !finePointer.matches
            ) {
                hideLens();
                return;
            }

            if (!image.complete || !image.naturalWidth || !image.naturalHeight) {
                lens.classList.remove("is-visible");
                return;
            }

            const imageRect = image.getBoundingClientRect();
            if (!imageRect.width || !imageRect.height) {
                hideLens();
                return;
            }

            synchronizeSource();
            lens.classList.add("is-visible");
            const lensWidth = lens.offsetWidth;
            const lensHeight = lens.offsetHeight;
            const x = clamp(pointerX - imageRect.left, 0, imageRect.width);
            const y = clamp(pointerY - imageRect.top, 0, imageRect.height);
            const sourceScale = Math.min(
                image.naturalWidth / imageRect.width,
                image.naturalHeight / imageRect.height
            );
            const zoom = clamp(sourceScale || 2.5, 1.75, 2.5);
            const edgeGap = 4;
            const lensLeft = clamp(
                x - lensWidth / 2,
                edgeGap,
                Math.max(edgeGap, imageRect.width - lensWidth - edgeGap)
            );
            const lensTop = clamp(
                y - lensHeight / 2,
                edgeGap,
                Math.max(edgeGap, imageRect.height - lensHeight - edgeGap)
            );

            lens.style.transform = `translate3d(${lensLeft}px, ${lensTop}px, 0)`;
            magnifiedImage.style.width = `${imageRect.width * zoom}px`;
            magnifiedImage.style.height = `${imageRect.height * zoom}px`;
            magnifiedImage.style.left = `${lensWidth / 2 - x * zoom}px`;
            magnifiedImage.style.top = `${lensHeight / 2 - y * zoom}px`;
        }

        function scheduleRender() {
            if (animationFrame === null) {
                animationFrame = window.requestAnimationFrame(renderLens);
            }
        }

        function showLens() {
            if (!pointerInside || !controlPressed || !finePointer.matches) {
                return;
            }
            active = true;
            container.classList.add("is-magnifier-armed");
            updateHint();
            scheduleRender();
        }

        container.addEventListener("pointerenter", function (event) {
            if (!finePointer.matches) {
                return;
            }
            pointerInside = true;
            pointerX = event.clientX;
            pointerY = event.clientY;
            if (event.ctrlKey && !controlPressed) {
                setControlPressed(true);
            }
            showLens();
            updateHint();
        });
        container.addEventListener("pointermove", function (event) {
            pointerX = event.clientX;
            pointerY = event.clientY;
            if (event.ctrlKey && !controlPressed) {
                setControlPressed(true);
            }
            if (active) {
                scheduleRender();
            } else {
                showLens();
                updateHint();
            }
        });
        container.addEventListener("pointerleave", function () {
            pointerInside = false;
            hideLens();
        });
        container.addEventListener("cfm:magnifier-control-change", function () {
            if (controlPressed) {
                showLens();
            } else {
                hideLens();
            }
        });
        image.addEventListener("load", function () {
            synchronizeSource();
            if (active) {
                scheduleRender();
            }
        });
    }

    function setControlPressed(pressed) {
        if (controlPressed === pressed) {
            return;
        }
        controlPressed = pressed;
        document.querySelectorAll(containerSelector).forEach(function (container) {
            container.dispatchEvent(new CustomEvent("cfm:magnifier-control-change"));
        });
    }

    function initializeWithin(root) {
        if (!root) {
            return;
        }
        if (root.matches && root.matches(containerSelector)) {
            initializeContainer(root);
        }
        if (root.querySelectorAll) {
            root.querySelectorAll(containerSelector).forEach(initializeContainer);
        }
    }

    function initializePage() {
        initializeWithin(document);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializePage, {once: true});
    } else {
        initializePage();
    }
    document.addEventListener("shown.bs.collapse", function (event) {
        initializeWithin(event.target);
    });
    document.addEventListener("htmx:load", function (event) {
        initializeWithin(event.detail && event.detail.elt);
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Control") {
            setControlPressed(true);
        }
    });
    document.addEventListener("keyup", function (event) {
        if (event.key === "Control") {
            setControlPressed(false);
        }
    });
    window.addEventListener("blur", function () {
        setControlPressed(false);
    });
    document.addEventListener("visibilitychange", function () {
        if (document.hidden) {
            setControlPressed(false);
        }
    });
})();
