const GESTURE_IDLE_MS = 280;
const MIN_TURN_INTERVAL_MS = 500;
const TURN_DISTANCE_PX = 40;
const EDGE_TOLERANCE_PX = 1;

export function installPDFNavigation({container, viewer, singlePageMode}) {
    let lastWheel = -Infinity;
    let lastTurn = -Infinity;
    let distance = 0;
    let direction = 0;
    let turned = false;
    let panned = false;
    let positionFrame = 0;
    let applyPosition = null;
    const heldKeys = new Set();
    const keyDirections = new Map([
        ["ArrowLeft", -1], ["ArrowRight", 1], ["ArrowUp", -1], ["ArrowDown", 1],
        ["PageUp", -1], ["PageDown", 1], ["Home", -1], ["End", 1],
    ]);
    const verticalKeys = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown"]);
    const controlSelector = "input, textarea, select, button, [contenteditable], [role=button], [role=slider], [role=combobox]";

    function resetGesture() {
        distance = 0;
        direction = 0;
        turned = false;
        panned = false;
    }

    function suspend() {
        const activeGesture = performance.now() - lastWheel < GESTURE_IDLE_MS;
        resetGesture();
        if (activeGesture) {
            turned = true;
            lastTurn = performance.now();
        }
    }

    function atVerticalEdge(direction) {
        const bottom = Math.max(0, container.scrollHeight - container.clientHeight);
        const pageView = viewer.getPageView(viewer.currentPageNumber - 1);
        // PDF.js page borders/margins can overflow even when the PDF itself fits.
        const pageFits = pageView && pageView.height <= container.clientHeight + EDGE_TOLERANCE_PX;
        return pageFits || (direction > 0
            ? container.scrollTop >= bottom - EDGE_TOLERANCE_PX
            : container.scrollTop <= EDGE_TOLERANCE_PX);
    }

    function settlePosition() {
        cancelAnimationFrame(positionFrame);
        positionFrame = 0;
        const apply = applyPosition;
        applyPosition = null;
        apply?.();
    }

    function navigate(direction, {destination = null, enterAtBottom = direction < 0} = {}) {
        if (destination !== null) {
            if (destination !== viewer.currentPageNumber) viewer.currentPageNumber = destination;
        } else if (!(direction > 0 ? viewer.nextPage() : viewer.previousPage())) {
            return false;
        }
        const selectedPage = viewer.currentPageNumber;
        const selectedMode = viewer.scrollMode;
        cancelAnimationFrame(positionFrame);
        // PDF.js and the reader's native fit update run before positioning this page.
        applyPosition = () => {
            if (viewer.scrollMode === selectedMode && viewer.currentPageNumber === selectedPage &&
                (selectedMode === singlePageMode || destination !== null)) {
                container.scrollTop = enterAtBottom ? container.scrollHeight - container.clientHeight : 0;
            }
        };
        positionFrame = requestAnimationFrame(settlePosition);
        return true;
    }

    function onWheel(event) {
        if (!viewer.pdfDocument || viewer.scrollMode !== singlePageMode) {
            resetGesture();
            return;
        }
        if (event.defaultPrevented || !event.cancelable || event.ctrlKey || event.metaKey ||
            event.altKey || event.shiftKey || !Number.isFinite(event.deltaY) ||
            Math.abs(event.deltaY) <= Math.abs(event.deltaX) ||
            event.target.closest?.(controlSelector)) {
            return;
        }

        const now = performance.now();
        if (now - lastWheel >= GESTURE_IDLE_MS && now - lastTurn >= MIN_TURN_INTERVAL_MS) {
            resetGesture();
        }
        lastWheel = now;
        if (turned) {
            event.preventDefault();
            return;
        }

        settlePosition();
        const nextDirection = Math.sign(event.deltaY);
        if (!atVerticalEdge(nextDirection)) {
            // Native scrolling owns zoomed-page panning, including diagonal movement.
            panned = true;
            distance = 0;
            direction = 0;
            return;
        }

        event.preventDefault();
        // Reaching an edge and flipping are separate gestures; ignore pan inertia.
        if (panned) return;
        if (nextDirection !== direction) distance = 0;
        direction = nextDirection;
        const lineHeight = parseFloat(getComputedStyle(container).lineHeight) || 16;
        const unit = event.deltaMode === 1 ? lineHeight : event.deltaMode === 2 ? container.clientHeight : 1;
        distance += Math.abs(event.deltaY) * unit;
        if (distance < TURN_DISTANCE_PX) return;

        turned = true;
        lastTurn = now;
        navigate(direction);
    }

    function onKeyDown(event) {
        if (!viewer.pdfDocument || !viewer.pagesCount || !keyDirections.has(event.key) ||
            event.defaultPrevented || !event.cancelable || event.isComposing ||
            event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
            event.target.closest?.(controlSelector)) return;

        const direction = keyDirections.get(event.key);
        const held = heldKeys.has(event.key) || event.repeat;
        heldKeys.add(event.key);
        // A fresh input can arrive before the previous turn's animation frame.
        settlePosition();
        if (verticalKeys.has(event.key)) {
            if (viewer.scrollMode !== singlePageMode || !atVerticalEdge(direction)) return;
        }

        event.preventDefault();
        // Repeated keys may pan within a page, but crossing a page needs a fresh press.
        if (held) return;
        const destination = event.key === "Home" ? 1 : event.key === "End" ? viewer.pagesCount : null;
        suspend();
        const enterAtBottom = destination === null ? direction < 0 : event.key === "End";
        if (navigate(direction, {destination, enterAtBottom})) container.focus({preventScroll: true});
    }

    function onKeyUp(event) {
        heldKeys.delete(event.key);
    }

    function clearKeys() {
        heldKeys.clear();
    }

    function onFocusOut(event) {
        if (!container.contains(event.relatedTarget)) clearKeys();
    }

    container.addEventListener("wheel", onWheel, {passive: false});
    container.addEventListener("keydown", onKeyDown);
    container.addEventListener("focusout", onFocusOut);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", clearKeys);
    return {
        suspend,
        destroy() {
            cancelAnimationFrame(positionFrame);
            applyPosition = null;
            container.removeEventListener("wheel", onWheel);
            container.removeEventListener("keydown", onKeyDown);
            container.removeEventListener("focusout", onFocusOut);
            window.removeEventListener("keyup", onKeyUp);
            window.removeEventListener("blur", clearKeys);
            clearKeys();
        },
    };
}
