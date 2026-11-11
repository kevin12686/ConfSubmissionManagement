(function () {
    "use strict";
    // Each side retains at most one decoded crop. Never persist signed URLs or pixels.
    window.CFMComparisonDetail = {
        create() {
            const slots = new Map();
            function clear(side) {
                const slot = slots.get(side);
                if (!slot) return;
                slots.delete(side);
                clearTimeout(slot.timer);
                slot.controller?.abort();
                slot.bitmap?.close();
            }
            return {
                clear,
                request(side, url, ready, failed) {
                    if (!window.fetch || !window.createImageBitmap || !window.AbortController) return;
                    const previous = slots.get(side);
                    if (previous?.url === url) {
                        previous.ready = ready; previous.failed = failed;
                        if (previous.bitmap) ready(previous.bitmap);
                        else if (previous.error !== undefined) failed(previous.error);
                        return;
                    }
                    clear(side);
                    const slot = {url, ready, failed};
                    slots.set(side, slot);
                    slot.timer = setTimeout(async () => {
                        slot.controller = new window.AbortController();
                        const timeout = setTimeout(() => slot.controller.abort(), 10000);
                        try {
                            const response = await window.fetch(url, {signal: slot.controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error"});
                            if (!response.ok) throw new Error(response.status === 409 ? "invalid_snapshot" : "unavailable");
                            if (!(response.headers.get("Content-Type") || "").startsWith("image/png")) throw new Error("unavailable");
                            const bitmap = await window.createImageBitmap(await response.blob());
                            if (slots.get(side) !== slot) { bitmap.close(); return; }
                            slot.bitmap = bitmap;
                            slot.ready(bitmap);
                        } catch (error) {
                            if (slots.get(side) === slot) {
                                slot.error = error.message === "invalid_snapshot";
                                slot.failed(slot.error);
                            }
                        } finally {
                            clearTimeout(timeout);
                        }
                    }, 160);
                },
                dispose() { for (const side of slots.keys()) clear(side); },
            };
        },
    };
})();
