// Pure editor decisions (no canvas, no MapLibre, no fetch). editor.js only
// orchestrates DOM around these; every rule here has Vitest coverage in
// tests/frontend/editor-core.test.js.

// Hold-to-show overlay layers bound to letter keys.
export const KEY_TO_OVERLAY = Object.freeze({
    d: "secondary", r: "tertiary", t: "ref_primary", y: "ref_secondary",
});

// Number of class-selection digit shortcuts (1..6).
export const MAX_NUMBER_SHORTCUTS = 6;

const LETTER_ACTIONS = Object.freeze({
    q: { action: "tool", arg: "brush" },
    w: { action: "tool", arg: "eraser" },
    e: { action: "tool", arg: "fill" },
    a: { action: "brush", arg: -1 },
    s: { action: "brush", arg: 1 },
    z: { action: "opacity", arg: -0.1 },
    x: { action: "opacity", arg: 0.1 },
    c: { action: "next-missing" },
    f: { action: "toggle-missing" },
});

// Map a keydown-like event ({key, ctrlKey, metaKey, altKey, shiftKey}) to an
// editor action, or null. Chords (Ctrl/Meta) only resolve to the chord
// actions (undo/redo/submit/switch-project); letter and digit shortcuts never
// fire while Ctrl, Meta or Alt is held — Ctrl+S must not grow the brush,
// Ctrl+C must not jump to a missing pixel, etc.
export function resolveShortcut(ev) {
    const key = ev?.key;
    if (typeof key !== "string" || !key) return null;
    const low = key.toLowerCase();
    const chord = !!(ev.ctrlKey || ev.metaKey);
    if (ev.altKey) return null;
    if (chord) {
        if (low === "z") return { action: ev.shiftKey ? "redo" : "undo" };
        if (low === "y") return { action: "redo" };
        if (low === "s") return { action: "submit" };
        if (low === "p") return { action: "switch-project" };
        return null;
    }
    if (key === " ") return { action: "hide-mask" };
    if (key.length === 1 && key >= "1" && key <= String(MAX_NUMBER_SHORTCUTS)) {
        return { action: "class", arg: Number(key) - 1 };
    }
    if (LETTER_ACTIONS[low]) return { ...LETTER_ACTIONS[low] };
    if (KEY_TO_OVERLAY[low]) return { action: "overlay", arg: KEY_TO_OVERLAY[low] };
    return null;
}

// Per-pixel grid geometry for the current tile geometry and zoom.
//   display  — canvas backing size in canvas units (768)
//   cssSize  — untransformed CSS size of the canvas stack (offsetWidth)
//   zoom     — current stack scale
// Returns cell size and line width in canvas units, the on-screen size of
// one mask pixel in CSS px, and whether the grid should be drawn at all
// (only when a pixel is at least `minScreenPx` CSS px — below that the lines
// just darken the imagery). The frame is `lineMeters` thick on the ground,
// capped at `maxLineFraction` of the cell so fine-resolution projects
// (meters_per_pixel ≤ 2.5) don't get lines that swallow the pixel.
export function gridGeometry({
    tilePx, metersPerPixel, display, cssSize = display, zoom = 1,
    lineMeters = 0.5, minScreenPx = 3, maxLineFraction = 0.2,
}) {
    const cell = display / tilePx;
    const screenPx = (cssSize / tilePx) * zoom;
    const mpp = metersPerPixel > 0 ? metersPerPixel : 2.5;
    const frac = Math.min(maxLineFraction, lineMeters / mpp);
    return { cell, lineWidth: cell * frac, screenPx, visible: screenPx >= minScreenPx };
}

// A preloaded mask may only be reused for the tile /next actually returned
// when it is the same tile at the same version (any pause/resume/admin
// mutation bumps the version) and that tile is not paused.
export function canReusePreload(cached, real) {
    if (!cached || !cached.tile || !cached.maskBytes || !real) return false;
    if (real.paused_at) return false;
    return cached.tile.id === real.id && cached.tile.version === real.version;
}

// Heartbeat answer meaning "the server paused this tile under you" (auto
// sweep after inactivity, or an admin pause). The editor must /resume.
export function heartbeatNeedsResume(resp) {
    return !!resp && resp.ok === false && resp.reason === "not_active";
}

// Visible modals: any `.modal` without `.hidden`. Topmost = last in DOM order.
export function topmostOpenModal(root = document) {
    const open = root.querySelectorAll(".modal:not(.hidden)");
    return open.length ? open[open.length - 1] : null;
}

export function isAnyModalOpen(root = document) {
    return topmostOpenModal(root) !== null;
}

// The control that dismisses a modal through its own handlers (so promise-
// based modals resolve and clean up instead of being hidden underneath).
export function modalCancelControl(modal) {
    if (!modal) return null;
    return modal.querySelector('[data-modal-cancel], [id$="-cancel"], [id$="-close"]');
}
