// Keep the inspection source in this page only. The comparison workspace owns
// selection, overlays and magnifier behavior for both kinds of PDF source.
export function renderRegion(viewport, box, width, height) {
    const [x0, y0, x1, y1] = box;
    const scale = Math.min(width / ((x1 - x0) * viewport.width), height / ((y1 - y0) * viewport.height), 8);
    return {
        scale, x: x0 * viewport.width * scale, y: y0 * viewport.height * scale,
        width: Math.max(1, Math.ceil((x1 - x0) * viewport.width * scale)),
        height: Math.max(1, Math.ceil((y1 - y0) * viewport.height * scale)),
    };
}

function pageBlob(canvas) {
    return new Promise((resolve, reject) => canvas.toBlob(blob => {
        if (blob) resolve(blob);
        else reject(new Error("Page rendering failed."));
    }, "image/png"));
}

function openRenderer(pdfjs, runtime, bytes, data) {
    const tasks = {};
    for (const side of ["before", "after"]) {
        tasks[side] = pdfjs.getDocument({
            data: new Uint8Array(bytes[side]), isEvalSupported: false, enableXfa: false,
            cMapUrl: new URL("cmaps/", runtime).href, cMapPacked: true,
            standardFontDataUrl: new URL("standard_fonts/", runtime).href,
            wasmUrl: new URL("wasm/", runtime).href, iccUrl: new URL("iccs/", runtime).href,
        });
    }

    async function render(side, meta, signal, box, w, h, output) {
        signal.throwIfAborted();
        const pdf = await tasks[side].promise;
        const page = await pdf.getPage(meta.number);
        signal.throwIfAborted();
        const viewport = page.getViewport({scale: 1});
        // Refuse to project evidence onto a page interpreted at another size.
        if (Math.abs(meta.width - viewport.width) > 0.5 || Math.abs(meta.height - viewport.height) > 0.5) {
            throw new Error("Browser page geometry does not match comparison evidence.");
        }
        const region = renderRegion(viewport, box, w, h);
        const canvas = document.createElement("canvas");
        canvas.width = region.width;
        canvas.height = region.height;
        const job = page.render({canvas, viewport: page.getViewport({scale: region.scale}),
            transform: [1, 0, 0, 1, -region.x, -region.y]});
        const cancel = () => job.cancel();
        signal.addEventListener("abort", cancel, {once: true});
        try {
            await job.promise;
            signal.throwIfAborted();
            return await output(canvas);
        } finally {
            signal.removeEventListener("abort", cancel);
            page.cleanup();
            canvas.width = canvas.height = 0;
        }
    }

    return {
        ready: Promise.all(Object.entries(tasks).map(async ([side, task]) => {
            const pdf = await task.promise;
            if (pdf.numPages !== data[`${side}_pages`].length) throw new Error("Browser page count does not match comparison evidence.");
        })),
        page(side, meta, signal) {
            return render(side, meta, signal, [0, 0, 1, 1], Math.min(1400, meta.width * 2), Math.min(2000, meta.height * 2), pageBlob);
        },
        crop(side, meta, box, width, height, signal) {
            const cap = Math.min(1, 3072 / width, 3072 / height, Math.sqrt(4_000_000 / (width * height)));
            return render(side, meta, signal, box, Math.max(1, Math.floor(width * cap)), Math.max(1, Math.floor(height * cap)), createImageBitmap);
        },
        dispose() { Object.values(tasks).forEach(task => task.destroy().catch(() => {})); },
    };
}

function initialize(form) {
    const workspace = document.querySelector("[data-comparison-once-workspace]");
    const sources = form.closest("details");
    const baseline = form.elements.baseline;
    const input = form.querySelector("input[type=file]");
    const button = form.querySelector("button[type=submit]");
    const status = form.querySelector("[data-comparison-once-status]");
    const error = form.querySelector("[data-comparison-once-error]");
    const endpoint = new URL(form.dataset.inputUrl, location.href);
    const runtime = new URL(form.dataset.pdfRuntime, location.href);
    let pending, renderer, unmount;

    function clear() {
        pending?.abort();
        pending = null;
        unmount?.();
        unmount = null;
        renderer?.dispose();
        renderer = null;
        workspace.replaceChildren();
        status.hidden = error.hidden = true;
        error.textContent = "";
        button.disabled = !input.files.length;
    }

    async function checked(response) {
        if (response.ok) return response;
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `Comparison unavailable (HTTP ${response.status}). No files were changed.`);
    }

    baseline.addEventListener("change", clear);
    input.addEventListener("change", clear);
    form.addEventListener("submit", async event => {
        event.preventDefault();
        if (pending) return;
        clear();
        const file = input.files[0];
        if (!file) return;
        if (!file.size || file.size > Number(form.dataset.maxBytes)) {
            error.textContent = "Choose a non-empty PDF of at most 64 MiB.";
            error.hidden = false;
            return;
        }
        const request = new AbortController();
        pending = request;
        const options = {signal: request.signal, cache: "no-store", redirect: "error"};
        button.disabled = true;
        status.hidden = false;
        status.textContent = "Loading selected PDFs...";
        try {
            const pdfjs = await import(new URL("legacy/build/pdf.min.mjs", runtime).href);
            pdfjs.GlobalWorkerOptions.workerSrc = new URL("legacy/build/pdf.worker.min.mjs", runtime).href;
            const selection = new URL(endpoint);
            selection.searchParams.set("baseline", baseline.value);
            const base = await checked(await fetch(selection, options));
            const token = base.headers.get("X-CFM-Baseline");
            if (!token) throw new Error("The selected baseline could not be verified.");
            const before = await base.arrayBuffer();
            const after = await file.arrayBuffer();
            request.signal.throwIfAborted();
            status.textContent = "Comparing PDFs on local CPU...";
            const response = await checked(await fetch(endpoint, {...options, method: "POST", body: after, headers: {
                "Content-Type": "application/pdf", "X-CSRFToken": form.elements.csrfmiddlewaretoken.value,
                "X-CFM-Baseline": token, "X-CFM-Draft-Name": encodeURIComponent(file.name),
            }}));
            const {data, workspace: markup} = await response.json();
            request.signal.throwIfAborted();
            renderer = openRenderer(pdfjs, runtime, {before, after}, data);
            await renderer.ready;
            request.signal.throwIfAborted();
            if (form.contains(document.activeElement)) sources.querySelector("summary").focus({preventScroll: true});
            sources.open = false;
            status.hidden = true;
            workspace.innerHTML = markup;
            unmount = window.CFMComparison.mount(workspace.querySelector("[data-pdf-comparison]"), data, renderer);
        } catch (failure) {
            if (pending !== request) return;
            clear();
            sources.open = true;
            error.textContent = failure.message || "Comparison unavailable. No files were changed.";
            error.hidden = false;
        } finally {
            if (pending === request) {
                pending = null;
                button.disabled = !input.files.length;
            }
        }
    });
    function discard() {
        form.reset();
        clear();
        sources.open = true;
    }
    window.addEventListener("pagehide", discard);
    window.addEventListener("pageshow", event => { if (event.persisted) discard(); });
    clear();
}

if (typeof document !== "undefined") {
    const form = document.querySelector("[data-comparison-once]");
    if (form) initialize(form);
}
