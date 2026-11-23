import assert from "node:assert/strict";
import test from "node:test";
import {renderRegion} from "../../static/submissions/pdf_comparison_once.mjs";

test("full-page preview is bounded without distorting a rotated viewport", () => {
    const region = renderRegion({width: 842, height: 595}, [0, 0, 1, 1], 1400, 1190);
    assert.equal(region.width, 1400);
    assert.ok(Math.abs(region.width / region.height - 842 / 595) < 0.002);
    assert.equal(region.x, 0);
    assert.equal(region.y, 0);
});

test("magnification translates a cropped region in the same display coordinates", () => {
    assert.deepEqual(renderRegion({width: 800, height: 600}, [0.25, 0.5, 0.5, 0.75], 800, 800),
        {scale: 4, x: 800, y: 1200, width: 800, height: 600});
});

test("very small edge crops remain renderable and respect density limit", () => {
    const region = renderRegion({width: 842, height: 595}, [0.99999, 0.99999, 1, 1], 3072, 1000);
    assert.equal(region.scale, 8);
    assert.equal(region.width, 1);
    assert.equal(region.height, 1);
});
