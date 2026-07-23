(function () {
    "use strict";

    const STORAGE_PREFIX = "cfm:worklist-return:";
    const MAX_AGE_MS = 5 * 60 * 1000;

    function storageKey() {
        return `${STORAGE_PREFIX}${window.location.pathname}`;
    }

    function safeSessionGet(key) {
        try {
            return window.sessionStorage.getItem(key);
        } catch (error) {
            return null;
        }
    }

    function safeSessionSet(key, value) {
        try {
            window.sessionStorage.setItem(key, value);
        } catch (error) {
            // Position restoration is progressive enhancement.
        }
    }

    function safeSessionRemove(key) {
        try {
            window.sessionStorage.removeItem(key);
        } catch (error) {
            // Position restoration is progressive enhancement.
        }
    }

    function ensureReturnInput(form, value) {
        let input = form.querySelector("input[name='return_to']");
        if (!input) {
            input = document.createElement("input");
            input.type = "hidden";
            input.name = "return_to";
            form.appendChild(input);
        }
        input.value = value;
    }

    function cardSelector(cardId) {
        return `[data-cfm-worklist-card="${CSS.escape(String(cardId))}"]`;
    }

    function captureWorklistPosition(form) {
        const card = form.closest("[data-cfm-worklist-card]");
        const worklist = card?.closest("[data-cfm-worklist]");
        if (!card || !worklist || !card.id) return;

        const cards = Array.from(
            worklist.querySelectorAll("[data-cfm-worklist-card]")
        );
        const cardIndex = cards.indexOf(card);
        const returnUrl = `${window.location.pathname}${window.location.search}#${card.id}`;
        const payload = {
            timestamp: Date.now(),
            pathname: window.location.pathname,
            anchor: `#${card.id}`,
            worklistId: worklist.id || "",
            cardId: card.dataset.cfmWorklistCard || "",
            nextCardId:
                cardIndex >= 0 && cards[cardIndex + 1]
                    ? cards[cardIndex + 1].dataset.cfmWorklistCard || ""
                    : "",
            previousCardId:
                cardIndex > 0
                    ? cards[cardIndex - 1].dataset.cfmWorklistCard || ""
                    : "",
            viewportOffset: card.getBoundingClientRect().top,
            expandedIds: Array.from(card.querySelectorAll(".collapse.show"))
                .map((element) => element.id)
                .filter(Boolean),
        };

        safeSessionSet(storageKey(), JSON.stringify(payload));
        ensureReturnInput(form, returnUrl);
    }

    function loadPayload() {
        const key = storageKey();
        const raw = safeSessionGet(key);
        if (!raw) return null;

        try {
            const payload = JSON.parse(raw);
            if (
                !payload ||
                payload.pathname !== window.location.pathname ||
                Date.now() - Number(payload.timestamp || 0) > MAX_AGE_MS
            ) {
                safeSessionRemove(key);
                return null;
            }
            return payload;
        } catch (error) {
            safeSessionRemove(key);
            return null;
        }
    }

    function showCollapse(element) {
        if (!element || element.classList.contains("show")) return;
        const notifyExpanded = function () {
            element.dispatchEvent(
                new CustomEvent("cfm:worklist-expanded", { bubbles: true })
            );
        };
        const markTriggerExpanded = function () {
            if (!element.id) return;
            document
                .querySelectorAll(
                    `[data-bs-target="#${CSS.escape(element.id)}"]`
                )
                .forEach(function (trigger) {
                    trigger.classList.remove("collapsed");
                    trigger.setAttribute("aria-expanded", "true");
                });
        };
        if (window.bootstrap?.Collapse) {
            element.addEventListener("shown.bs.collapse", notifyExpanded, {
                once: true,
            });
            window.bootstrap.Collapse.getOrCreateInstance(element, {
                toggle: false,
            }).show();
            return;
        }
        element.classList.add("show");
        markTriggerExpanded();
        notifyExpanded();
    }

    function restoreExpandedState(card, payload, isOriginalCard) {
        if (!card) return;
        const collapseIds = isOriginalCard
            ? payload.expandedIds || []
            : [card.dataset.cfmWorklistCollapse || ""];
        collapseIds
            .filter(Boolean)
            .forEach((id) => showCollapse(document.getElementById(id)));
    }

    function restoreWorklistPosition() {
        const payload = loadPayload();
        if (!payload) return;

        if (document.querySelector("[data-cfm-worklist-return-deferred]")) {
            return;
        }

        const activeCardId = document
            .querySelector("[data-cfm-worklist-active-card]")
            ?.dataset.cfmWorklistActiveCard;
        const returnedThroughExpectedRedirect =
            window.location.hash === payload.anchor;
        const returnedWithValidation =
            activeCardId && String(activeCardId) === String(payload.cardId);
        if (!returnedThroughExpectedRedirect && !returnedWithValidation) {
            return;
        }

        const originalCard = document.querySelector(cardSelector(payload.cardId));
        const target =
            originalCard ||
            (payload.nextCardId
                ? document.querySelector(cardSelector(payload.nextCardId))
                : null) ||
            (payload.previousCardId
                ? document.querySelector(cardSelector(payload.previousCardId))
                : null) ||
            (payload.worklistId
                ? document.getElementById(payload.worklistId)
                : null);

        restoreExpandedState(target, payload, Boolean(originalCard));
        safeSessionRemove(storageKey());

        window.requestAnimationFrame(function () {
            window.requestAnimationFrame(function () {
                if (target) {
                    const targetTop =
                        target.getBoundingClientRect().top + window.scrollY;
                    const requestedOffset = Number(payload.viewportOffset);
                    const viewportOffset = Number.isFinite(requestedOffset)
                        ? Math.max(12, requestedOffset)
                        : 12;
                    window.scrollTo({
                        top: Math.max(0, targetTop - viewportOffset),
                        behavior: "auto",
                    });
                }
                if (window.location.hash === payload.anchor) {
                    window.history.replaceState(
                        window.history.state,
                        "",
                        `${window.location.pathname}${window.location.search}`
                    );
                }
            });
        });
    }

    document.addEventListener("submit", function (event) {
        const form = event.target;
        if (
            event.defaultPrevented ||
            !(form instanceof HTMLFormElement) ||
            form.method.toLowerCase() !== "post"
        ) {
            return;
        }
        captureWorklistPosition(form);
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", restoreWorklistPosition);
    } else {
        restoreWorklistPosition();
    }
})();
