(function () {
    "use strict";
    const root = document.querySelector("[data-pdf-reader]");
    if (!root) return;
    const query = selector => root.querySelector(selector);
    const loading = query("[data-pdf-loading]");
    const error = query("[data-pdf-error]");
    function failed(message) {
        loading.hidden = true;
        error.hidden = false;
        error.textContent = message + " No other file was substituted. Use Open directly or Download PDF to inspect this same file.";
    }
    async function initialize() {
        const navigationModule = new URL("./pdf_navigation.mjs", import.meta.url);
        navigationModule.search = new URL(import.meta.url).search;
        const {installPDFNavigation} = await import(navigationModule.href);
        const thumbnailsModule = new URL("./pdf_thumbnails.mjs", import.meta.url);
        thumbnailsModule.search = new URL(import.meta.url).search;
        const {installPDFThumbnails} = await import(thumbnailsModule.href);
        const runtime = new URL(root.dataset.pdfRuntime, location.origin);
        const pdfjs = await import(new URL("legacy/build/pdf.min.mjs", runtime).href);
        const {EventBus, PDFViewer, PDFLinkService, PDFFindController, ScrollMode} = await import(new URL("legacy/web/pdf_viewer.mjs", runtime).href);
        pdfjs.GlobalWorkerOptions.workerSrc = new URL("legacy/build/pdf.worker.min.mjs", runtime).href;
        const eventBus = new EventBus();
        const linkService = new PDFLinkService({eventBus, externalLinkTarget: 2, externalLinkRel: "noopener noreferrer"});
        const findController = new PDFFindController({eventBus, linkService});
        const container = query("[data-pdf-container]");
        const viewer = new PDFViewer({
            container, viewer: query("[data-pdf-pages]"), eventBus, linkService, findController,
            annotationMode: pdfjs.AnnotationMode.ENABLE,
            annotationEditorMode: pdfjs.AnnotationEditorType.DISABLE,
            enableAutoLinking: false,
            imageResourcesPath: new URL("legacy/web/images/", runtime).href,
        });
        linkService.setViewer(viewer);
        const navigation = installPDFNavigation({container, viewer, singlePageMode: ScrollMode.PAGE});
        const thumbnails = installPDFThumbnails({
            sidebar: query("[data-pdf-sidebar]"), list: query("[data-pdf-thumbnails]"),
            toggle: query("[data-pdf-sidebar-toggle]"), container, viewer,
            annotationMode: pdfjs.AnnotationMode.DISABLE,
        });
        const page = query("[data-pdf-page]");
        const scale = query("[data-pdf-scale]");
        const find = query("[data-pdf-find]");
        const layouts = {single: ScrollMode.PAGE, continuous: ScrollMode.VERTICAL};
        const layoutButtons = root.querySelectorAll("[data-pdf-layout]");
        let fitFrame = 0;
        function scheduleFit() {
            cancelAnimationFrame(fitFrame);
            // Let PDF.js finish the page/scroll transition before fitting its new viewport.
            fitFrame = requestAnimationFrame(() => {
                fitFrame = 0;
                if (viewer.pdfDocument && ["page-width", "page-fit"].includes(viewer.currentScaleValue)) {
                    viewer.currentScaleValue = viewer.currentScaleValue;
                }
            });
        }
        function syncLayout() {
            layoutButtons.forEach(button => {
                const selected = viewer.scrollMode === layouts[button.dataset.pdfLayout];
                button.classList.toggle("active", selected);
                button.setAttribute("aria-pressed", String(selected));
            });
        }
        function syncPage() {
            page.value = viewer.currentPageNumber;
            query("[data-pdf-previous]").disabled = viewer.currentPageNumber <= 1;
            query("[data-pdf-next]").disabled = viewer.currentPageNumber >= viewer.pagesCount;
        }
        eventBus.on("pagesinit", () => {
            root.querySelectorAll(".cfm-pdf-reader-toolbar button, .cfm-pdf-reader-toolbar input, .cfm-pdf-reader-toolbar select").forEach(element => { element.disabled = false; });
            page.max = viewer.pagesCount;
            query("[data-pdf-page-count]").textContent = `/ ${viewer.pagesCount}`;
            viewer.scrollMode = ScrollMode.PAGE;
            viewer.currentScaleValue = "page-fit";
            syncLayout();
            syncPage();
            if (document.activeElement === document.body) container.focus({preventScroll: true});
        });
        eventBus.on("pagerendered", event => {
            loading.hidden = true;
            if (event.error) failed("A PDF page could not be rendered.");
        });
        eventBus.on("pagechanging", () => {
            syncPage();
            thumbnails.setCurrentPage(viewer.currentPageNumber);
            if (viewer.scrollMode === ScrollMode.PAGE) scheduleFit();
        });
        eventBus.on("scrollmodechanged", syncLayout);
        eventBus.on("scrollmodechanged", navigation.suspend);
        eventBus.on("rotationchanging", event => thumbnails.setRotation(event.pagesRotation));
        eventBus.on("scalechanging", event => {
            navigation.suspend();
            const preset = [...scale.options].find(option => option.value === String(viewer.currentScaleValue));
            if (preset) {
                scale.value = preset.value;
            } else {
                const option = query("[data-pdf-custom-scale]");
                option.textContent = `${Math.round(event.scale * 100)}%`;
                option.hidden = false;
                scale.value = "custom";
            }
        });
        query("[data-pdf-previous]").addEventListener("click", () => viewer.previousPage());
        query("[data-pdf-next]").addEventListener("click", () => viewer.nextPage());
        layoutButtons.forEach(button => button.addEventListener("click", () => {
            viewer.scrollMode = layouts[button.dataset.pdfLayout];
            syncLayout();
        }));
        page.addEventListener("change", () => {
            const number = Number(page.value);
            if (Number.isInteger(number) && number >= 1 && number <= viewer.pagesCount && number !== viewer.currentPageNumber) viewer.currentPageNumber = number;
            syncPage();
        });
        page.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); page.dispatchEvent(new Event("change")); } });
        scale.addEventListener("change", () => { if (scale.value !== "custom") viewer.currentScaleValue = scale.value; });
        root.querySelectorAll("[data-pdf-zoom]").forEach(button => button.addEventListener("click", () => {
            viewer.currentScale = Math.min(4, Math.max(.25, viewer.currentScale * (button.dataset.pdfZoom === "in" ? 1.2 : 1 / 1.2)));
        }));
        query("[data-pdf-rotate]").addEventListener("click", () => { viewer.pagesRotation = (viewer.pagesRotation + 90) % 360; });
        function search(type = "", previous = false) {
            eventBus.dispatch("find", {source: find, type, query: find.value, phraseSearch: true, caseSensitive: false, entireWord: false, highlightAll: true, findPrevious: previous, matchDiacritics: false});
        }
        find.addEventListener("input", () => search());
        find.addEventListener("keydown", event => {
            if (event.key === "Enter") { event.preventDefault(); search("again", event.shiftKey); }
            if (event.key === "Escape") { find.value = ""; search(); container.focus(); }
        });
        root.querySelectorAll("[data-pdf-find-step]").forEach(button => button.addEventListener("click", () => search("again", button.dataset.pdfFindStep === "previous")));
        eventBus.on("updatefindmatchescount", event => {
            const {current, total} = event.matchesCount;
            query("[data-pdf-find-count]").textContent = find.value ? `${current} / ${total}` : "";
        });
        eventBus.on("updatefindcontrolstate", event => {
            const {current, total} = event.matchesCount;
            query("[data-pdf-find-count]").textContent = !find.value ? "" : event.state === 3 ? "Searching…" : event.state === 1 ? "No matches" : `${current} / ${total}`;
        });
        document.addEventListener("keydown", event => {
            if (!find.disabled && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") { event.preventDefault(); find.focus(); find.select(); }
        });
        const observer = new ResizeObserver(scheduleFit);
        observer.observe(container);
        const task = pdfjs.getDocument({
            url: root.dataset.pdfUrl, isEvalSupported: false, enableXfa: false,
            cMapUrl: new URL("cmaps/", runtime).href, cMapPacked: true,
            standardFontDataUrl: new URL("standard_fonts/", runtime).href,
            wasmUrl: new URL("wasm/", runtime).href, iccUrl: new URL("iccs/", runtime).href,
        });
        let unlock;
        const passwordForm = query("[data-pdf-password-form]");
        task.onPassword = (callback, reason) => {
            unlock = callback;
            passwordForm.hidden = false;
            loading.hidden = true;
            query("[data-pdf-password-message]").textContent = reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD ? "Incorrect password. Try again." : "This PDF is password protected.";
            passwordForm.querySelector("input").focus();
        };
        passwordForm.addEventListener("submit", event => {
            event.preventDefault();
            passwordForm.hidden = true;
            loading.hidden = false;
            unlock(passwordForm.querySelector("input").value);
            passwordForm.querySelector("input").value = "";
        });
        query("[data-pdf-password-cancel]").addEventListener("click", () => {
            passwordForm.hidden = true;
            task.destroy();
            failed("PDF unlocking canceled.");
        });
        window.addEventListener("pagehide", event => { if (!event.persisted) { thumbnails.destroy(); navigation.destroy(); cancelAnimationFrame(fitFrame); observer.disconnect(); task.destroy(); } });
        const pdfDocument = await task.promise;
        viewer.setDocument(pdfDocument);
        linkService.setDocument(pdfDocument);
        thumbnails.setDocument(pdfDocument);
        root.dataset.pdfState = "loaded";
    }
    initialize().catch(exception => {
        root.dataset.pdfState = "error";
        failed(exception.name === "MissingPDFException" || exception.status === 404
            ? "The selected PDF is missing or no longer available."
            : exception.status === 409 ? "This PDF link is stale or no longer valid. Reopen it from its review page."
                : "This PDF could not be loaded in the reader.");
    });
})();
