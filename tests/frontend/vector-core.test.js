/**
 * vector-core: pure FeatureCollection helpers + client-side validation.
 *
 * These mirror tests/test_vector_utils.py — both must agree, since the
 * editor uses vector-core to surface errors before the submit round-trip.
 */
import { describe, it, expect } from "vitest";
import {
    addFeature, removeFeature, updateFeatureProps, updateFeatureGeometry,
    moveVertex, insertVertex, removeVertex,
    snapToEndpoint, SNAP_TOLERANCE_DEG,
    makeHistory, push, undo, redo,
    validateAttributes, validateTopology, validateBeforeSubmit,
    emptyFC,
} from "../../frontend/js/vector-core.js";


function _line(coords, props = {}) {
    return {
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords },
        properties: props,
    };
}


describe("vertex editing", () => {
    const base = () => addFeature(emptyFC(), [[0, 0], [1, 1], [2, 2]], { tipo: "rio" });

    it("moveVertex replaces one coordinate, preserving props and others", () => {
        const fc = moveVertex(base(), 0, 1, 5, 7);
        expect(fc.features[0].geometry.coordinates).toEqual([[0, 0], [5, 7], [2, 2]]);
        expect(fc.features[0].properties).toEqual({ tipo: "rio" });
    });

    it("moveVertex is a no-op for out-of-range indices", () => {
        const src = base();
        expect(moveVertex(src, 9, 0, 1, 1)).toBe(src);
        expect(moveVertex(src, 0, 9, 1, 1)).toBe(src);
    });

    it("insertVertex adds a point inside the chosen segment", () => {
        const fc = insertVertex(base(), 0, 0, 0.5, 0.5);  // between v0 and v1
        expect(fc.features[0].geometry.coordinates).toEqual([[0, 0], [0.5, 0.5], [1, 1], [2, 2]]);
    });

    it("insertVertex rejects a segment index past the last segment", () => {
        const src = base();
        expect(insertVertex(src, 0, 2, 9, 9)).toBe(src);  // only 2 segments (0,1)
    });

    it("removeVertex drops a point but keeps ≥2 vertices", () => {
        const fc = removeVertex(base(), 0, 1);
        expect(fc.features[0].geometry.coordinates).toEqual([[0, 0], [2, 2]]);
    });

    it("removeVertex refuses to go below 2 vertices", () => {
        const two = addFeature(emptyFC(), [[0, 0], [1, 1]]);
        expect(removeVertex(two, 0, 0)).toBe(two);
    });

    it("does not mutate the source FC", () => {
        const src = base();
        moveVertex(src, 0, 0, 9, 9);
        insertVertex(src, 0, 0, 9, 9);
        removeVertex(src, 0, 1);
        expect(src.features[0].geometry.coordinates).toEqual([[0, 0], [1, 1], [2, 2]]);
    });
});

describe("addFeature", () => {
    it("appends a LineString with copied coords + props", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]], { tipo: "rio" });
        expect(fc.features).toHaveLength(1);
        expect(fc.features[0].geometry.type).toBe("LineString");
        expect(fc.features[0].geometry.coordinates).toEqual([[0, 0], [1, 1]]);
        expect(fc.features[0].properties).toEqual({ tipo: "rio" });
    });

    it("does not mutate the source collection", () => {
        const src = emptyFC();
        addFeature(src, [[0, 0], [1, 1]]);
        expect(src.features).toHaveLength(0);
    });
});

describe("removeFeature", () => {
    it("removes by index, returns same FC for OOB", () => {
        let fc = addFeature(emptyFC(), [[0, 0], [1, 1]]);
        fc = addFeature(fc, [[2, 2], [3, 3]]);
        const after = removeFeature(fc, 0);
        expect(after.features).toHaveLength(1);
        expect(after.features[0].geometry.coordinates).toEqual([[2, 2], [3, 3]]);
        expect(removeFeature(fc, 99).features).toHaveLength(2);
    });
});

describe("updateFeatureProps", () => {
    it("merges properties without losing existing keys", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]], { tipo: "rio", existente: "x" });
        const updated = updateFeatureProps(fc, 0, { tipo: "arroio" });
        expect(updated.features[0].properties).toEqual({ tipo: "arroio", existente: "x" });
    });
});

