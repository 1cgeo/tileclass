// Pure mask manipulation functions — no DOM, no canvas, no I/O.
// Extracted from editor.js so they can be unit-tested headlessly.
// Each function takes `tilePx` so the editor can run at any project tile size.
// Defaults to 256 keep tests + the seed default project working unchanged.

export const TILE = 256;
export const PIXELS = TILE * TILE;
export const EMPTY = 255;

// Paint a square brush of radius r centered at (cx, cy) with `value`.
// Mutates `mask` in place. Returns { touched: Map<index, prevValue>, deltaFilled }.
export function paintAt(mask, cx, cy, value, r = 0, touched = new Map(),
                        tilePx = TILE) {
    let deltaFilled = 0;
    const x0 = Math.max(0, cx - r), x1 = Math.min(tilePx, cx + r + 1);
    const y0 = Math.max(0, cy - r), y1 = Math.min(tilePx, cy + r + 1);
    for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
            const i = y * tilePx + x;
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
export function paintLine(mask, x0, y0, x1, y1, value, r = 0,
                          tilePx = TILE) {
    const touched = new Map();
    let delta = 0;
    const dx = Math.abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    const dy = -Math.abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
    let err = dx + dy, x = x0, y = y0;
    while (true) {
        const res = paintAt(mask, x, y, value, r, touched, tilePx);
        delta += res.deltaFilled;
        if (x === x1 && y === y1) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; x += sx; }
        if (e2 <= dx) { err += dx; y += sy; }
    }
    return { touched, deltaFilled: delta };
}

// 4-way flood fill. Replaces the contiguous (4-connected) region of the seed's
// color with `replacement`. Returns the undo entry directly in the CLAUDE.md
// format — { positions: Uint32Array, prevValues: Uint8Array } — plus
// deltaFilled. Memory is O(region) in typed arrays: pixels are painted as
// they are pushed (the mask doubles as the visited set, since replacement
// ≠ target), so each index enters the stack at most once.
function _grow(arr, need, cap) {
    // Never exceeds `cap` (= tile pixels): each index is pushed at most once.
    if (need <= arr.length || arr.length >= cap) return arr;
    const next = new Uint32Array(Math.min(cap, Math.max(need, arr.length * 2)));
    next.set(arr);
    return next;
}

export function floodFill(mask, cx, cy, replacement, tilePx = TILE) {
    const n = tilePx * tilePx;
    const start = cy * tilePx + cx;
    const target = mask[start];
    if (target === replacement || cx < 0 || cy < 0 || cx >= tilePx || cy >= tilePx) {
        return { positions: new Uint32Array(0), prevValues: new Uint8Array(0), deltaFilled: 0 };
    }
    const initial = Math.min(n, 4096);
    let positions = new Uint32Array(initial);
    let stack = new Uint32Array(initial);
    let count = 0, sp = 0;
    mask[start] = replacement;
    positions[count++] = start;
    stack[sp++] = start;
    while (sp > 0) {
        const i = stack[--sp];
        const x = i % tilePx;
        // Up to 4 pushes per pop: make room once, not per neighbor.
        if (sp + 4 > stack.length) stack = _grow(stack, sp + 4, n);
        if (count + 4 > positions.length) positions = _grow(positions, count + 4, n);
        let j;
        if (x + 1 < tilePx && mask[j = i + 1] === target) {
            mask[j] = replacement; positions[count++] = j; stack[sp++] = j;
        }
        if (x > 0 && mask[j = i - 1] === target) {
            mask[j] = replacement; positions[count++] = j; stack[sp++] = j;
        }
        if (i + tilePx < n && mask[j = i + tilePx] === target) {
            mask[j] = replacement; positions[count++] = j; stack[sp++] = j;
        }
        if (i >= tilePx && mask[j = i - tilePx] === target) {
            mask[j] = replacement; positions[count++] = j; stack[sp++] = j;
        }
    }
    let deltaFilled = 0;
    if (target === EMPTY && replacement !== EMPTY) deltaFilled = count;
    else if (target !== EMPTY && replacement === EMPTY) deltaFilled = -count;
    return {
        positions: count === positions.length ? positions : positions.slice(0, count),
        prevValues: new Uint8Array(count).fill(target),
        deltaFilled,
    };
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

// Screen-space (clientX, clientY) with a DOMRect → logical pixel in [0..tilePx-1].
// Pure — rect can be any {left, top, width, height}.
export function screenToLogical(rect, clientX, clientY, tilePx = TILE) {
    const sx = (clientX - rect.left) / rect.width;
    const sy = (clientY - rect.top) / rect.height;
    const x = Math.floor(sx * tilePx);
    const y = Math.floor(sy * tilePx);
    return [
        Math.max(0, Math.min(tilePx - 1, x)),
        Math.max(0, Math.min(tilePx - 1, y)),
    ];
}

// Validate a submission. Same semantics as backend mask_utils.validate_submission.
// `validClasses` is an iterable of allowed non-255 class IDs (defaults to 1..6 to
// preserve callers that don't pass it). Returns { ok, missing } or throws.
export function validateSubmission(mask, validClasses = [1, 2, 3, 4, 5, 6],
                                   tilePx = TILE) {
    const expected = (tilePx * tilePx);
    if (mask.length !== expected) {
        throw new Error(`expected ${expected} bytes, got ${mask.length}`);
    }
    const allowed = validClasses instanceof Set ? validClasses : new Set(validClasses);
    let missing = 0;
    for (let i = 0; i < expected; i++) {
        const v = mask[i];
        if (v === EMPTY) { missing++; continue; }
        if (!allowed.has(v)) throw new Error(`invalid class value ${v} at ${i}`);
    }
    return { ok: missing === 0, missing };
}

// Bounded undo stack helper.
export function pushBounded(stack, entry, maxLen) {
    stack.push(entry);
    if (stack.length > maxLen) stack.shift();
}
