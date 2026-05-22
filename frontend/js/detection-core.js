// Pure box-collection logic for the detection editor.
//
// Mirrors backend/detection_utils.py: a box is a GeoJSON Polygon whose outer
// ring is an axis-aligned rectangle, with properties.class_id. No DOM/MapLibre
// — the only state is the FeatureCollection passed in/out. The undo/redo
// helpers are shared with the vector editor (identical snapshot semantics).

import { makeHistory, push, undo, redo } from "./vector-core.js";

export { makeHistory, push, undo, redo };

export function emptyFC() {
    return { type: "FeatureCollection", features: [] };
}

// Closed ring (5 points) for the axis-aligned rectangle spanning two corners.
export function rectRing(p0, p1) {
    const w = Math.min(p0[0], p1[0]);
    const e = Math.max(p0[0], p1[0]);
    const s = Math.min(p0[1], p1[1]);
    const n = Math.max(p0[1], p1[1]);
    return [[w, s], [e, s], [e, n], [w, n], [w, s]];
}

export function addBox(fc, p0, p1, properties = {}) {
    const f = {
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [rectRing(p0, p1)] },
        properties: { ...properties },
    };
    return { ...fc, features: [...fc.features, f] };
}

export function removeFeature(fc, idx) {
    if (idx < 0 || idx >= fc.features.length) return fc;
    return { ...fc, features: fc.features.filter((_, i) => i !== idx) };
}

export function updateFeatureProps(fc, idx, patch) {
    if (idx < 0 || idx >= fc.features.length) return fc;
    return {
        ...fc,
        features: fc.features.map((f, i) =>
            i === idx
                ? { ...f, properties: { ...(f.properties || {}), ...patch } }
                : f,
        ),
    };
}

// Bounding box [w, s, e, n] of a box feature, or null when malformed.
export function featureBounds(f) {
    const ring = f.geometry?.coordinates?.[0];
    if (!Array.isArray(ring) || ring.length < 4) return null;
    const xs = ring.map(c => c[0]);
    const ys = ring.map(c => c[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

// Index of the topmost (last-drawn) box containing the point, or -1. Used by
// the select tool — later features sit visually on top, so scan in reverse.
export function hitTest(fc, point) {
    const [px, py] = point;
    for (let i = fc.features.length - 1; i >= 0; i--) {
        const b = featureBounds(fc.features[i]);
        if (b && px >= b[0] && px <= b[2] && py >= b[1] && py <= b[3]) return i;
    }
    return -1;
}

// Discard degenerate boxes (zero width/height) — a stray click shouldn't
// create an invisible box. tolerance in degrees (~0.1 m).
export function isDegenerate(p0, p1, tolerance = 1e-7) {
    return Math.abs(p0[0] - p1[0]) < tolerance || Math.abs(p0[1] - p1[1]) < tolerance;
}

// ---- Validation (mirror of backend.detection_utils) -----------------------

export function validateBeforeSubmit(fc, allowedIds, { boxRequired = false } = {}) {
    if (!fc || fc.type !== "FeatureCollection" || !Array.isArray(fc.features)) {
        return ["FeatureCollection inválida"];
    }
    const allowed = new Set(allowedIds || []);
    const errors = [];
    fc.features.forEach((f, i) => {
        const ring = f.geometry?.type === "Polygon" ? f.geometry.coordinates?.[0] : null;
        if (!Array.isArray(ring) || ring.length < 4) {
            errors.push(`feature[${i}]: Polygon (caixa) esperado`);
            return;
        }
        const xs = new Set(ring.map(c => Math.round(c[0] * 1e9) / 1e9));
        const ys = new Set(ring.map(c => Math.round(c[1] * 1e9) / 1e9));
        if (xs.size !== 2 || ys.size !== 2) {
            errors.push(`feature[${i}]: caixa deve ser retângulo alinhado aos eixos`);
        }
        const cid = (f.properties || {}).class_id;
        if (!Number.isInteger(cid)) {
            errors.push(`feature[${i}]: class_id obrigatório`);
        } else if (allowed.size && !allowed.has(cid)) {
            errors.push(`feature[${i}].class_id=${cid} fora da paleta`);
        }
    });
    if (boxRequired && !fc.features.length) {
        errors.push("tile precisa de ao menos uma caixa");
    }
    return errors;
}