describe("updateFeatureGeometry", () => {
    it("replaces coordinates", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]]);
        const moved = updateFeatureGeometry(fc, 0, [[5, 5], [6, 6]]);
        expect(moved.features[0].geometry.coordinates).toEqual([[5, 5], [6, 6]]);
    });
});

describe("snapToEndpoint", () => {
    it("returns the existing endpoint within tolerance", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]]);
        const eps = SNAP_TOLERANCE_DEG / 2;
        const snapped = snapToEndpoint(fc, [1.0 + eps, 1.0 - eps]);
        expect(snapped).toEqual([1, 1]);
    });

    it("returns null beyond tolerance", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]]);
        const far = SNAP_TOLERANCE_DEG * 100;
        expect(snapToEndpoint(fc, [1.0 + far, 1.0])).toBeNull();
    });

    it("ignores interior vertices", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [0.5, 0.5], [1, 1]]);
        // [0.5, 0.5] is interior; only [0,0] and [1,1] are snap candidates.
        const eps = SNAP_TOLERANCE_DEG / 2;
        expect(snapToEndpoint(fc, [0.5 + eps, 0.5 + eps])).toBeNull();
    });
});

describe("undo/redo", () => {
    it("rolls forward and back through edits", () => {
        let fc = emptyFC();
        let h = makeHistory();
        h = push(h, fc);
        fc = addFeature(fc, [[0, 0], [1, 1]]);
        h = push(h, fc);
        fc = addFeature(fc, [[2, 2], [3, 3]]);

        // Undo twice.
        let r = undo(h, fc);
        expect(r.snapshot.features).toHaveLength(1);
        r = undo(r.history, r.snapshot);
        expect(r.snapshot.features).toHaveLength(0);

        // Redo back.
        r = redo(r.history, r.snapshot);
        expect(r.snapshot.features).toHaveLength(1);
        r = redo(r.history, r.snapshot);
        expect(r.snapshot.features).toHaveLength(2);
    });

    it("push clears the redo stack", () => {
        let fc = emptyFC();
        let h = makeHistory();
        h = push(h, fc);
        const v1 = addFeature(fc, [[0, 0], [1, 1]]);
        h = push(h, v1);
        const r = undo(h, v1);
        // history.future now has v1 — but a new push must clear it.
        const h2 = push(r.history, r.snapshot);
        expect(h2.future).toEqual([]);
    });

    it("undo on empty history is a no-op", () => {
        const r = undo(makeHistory(), emptyFC());
        expect(r.snapshot).toEqual(emptyFC());
        expect(r.history.past).toEqual([]);
    });
});

describe("validateAttributes", () => {
    const schema = [
        { key: "tipo", type: "enum", required: true, options: ["a", "b"] },
        { key: "lanes", type: "number", required: false },
        { key: "iluminacao", type: "boolean", required: false },
    ];

    it("flags required missing", () => {
        const errs = validateAttributes([{ properties: {} }], schema);
        expect(errs.some(e => e.includes("tipo"))).toBe(true);
    });

    it("rejects out-of-options enum", () => {
        const errs = validateAttributes([{ properties: { tipo: "z" } }], schema);
        expect(errs.some(e => e.includes("'z'"))).toBe(true);
    });

    it("treats empty string as missing", () => {
        const errs = validateAttributes([{ properties: { tipo: "" } }], schema);
        expect(errs.some(e => e.includes("obrigatório"))).toBe(true);
    });

    it("rejects truthy-string for boolean", () => {
        const errs = validateAttributes(
            [{ properties: { tipo: "a", iluminacao: "true" } }],
            schema,
        );
        expect(errs.some(e => e.includes("boolean"))).toBe(true);
    });

    it("rejects a boolean for a number attribute (parity with backend guard)", () => {
        // typeof true === "boolean" → not a number; backend excludes bool too.
        const errs = validateAttributes([{ properties: { tipo: "a", lanes: true } }], schema);
        expect(errs.some(e => e.includes("lanes"))).toBe(true);
    });

    it("accepts a clean feature", () => {
        expect(validateAttributes(
            [{ properties: { tipo: "a", lanes: 2, iluminacao: true } }],
            schema,
        )).toEqual([]);
    });
});

