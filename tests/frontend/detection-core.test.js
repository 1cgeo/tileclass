/**
 * detection-core: pure box-collection helpers + client-side validation.
 *
 * These mirror backend/detection_utils.py — both must agree, since the editor
 * uses detection-core to surface errors before the submit round-trip.
 */
import { describe, it, expect } from "vitest";
import {
    emptyFC, rectRing, addBox, removeFeature, updateFeatureProps,
    featureBounds, hitTest, isDegenerate, validateBeforeSubmit,
    makeHistory, push, undo, redo,
} from "../../frontend/js/detection-core.js";


describe("rectRing", () => {
    it("builds a closed axis-aligned ring regardless of corner order", () => {
        const ring = rectRing([2, 3], [0, 1]);  // corners swapped
        expect(ring).toEqual([[0, 1], [2, 1], [2, 3], [0, 3], [0, 1]]);
        // First and last vertex coincide (closed).
        expect(ring[0]).toEqual(ring[ring.length - 1]);
    });
});

describe("addBox / removeFeature / updateFeatureProps", () => {
    it("adds a box Polygon with class props", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], { class_id: 2 });
        expect(fc.features).toHaveLength(1);
        expect(fc.features[0].geometry.type).toBe("Polygon");
        expect(fc.features[0].properties.class_id).toBe(2);
    });
    it("does not mutate the input FC (immutability)", () => {
        const fc0 = emptyFC();
        addBox(fc0, [0, 0], [1, 1], { class_id: 1 });
        expect(fc0.features).toHaveLength(0);
    });
    it("removes by index and re-classes by index", () => {
        let fc = addBox(addBox(emptyFC(), [0, 0], [1, 1], { class_id: 1 }),
                        [2, 2], [3, 3], { class_id: 1 });
        fc = updateFeatureProps(fc, 0, { class_id: 9 });
        expect(fc.features[0].properties.class_id).toBe(9);
        fc = removeFeature(fc, 0);
        expect(fc.features).toHaveLength(1);
        expect(fc.features[0].properties.class_id).toBe(1);
    });
});

describe("featureBounds / hitTest", () => {
    const fc = addBox(addBox(emptyFC(), [0, 0], [2, 2], { class_id: 1 }),
                      [1, 1], [3, 3], { class_id: 2 });
    it("computes [w,s,e,n]", () => {
        expect(featureBounds(fc.features[0])).toEqual([0, 0, 2, 2]);
    });
    it("returns the topmost (last-drawn) box under the point", () => {
        // (1.5,1.5) is inside both; the later box (idx 1) wins.
        expect(hitTest(fc, [1.5, 1.5])).toBe(1);
        // (0.5,0.5) only inside the first.
        expect(hitTest(fc, [0.5, 0.5])).toBe(0);
        // outside everything
        expect(hitTest(fc, [9, 9])).toBe(-1);
    });
});

describe("isDegenerate", () => {
    it("flags zero-width / zero-height boxes", () => {
        expect(isDegenerate([1, 1], [1, 2])).toBe(true);   // zero width
        expect(isDegenerate([1, 1], [2, 1])).toBe(true);   // zero height
        expect(isDegenerate([1, 1], [2, 2])).toBe(false);
    });
});

describe("validateBeforeSubmit", () => {
    const allowed = [1, 2];
    it("passes for valid boxes with known classes", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], { class_id: 1 });
        expect(validateBeforeSubmit(fc, allowed)).toEqual([]);
    });
    it("rejects out-of-palette class", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], { class_id: 7 });
        expect(validateBeforeSubmit(fc, allowed).length).toBe(1);
    });
    it("rejects a box without class_id", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], {});
        expect(validateBeforeSubmit(fc, allowed)[0]).toMatch(/class_id/);
    });
    it("rejects non-rectangle polygons", () => {
        const tri = {
            type: "FeatureCollection",
            features: [{
                type: "Feature", properties: { class_id: 1 },
                geometry: { type: "Polygon", coordinates: [[[0, 0], [2, 0], [1, 2], [0, 0]]] },
            }],
        };
        expect(validateBeforeSubmit(tri, allowed).some(e => /retângulo/.test(e))).toBe(true);
    });
    it("enforces boxRequired only when set", () => {
        expect(validateBeforeSubmit(emptyFC(), allowed, { boxRequired: true })).toHaveLength(1);
        expect(validateBeforeSubmit(emptyFC(), allowed, { boxRequired: false })).toEqual([]);
    });
});

describe("malformed input handling", () => {
    it("featureBounds returns null on a too-short / missing ring", () => {
        expect(featureBounds({ geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 1]]] } })).toBeNull();
        expect(featureBounds({ geometry: {} })).toBeNull();
        expect(featureBounds({})).toBeNull();
    });
    it("hitTest skips features whose bounds are null", () => {
        const fc = {
            type: "FeatureCollection",
            features: [
                { type: "Feature", properties: { class_id: 1 }, geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 1]]] } }, // malformed
                ...addBox(emptyFC(), [0, 0], [2, 2], { class_id: 2 }).features,
            ],
        };
        // Point inside the valid box → index 1 (malformed at 0 is skipped, not crashed).
        expect(hitTest(fc, [1, 1])).toBe(1);
    });
});

describe("JS↔Python parity on class_id rejection", () => {
    // Python detection_utils rejects non-int and bool class_id; JS Number.isInteger
    // must agree so the editor blocks the same payloads the backend would 422.
    const allowed = [1, 2];
    it("rejects float class_id", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], { class_id: 1.5 });
        expect(validateBeforeSubmit(fc, allowed).length).toBeGreaterThan(0);
    });
    it("rejects boolean class_id (true is not an integer class)", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], { class_id: true });
        expect(validateBeforeSubmit(fc, allowed).length).toBeGreaterThan(0);
    });
    it("rejects string class_id", () => {
        const fc = addBox(emptyFC(), [0, 0], [1, 1], { class_id: "1" });
        expect(validateBeforeSubmit(fc, allowed).length).toBeGreaterThan(0);
    });
});

describe("undo/redo (shared with vector-core)", () => {
    it("round-trips a snapshot", () => {
        let h = makeHistory();
        const a = emptyFC();
        const b = addBox(a, [0, 0], [1, 1], { class_id: 1 });
        h = push(h, a);
        const u = undo(h, b);
        expect(u.snapshot).toBe(a);
        const r = redo(u.history, u.snapshot);
        expect(r.snapshot).toBe(b);
    });
});
