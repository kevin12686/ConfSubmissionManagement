const CACHE_LIMIT = 24;
const THUMBNAIL_WIDTH = 128;
const THUMBNAIL_HEIGHT = 168;

export function installPDFThumbnails({sidebar, list, toggle, container, viewer, annotationMode}) {
    const desktop = window.matchMedia("(min-width: 900px)");
    const items = new Map();
    const wanted = new Set();
    const cache = new Map();
    let pdfDocument = null;
    let currentPage = 1;
    let focusPage = 1;
    let rotation = 0;
    let generation = 0;
    let open = false;
    let destroyed = false;
    let running = false;
    let renderTask = null;

    function placeholder(item, state) {
        item.state = state;
        item.preview.replaceChildren();
        const label = document.createElement("span");
        label.className = "cfm-pdf-thumbnail-placeholder";
        label.textContent = state === "error" ? "Preview unavailable" : state === "rendering" ? "Loading…" : "Preview";
        if (state === "rendering") {
            const spinner = document.createElement("span");
            spinner.className = "spinner-border spinner-border-sm";
            spinner.setAttribute("aria-hidden", "true");
            item.preview.append(spinner);
        }
        item.preview.append(label);
    }

    function reveal(number) {
        const button = items.get(number)?.button;
        if (!button || !open) return;
        const top = button.offsetTop;
        const bottom = top + button.offsetHeight;
        if (top < list.scrollTop) list.scrollTop = top;
        else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
    }

    function evict() {
        for (const [number, canvas] of cache) {
            if (cache.size <= CACHE_LIMIT) break;
            if (wanted.has(number) || number === currentPage) continue;
            canvas.width = canvas.height = 0;
            cache.delete(number);
            placeholder(items.get(number), "idle");
        }
    }

    async function pump() {
        if (running || !open || destroyed || !pdfDocument) return;
        const number = [...wanted].find(value => items.get(value)?.state === "idle");
        if (!number) return;
        running = true;
        const epoch = generation;
        const item = items.get(number);
        placeholder(item, "rendering");
        let canvas;
        try {
            const page = await pdfDocument.getPage(number);
            if (epoch !== generation || !open || destroyed) return;
            const angle = (page.rotate + rotation) % 360;
            const base = page.getViewport({scale: 1, rotation: angle});
            const scale = Math.min(THUMBNAIL_WIDTH / base.width, THUMBNAIL_HEIGHT / base.height);
            const viewport = page.getViewport({scale, rotation: angle});
            const ratio = Math.min(window.devicePixelRatio || 1, 2);
            canvas = document.createElement("canvas");
            canvas.width = Math.ceil(viewport.width * ratio);
            canvas.height = Math.ceil(viewport.height * ratio);
            canvas.style.width = `${viewport.width}px`;
            canvas.style.height = `${viewport.height}px`;
            canvas.setAttribute("aria-hidden", "true");
            renderTask = page.render({
                canvasContext: canvas.getContext("2d"), viewport, annotationMode,
                transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
            });
            await renderTask.promise;
            if (epoch !== generation || !open || destroyed) return;
            item.preview.replaceChildren(canvas);
            item.state = "ready";
            cache.set(number, canvas);
            canvas = null;
            evict();
        } catch (error) {
            if (epoch === generation && open && !destroyed) placeholder(item, "error");
        } finally {
            if (canvas) canvas.width = canvas.height = 0;
            if (item.state === "rendering") placeholder(item, "idle");
            renderTask = null;
            running = false;
            pump();
        }
    }

    const observer = new IntersectionObserver(entries => {
        if (!open || destroyed) return;
        for (const entry of entries) {
            const number = Number(entry.target.dataset.pdfThumbnailPage);
            if (entry.isIntersecting) wanted.add(number);
            else wanted.delete(number);
        }
        evict();
        pump();
    }, {root: list, rootMargin: "200px 0px"});

    function setOpen(value) {
        if (destroyed) return;
        open = Boolean(value && pdfDocument);
        sidebar.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.classList.toggle("active", open);
        generation++;
        renderTask?.cancel();
        observer.disconnect();
        wanted.clear();
        if (open) {
            for (const item of items.values()) {
                if (item.state === "error") placeholder(item, "idle");
                observer.observe(item.button);
            }
            reveal(currentPage);
        }
    }

    function setCurrentPage(number) {
        if (!items.has(number)) return;
        items.get(currentPage)?.button.removeAttribute("aria-current");
        currentPage = number;
        items.get(number).button.setAttribute("aria-current", "page");
        if (!list.contains(document.activeElement)) {
            items.get(focusPage)?.button.setAttribute("tabindex", "-1");
            focusPage = number;
            items.get(number).button.setAttribute("tabindex", "0");
        }
        const canvas = cache.get(number);
        if (canvas) { cache.delete(number); cache.set(number, canvas); }
        reveal(number);
    }

    function onClick(event) {
        const button = event.target.closest("[data-pdf-thumbnail-page]");
        const number = Number(button?.dataset.pdfThumbnailPage);
        if (!items.has(number) || destroyed) return;
        items.get(focusPage).button.tabIndex = -1;
        focusPage = number;
        button.tabIndex = 0;
        if (viewer.currentPageNumber !== number) viewer.currentPageNumber = number;
        if (!desktop.matches) setOpen(false);
        container.focus({preventScroll: true});
    }

    function onKeyDown(event) {
        if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || event.isComposing) return;
        if (event.key === "Escape") {
            event.preventDefault();
            setOpen(false);
            toggle.focus({preventScroll: true});
            return;
        }
        const button = event.target.closest("[data-pdf-thumbnail-page]");
        if (!button) return;
        const number = Number(button.dataset.pdfThumbnailPage);
        const next = event.key === "ArrowUp" ? number - 1 : event.key === "ArrowDown" ? number + 1
            : event.key === "Home" ? 1 : event.key === "End" ? items.size : null;
        if (next === null) return;
        event.preventDefault();
        if (!items.has(next)) return;
        items.get(focusPage).button.tabIndex = -1;
        focusPage = next;
        items.get(next).button.tabIndex = 0;
        items.get(next).button.focus({preventScroll: true});
        reveal(next);
    }

    const onToggle = () => setOpen(!open);
    const onBreakpoint = event => setOpen(event.matches);
    toggle.addEventListener("click", onToggle);
    list.addEventListener("click", onClick);
    sidebar.addEventListener("keydown", onKeyDown);
    desktop.addEventListener("change", onBreakpoint);

    return {
        setDocument(documentProxy) {
            if (destroyed || pdfDocument) return;
            pdfDocument = documentProxy;
            const fragment = document.createDocumentFragment();
            for (let number = 1; number <= pdfDocument.numPages; number++) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "cfm-pdf-thumbnail";
                button.dataset.pdfThumbnailPage = number;
                button.setAttribute("aria-label", `Page ${number}`);
                button.tabIndex = number === currentPage ? 0 : -1;
                const preview = document.createElement("span");
                preview.className = "cfm-pdf-thumbnail-preview";
                const label = document.createElement("span");
                label.className = "cfm-pdf-thumbnail-number";
                label.textContent = number;
                button.append(preview, label);
                const item = {button, preview};
                items.set(number, item);
                placeholder(item, "idle");
                fragment.append(button);
            }
            list.replaceChildren(fragment);
            toggle.disabled = false;
            setCurrentPage(viewer.currentPageNumber);
            setOpen(desktop.matches);
        },
        setCurrentPage,
        setRotation(value) {
            if (rotation === value || destroyed) return;
            rotation = value;
            generation++;
            renderTask?.cancel();
            for (const canvas of cache.values()) canvas.width = canvas.height = 0;
            cache.clear();
            for (const item of items.values()) placeholder(item, "idle");
            pump();
        },
        destroy() {
            destroyed = true;
            generation++;
            observer.disconnect();
            renderTask?.cancel();
            for (const canvas of cache.values()) canvas.width = canvas.height = 0;
            cache.clear();
            wanted.clear();
            toggle.removeEventListener("click", onToggle);
            list.removeEventListener("click", onClick);
            sidebar.removeEventListener("keydown", onKeyDown);
            desktop.removeEventListener("change", onBreakpoint);
            list.replaceChildren();
            items.clear();
            pdfDocument = null;
        },
    };
}
