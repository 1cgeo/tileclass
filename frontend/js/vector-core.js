// Pure feature-collection logic for the vector editor.
//
// Mirrors backend/vector_utils.py: same validation rules so the editor
// can show errors inline before the submit round-trip. No DOM/MapLibre —
// the only state is the FeatureCollection passed in/out.

export const SNAP_TOLERANCE_DEG = 1.5e-5;

export const ALLOWED_DIRECTIONS = ["forward", "reverse", "both"];

export function emptyFC() {
    return { type: "FeatureCollection", features: [] };
}

export function addFeature(fc, coords, properties = {}) {
    const f = {
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords.map(c => [c[0], c[1]]) },
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

export function updateFeatureGeometry(fc, idx, coords) {
    if (idx < 0 || idx >= fc.features.length) return fc;
    return {
        ...fc,
        features: fc.features.map((f, i) =>
            i === idx
                ? { ...f, geometry: { type: "LineString",
                                      coordinates: coords.map(c => [c[0], c[1]]) } }
                : f,
        ),
    };
}

// ---- Vertex editing (move / insert / remove) -------------------------------
//
// Operate on one feature's LineString immutably (properties preserved via
// updateFeatureGeometry). Out-of-range indices are no-ops returning the same
// FC, so the caller never has to guard.

export function moveVertex(fc, featureIdx, vertexIdx, lng, lat) {
    const f = fc.features[featureIdx];
    if (!f) return fc;
    const c = f.geometry?.coordinates || [];
    if (vertexIdx < 0 || vertexIdx >= c.length) return fc;
    const coords = c.map((p, i) => (i === vertexIdx ? [lng, lat] : [p[0], p[1]]));
    return updateFeatureGeometry(fc, featureIdx, coords);
}

// Insert a vertex into the segment between vertices segmentIdx and segmentIdx+1.
export function insertVertex(fc, featureIdx, segmentIdx, lng, lat) {
    const f = fc.features[featureIdx];
    if (!f) return fc;
    const c = f.geometry?.coordinates || [];
    if (segmentIdx < 0 || segmentIdx >= c.length - 1) return fc;
    const coords = [];
    for (let i = 0; i < c.length; i++) {
        coords.push([c[i][0], c[i][1]]);
        if (i === segmentIdx) coords.push([lng, lat]);
    }
    return updateFeatureGeometry(fc, featureIdx, coords);
}

// Remove a vertex. Refuses (returns the same FC) if it would drop the line
// below the 2 vertices a LineString requires.
export function removeVertex(fc, featureIdx, vertexIdx) {
    const f = fc.features[featureIdx];
    if (!f) return fc;
    const c = f.geometry?.coordinates || [];
    if (vertexIdx < 0 || vertexIdx >= c.length) return fc;
    if (c.length <= 2) return fc;
    const coords = c.filter((_, i) => i !== vertexIdx).map(p => [p[0], p[1]]);
    return updateFeatureGeometry(fc, featureIdx, coords);
}

// ---- Snap-to-vertex --------------------------------------------------------

// Returns the existing vertex closest to `point` within `tolerance` degrees,
// or null. Scans only line endpoints — interior vertices are ignored because
// shared interior points don't form graph nodes.
export function snapToEndpoint(fc, point, tolerance = SNAP_TOLERANCE_DEG) {
    let best = null;
    let bestD = tolerance;
    const [px, py] = point;
    for (const f of fc.features) {
        const c = f.geometry?.coordinates;
        if (!c || c.length < 2) continue;
        for (const idx of [0, c.length - 1]) {
            const [vx, vy] = c[idx];
            const d = Math.max(Math.abs(px - vx), Math.abs(py - vy));
            if (d <= bestD) {
                bestD = d;
                best = [vx, vy];
            }
        }
    }
    return best;
}

// ---- Undo/redo via patches -------------------------------------------------
//
// Storing the full FC for every step is wasteful for very large projects;
// patches are minimal "before/after FC" pairs but reified as plain values
// so the UI can replay them without serialization.

export function makeHistory() {
    return { past: [], future: [] };
}

export function push(history, snapshot) {
    return {
        past: [...history.past.slice(-49), snapshot],  // cap 50 frames
        future: [],  // any new edit clears the redo stack
    };
}

export function undo(history, current) {
    if (!history.past.length) return { history, snapshot: current };
    const prev = history.past[history.past.length - 1];
    return {
        history: {
            past: history.past.slice(0, -1),
            future: [current, ...history.future],
        },
        snapshot: prev,
    };
}

export function redo(history, current) {
    if (!history.future.length) return { history, snapshot: current };
    const next = history.future[0];
    return {
        history: {
            past: [...history.past, current],
            future: history.future.slice(1),
        },
        snapshot: next,
    };
}

// ---- Validation (mirror of backend.vector_utils) --------------------------

export function validateAttributes(features, schema) {
    const errors = [];
    features.forEach((f, i) => {
        const props = f.properties || {};
        for (const a of schema || []) {
            const value = props[a.key];
            if (value === undefined || value === null || value === "") {
                if (a.required) errors.push(`feature[${i}]: '${a.key}' obrigatório`);
                continue;
            }
            if (a.type === "number") {
                if (typeof value !== "number" || Number.isNaN(value))
                    errors.push(`feature[${i}].${a.key}: número esperado`);
            } else if (a.type === "boolean") {
                if (typeof value !== "boolean")
                    errors.push(`feature[${i}].${a.key}: boolean esperado`);
            } else if (a.type === "enum") {
                if (!(a.options || []).includes(value))
                    errors.push(`feature[${i}].${a.key}: '${value}' fora das opções`);
            } else if (a.type === "text") {
                if (typeof value !== "string")
                    errors.push(`feature[${i}].${a.key}: texto esperado`);
            }
        }
    });
    return errors;
}

export function validateTopology(features) {
    const errors = [];
    if (!features.length) return errors;
    for (const [i, f] of features.entries()) {
        const d = (f.properties || {}).direction;
        if (!ALLOWED_DIRECTIONS.includes(d)) {
            errors.push(`feature[${i}].direction: forward/reverse/both`);
        }
    }
    if (errors.length) return errors;

    // Build node graph.
    const nodes = [];
    const edges = [];
    const snapIdx = (p) => {
        for (let i = 0; i < nodes.length; i++) {
            if (Math.abs(p[0] - nodes[i][0]) < SNAP_TOLERANCE_DEG
                && Math.abs(p[1] - nodes[i][1]) < SNAP_TOLERANCE_DEG)
                return i;
        }
        nodes.push([p[0], p[1]]);
        return nodes.length - 1;
    };
    for (const f of features) {
        const c = f.geometry.coordinates;
        const a = snapIdx(c[0]);
        const b = snapIdx(c[c.length - 1]);
        if (a === b) {
            errors.push("feature termina no mesmo nó (loop de 1 aresta)");
            continue;
        }
        edges.push([a, b, (f.properties || {}).direction]);
    }
    if (errors.length) return errors;
    const adj = nodes.map(() => []);
    for (const [a, b, d] of edges) {
        if (d === "forward") adj[a].push(b);
        else if (d === "reverse") adj[b].push(a);
        else { adj[a].push(b); adj[b].push(a); }
    }
    const color = nodes.map(() => 0);
    const dfs = (start) => {
        const stack = [[start, 0]];
        color[start] = 1;
        while (stack.length) {
            const top = stack[stack.length - 1];
            const [n, ci] = top;
            const out = adj[n];
            if (ci >= out.length) {
                color[n] = 2;
                stack.pop();
                continue;
            }
            top[1] = ci + 1;
            const nx = out[ci];
            if (color[nx] === 1) return true;
            if (color[nx] === 0) {
                color[nx] = 1;
                stack.push([nx, 0]);
            }
        }
        return false;
    };
    for (let i = 0; i < nodes.length; i++) {
        if (color[i] === 0 && dfs(i)) {
            errors.push("ciclo detectado no grafo (drenagem precisa ser acíclica)");
            break;
        }
    }
    return errors;
}

export function validateBeforeSubmit(fc, schema, { topologyRequired = false } = {}) {
    if (!fc || fc.type !== "FeatureCollection" || !Array.isArray(fc.features)) {
        return ["FeatureCollection inválida"];
    }
    for (const [i, f] of fc.features.entries()) {
        if (f.geometry?.type !== "LineString") {
            return [`feature[${i}]: LineString esperado`];
        }
        if (!Array.isArray(f.geometry.coordinates) || f.geometry.coordinates.length < 2) {
            return [`feature[${i}]: ≥2 vértices`];
        }
    }
    const errs = validateAttributes(fc.features, schema);
    if (topologyRequired) errs.push(...validateTopology(fc.features));
    return errs;
}