describe("validateTopology", () => {
    it("requires direction property", () => {
        const errs = validateTopology([_line([[0, 0], [1, 1]])]);
        expect(errs.some(e => e.includes("direction"))).toBe(true);
    });

    it("accepts a chain", () => {
        expect(validateTopology([
            _line([[0, 0], [1, 1]], { direction: "forward" }),
            _line([[1, 1], [2, 2]], { direction: "forward" }),
        ])).toEqual([]);
    });

    it("detects a cycle", () => {
        const errs = validateTopology([
            _line([[0, 0], [1, 0]], { direction: "forward" }),
            _line([[1, 0], [1, 1]], { direction: "forward" }),
            _line([[1, 1], [0, 0]], { direction: "forward" }),
        ]);
        expect(errs.some(e => e.toLowerCase().includes("ciclo"))).toBe(true);
    });

    it("rejects an invalid direction VALUE (not just absence)", () => {
        // Parity with backend: 'north' is not in {forward,reverse,both}.
        const errs = validateTopology([_line([[0, 0], [1, 1]], { direction: "north" })]);
        expect(errs.some(e => e.includes("direction"))).toBe(true);
    });

    it("detects a cycle built from reverse edges", () => {
        // All edges reverse: b→a orientation, still forms a directed cycle.
        const errs = validateTopology([
            _line([[0, 0], [1, 0]], { direction: "reverse" }),
            _line([[1, 0], [1, 1]], { direction: "reverse" }),
            _line([[1, 1], [0, 0]], { direction: "reverse" }),
        ]);
        expect(errs.some(e => e.toLowerCase().includes("ciclo"))).toBe(true);
    });

    it("does NOT false-positive on a converging DAG (diamond)", () => {
        // a→b, a→c, b→d, c→d : two paths converge but there is no cycle.
        expect(validateTopology([
            _line([[0, 0], [1, 1]], { direction: "forward" }),
            _line([[0, 0], [1, -1]], { direction: "forward" }),
            _line([[1, 1], [2, 0]], { direction: "forward" }),
            _line([[1, -1], [2, 0]], { direction: "forward" }),
        ])).toEqual([]);
    });

    it("snaps endpoints within tolerance", () => {
        const eps = SNAP_TOLERANCE_DEG / 2;
        expect(validateTopology([
            _line([[0, 0], [1, 1]], { direction: "forward" }),
            _line([[1 + eps, 1 - eps], [2, 2]], { direction: "forward" }),
        ])).toEqual([]);
    });

    it("flags self-loop", () => {
        const errs = validateTopology([
            _line([[0, 0], [1, 1], [0, 0]], { direction: "forward" }),
        ]);
        expect(errs.some(e => e.includes("loop") || e.toLowerCase().includes("ciclo"))).toBe(true);
    });

    it("undirected edge counts both ways for cycle detection", () => {
        const errs = validateTopology([
            _line([[0, 0], [1, 0]], { direction: "both" }),
            _line([[1, 0], [0, 0]], { direction: "forward" }),
        ]);
        expect(errs.some(e => e.toLowerCase().includes("ciclo"))).toBe(true);
    });
});

describe("validateBeforeSubmit", () => {
    const schema = [
        { key: "tipo", type: "enum", required: true, options: ["rio"] },
    ];

    it("returns [] for clean input", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]], { tipo: "rio" });
        expect(validateBeforeSubmit(fc, schema)).toEqual([]);
    });

    it("rejects non-LineString geometry", () => {
        const bad = {
            type: "FeatureCollection",
            features: [{ type: "Feature",
                          geometry: { type: "Point", coordinates: [0, 0] },
                          properties: {} }],
        };
        const errs = validateBeforeSubmit(bad, schema);
        expect(errs.some(e => e.includes("LineString"))).toBe(true);
    });

    it("rejects single-vertex line", () => {
        const fc = { type: "FeatureCollection", features: [_line([[0, 0]])] };
        const errs = validateBeforeSubmit(fc, schema);
        expect(errs.some(e => e.includes("≥2"))).toBe(true);
    });

    it("rejects malformed FC root", () => {
        expect(validateBeforeSubmit(null, schema)).toEqual(["FeatureCollection inválida"]);
        expect(validateBeforeSubmit({ type: "X" }, schema)).toEqual(["FeatureCollection inválida"]);
    });

    it("aggregates topology errors when topology_required", () => {
        const fc = addFeature(emptyFC(), [[0, 0], [1, 1]], { tipo: "rio" });
        // No direction → topology error.
        const errs = validateBeforeSubmit(fc, schema, { topologyRequired: true });
        expect(errs.some(e => e.includes("direction"))).toBe(true);
    });
});
