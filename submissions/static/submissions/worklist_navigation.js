(function () {
    "use strict";

    const STORAGE_PREFIX = "cfm:worklist-return:";
    const PAGINATION_STORAGE_PREFIX = "cfm:pagination-return:";
    const MAX_AGE_MS = 5 * 60 * 1000;
    let pendingPaginationPosition = null;
    let settlingPaginationPosition = null;

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

    function paginationStorageKey(url) {
        return `${PAGINATION_STORAGE_PREFIX}${url.pathname}${url.search}`;
    }

    function visibleInViewport(element) {
        const bounds = element.getBoundingClientRect();
        return bounds.bottom > 0 && bounds.top < window.innerHeight;
    }

    function paginationPositionPayload(navigation, link) {
        const targetSelector = link.getAttribute("hx-target") || "";
        return {
            timestamp: Date.now(),
            viewportOffset: navigation.getBoundingClientRect().top,
            targetSelector,
        };
    }

    function restoreTopPaginationPosition(payload, root) {
        if (!payload || Date.now() - Number(payload.timestamp || 0) > MAX_AGE_MS) {
            return;
        }
        const searchRoot = root instanceof Element ? root : document;
        const navigation = searchRoot.matches?.(
            '[data-cfm-pagination-position="top"]'
        )
            ? searchRoot
            : searchRoot.querySelector(
                  '[data-cfm-pagination-position="top"]'
              );
        if (!navigation) return;

        const absoluteTop =
            navigation.getBoundingClientRect().top + window.scrollY;
        window.scrollTo({
            top: Math.max(0, absoluteTop - Number(payload.viewportOffset || 0)),
            behavior: "auto",
        });
    }

    function capturePaginationPosition(event) {
        if (
            event.defaultPrevented ||
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey
        ) {
            return;
        }
        const eventTarget =
            event.target instanceof Element ? event.target : null;
        const link = eventTarget?.closest(
            '[data-cfm-pagination-position="top"] a'
        );
        const navigation = link?.closest(
            '[data-cfm-pagination-position="top"]'
        );
        if (!link || !navigation || !visibleInViewport(navigation)) return;

        const payload = paginationPositionPayload(navigation, link);
        if (link.hasAttribute("hx-get") || link.hasAttribute("data-hx-get")) {
            pendingPaginationPosition = payload;
            return;
        }

        const destination = new URL(link.href, window.location.href);
        safeSessionSet(
            paginationStorageKey(destination),
            JSON.stringify(payload)
        );
    }

    function restoreFullPagePaginationPosition() {
        const key = paginationStorageKey(new URL(window.location.href));
        const raw = safeSessionGet(key);
        if (!raw) return;
        safeSessionRemove(key);
        try {
            const payload = JSON.parse(raw);
            window.requestAnimationFrame(function () {
                restoreTopPaginationPosition(payload, document);
            });
        } catch (error) {
            // Ignore stale or malformed progressive-enhancement state.
        }
    }

    function paginationEventTarget(event, payload) {
        const target = event.detail?.target || event.target;
        const selector = payload?.targetSelector;
        if (
            selector &&
            target instanceof Element &&
            !target.matches(selector)
        ) {
            return null;
        }
        return target;
    }

    function restoreSwappedPaginationPosition(event) {
        if (!pendingPaginationPosition) return;
        const target = paginationEventTarget(event, pendingPaginationPosition);
        if (!target) return;
        const payload = pendingPaginationPosition;
        pendingPaginationPosition = null;
        settlingPaginationPosition = payload;
        restoreTopPaginationPosition(payload, target);
    }

    function restoreSettledPaginationPosition(event) {
        if (!settlingPaginationPosition) return;
        const target = paginationEventTarget(event, settlingPaginationPosition);
        if (!target) return;
        const payload = settlingPaginationPosition;
        settlingPaginationPosition = null;
        window.requestAnimationFrame(function () {
            const currentTarget = payload.targetSelector
                ? document.querySelector(payload.targetSelector)
                : document;
            restoreTopPaginationPosition(payload, currentTarget || document);
        });
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

    function notifyExpanded(element) {
        element.dispatchEvent(
            new CustomEvent("cfm:worklist-expanded", { bubbles: true })
        );
    }

    function showCollapse(element) {
        if (!element || element.classList.contains("show")) return;
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
            window.bootstrap.Collapse.getOrCreateInstance(element, {
                toggle: false,
            }).show();
            return;
        }
        element.classList.add("show");
        markTriggerExpanded();
        notifyExpanded(element);
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

    document.addEventListener("click", capturePaginationPosition, true);
    document.body.addEventListener(
        "htmx:afterSwap",
        restoreSwappedPaginationPosition
    );
    document.body.addEventListener(
        "htmx:afterSettle",
        restoreSettledPaginationPosition
    );
    document.body.addEventListener("htmx:responseError", function () {
        pendingPaginationPosition = null;
        settlingPaginationPosition = null;
    });
    document.body.addEventListener("htmx:sendError", function () {
        pendingPaginationPosition = null;
        settlingPaginationPosition = null;
    });

    document.addEventListener("shown.bs.collapse", function (event) {
        if (
            event.target instanceof Element &&
            event.target.closest("[data-cfm-worklist]")
        ) {
            notifyExpanded(event.target);
        }
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            restoreFullPagePaginationPosition();
            restoreWorklistPosition();
        });
    } else {
        restoreFullPagePaginationPosition();
        restoreWorklistPosition();
    }
})();
