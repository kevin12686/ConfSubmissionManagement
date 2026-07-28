(function () {
    "use strict";

    const expansionSelector = "[data-cfm-context-expansion]";
    const viewportMargin = 12;
    const userRequestedExpansions = new WeakSet();

    function prefersReducedMotion() {
        return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    function visibleContentTop() {
        const headerBottom =
            document.querySelector(".cfm-app-header")?.getBoundingClientRect().bottom || 0;
        return Math.max(viewportMargin, headerBottom + viewportMargin);
    }

    function controlledExpansion(trigger) {
        const controlledId = trigger.getAttribute("aria-controls");
        if (controlledId) {
            return document.getElementById(controlledId);
        }

        const targetSelector = trigger.getAttribute("data-bs-target");
        if (!targetSelector || !targetSelector.startsWith("#")) return null;
        try {
            return document.querySelector(targetSelector);
        } catch (error) {
            return null;
        }
    }

    function triggerFor(expansion) {
        if (expansion.id) {
            return Array.from(
                document.querySelectorAll("[aria-controls]"),
            ).find(function (candidate) {
                return (
                    candidate.getAttribute("aria-controls") === expansion.id
                    && !expansion.contains(candidate)
                );
            });
        }
        return null;
    }

    function ownerFor(expansion) {
        const trigger = triggerFor(expansion);
        const explicitOwner = trigger?.closest("[data-cfm-expansion-owner]");
        if (explicitOwner) return explicitOwner;

        const triggerRow = trigger?.closest("tr");
        if (triggerRow) return triggerRow;

        let previousElement = expansion.previousElementSibling;
        while (previousElement?.matches(expansionSelector)) {
            previousElement = previousElement.previousElementSibling;
        }
        return previousElement?.matches("tr") ? previousElement : expansion;
    }

    function revealExpansion(expansion) {
        window.requestAnimationFrame(function () {
            const expansionRect = expansion.getBoundingClientRect();
            if (!expansionRect.height) return;

            const owner = ownerFor(expansion);
            const ownerRect = owner.getBoundingClientRect();
            const groupTop = Math.min(ownerRect.top, expansionRect.top);
            const groupBottom = Math.max(ownerRect.bottom, expansionRect.bottom);
            const groupHeight = groupBottom - groupTop;
            const viewportTop = visibleContentTop();
            const viewportBottom = window.innerHeight - viewportMargin;
            const availableHeight = Math.max(0, viewportBottom - viewportTop);
            const groupIsFullyVisible =
                groupTop >= viewportTop && groupBottom <= viewportBottom;

            if (groupIsFullyVisible) return;

            const groupFitsViewport = groupHeight <= availableHeight;
            const targetViewportTop = groupFitsViewport
                ? viewportTop + ((availableHeight - groupHeight) / 2)
                : viewportTop;
            window.scrollTo({
                behavior: prefersReducedMotion() ? "auto" : "smooth",
                left: window.scrollX,
                top: Math.max(0, window.scrollY + groupTop - targetViewportTop),
            });
        });
    }

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target?.closest('[data-bs-toggle="collapse"]');
        if (!trigger || trigger.getAttribute("aria-expanded") === "true") return;

        const expansion = controlledExpansion(trigger);
        if (expansion?.matches(expansionSelector)) {
            userRequestedExpansions.add(expansion);
        }
    }, true);

    document.addEventListener("shown.bs.collapse", function (event) {
        const expansion = event.target;
        if (
            !(expansion instanceof Element)
            || !expansion.matches(expansionSelector)
        ) {
            return;
        }
        if (!userRequestedExpansions.has(expansion)) return;
        userRequestedExpansions.delete(expansion);
        revealExpansion(expansion);
    });

    document.addEventListener("hidden.bs.collapse", function (event) {
        if (event.target instanceof Element) {
            userRequestedExpansions.delete(event.target);
        }
    });

    window.CFMContextExpansion = {reveal: revealExpansion};
})();
