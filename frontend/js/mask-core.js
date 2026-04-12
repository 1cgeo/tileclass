// Pure mask manipulation functions — no DOM, no canvas, no I/O.
// Extracted from editor.js so they can be unit-tested headlessly.
// These operate on a Uint8Array(65536) mask and return metadata about changes.

export const TILE = 256;
export const PIXELS = TILE * TILE;
export const EMPTY = 255;

// Paint a square brush of radius r centered at (cx, cy) with `value`.
// Mutates `mask` in place. Returns { touched: Map<index, prevValue>, deltaFilled }.
export function paintAt(mask, cx, cy, value, r = 0, touched = new Map()) {
    let deltaFilled = 0;
    const x0 = Math.max(0, cx - r), x1 = Math.min(TILE, cx + r + 1);
    const y0 = Math.max(0, cy - r), y1 = Math.min(TILE, cy + r + 1);
    for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
            const i = y * TILE + x;
            const prev = mask[i];
            if (prev === value) continue;
            if (!touched.has(i)) touched.set(i, prev);
            if (prev === EMPTY && value !== EMPTY) deltaFilled++;
            else if (prev !== EMPTY && value === EMPTY) deltaFilled--;
            mask[i] = value;
        }
    }
    return { touched, deltaFilled, region: [x0, y0, x1, y1] };
}

// Bresenham line; calls paintAt at every step.
export function paintLine(mask, x0, y0, x1, y1, value, r = 0) {
    const touched = new Map();
    let delta = 0;
    const dx = Math.abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    const dy = -Math.abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
    let err = dx + dy, x = x0, y = y0;
    while (true) {
        const res = paintAt(mask, x, y, value, r, touched);
        delta += res.deltaFilled;
        if (x === x1 && y === y1) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; x += sx; }
        if (e2 <= dx) { err += dx; y += sy; }
    }
    return { touched, deltaFilled: delta };
}

// 4-way flood fill. Replaces contiguous region of `target` color with `replacement`.
export function floodFill(mask, cx, cy, replacement) {
    const target = mask[cy * TILE + cx];
    const touched = new Map();
    if (target === replacement) return { touched, deltaFilled: 0 };
    let delta = 0;
    const stack = [[cx, cy]];
    while (stack.length) {
        const [x, y] = stack.pop();
        if (x < 0 || x >= TILE || y < 0 || y >= TILE) continue;
        const i = y * TILE + x;
        if (mask[i] !== target) continue;
        touched.set(i, target);
        if (target === EMPTY && replacement !== EMPTY) delta++;
        else if (target !== EMPTY && replacement === EMPTY) delta--;
        mask[i] = replacement;
        stack.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]);
    }
    return { touched, deltaFilled: delta };
}

// Compact an in-gesture Map<index, prevValue> into paired typed arrays.
export function toUndoEntry(touchedMap) {
    const n = touchedMap.size;
    const positions = new Uint32Array(n);
    const prevValues = new Uint8Array(n);
    let i = 0;
    for (const [p, v] of touchedMap) { positions[i] = p; prevValues[i] = v; i++; }
    return { positions, prevValues };
}

// Apply an undo/redo entry to `mask`. Returns inverse entry for redo/undo.
export function applyPatch(mask, entry) {
    const { positions, prevValues } = entry;
    const newPrev = new Uint8Array(prevValues.length);
    let delta = 0;
    for (let i = 0; i < positions.length; i++) {
        const p = positions[i];
        newPrev[i] = mask[p];
        const v = prevValues[i];
        if (mask[p] === EMPTY && v !== EMPTY) delta++;
        else if (mask[p] !== EMPTY && v === EMPTY) delta--;
        mask[p] = v;
    }
    return { inverse: { positions, prevValues: newPrev }, deltaFilled: delta };
}

// Count non-255 pixels.
export function countFilled(mask) {
    let c = 0;
    for (let i = 0; i < mask.length; i++) if (mask[i] !== EMPTY) c++;
    return c;
}

// Screen-space (clientX, clientY) with a DOMRect → logical pixel in [0..TILE-1].
// Pure — rect can be any {left, top, width, height}.
export function screenToLogical(rect, clientX, clientY) {
    const sx = (clientX - rect.left) / rect.width;
    const sy = (clientY - rect.top) / rect.height;
    const x = Math.floor(sx * TILE);
    const y = Math.floor(sy * TILE);
    return [
        Math.max(0, Math.min(TILE - 1, x)),
        Math.max(0, Math.min(TILE - 1, y)),
    ];
}

// Validate a submission. Same semantics as backend mask_utils.validate_submission.
// Returns { ok: bool, missing: int } or throws if invalid value present.
export function validateSubmission(mask) {
    if (mask.length !== PIXELS) {
        throw new Error(`expected ${PIXELS} bytes, got ${mask.length}`);
    }
    let missing = 0;
    for (let i = 0; i < PIXELS; i++) {
        const v = mask[i];
        if (v === EMPTY) { missing++; continue; }
        if (v < 1 || v > 6) throw new Error(`invalid class value ${v} at ${i}`);
    }
    return { ok: missing === 0, missing };
}

// Bounded undo stack helper.
export function pushBounded(stack, entry, maxLen) {
    stack.push(entry);
    if (stack.length > maxLen) stack.shift();
}
