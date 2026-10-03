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
    if (key === "?") return { action: "help" };
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

// ---- Presentation helpers (header, stage, submit button) -------------------

// Zoom that fits the DISPLAY-sized paint area inside the stage viewport with
// `padX`/`padY` CSS px of room on each side (default `padding`), never above
// `max` (no upscaling past 1:1 on large screens) nor below `min`. The editor
// passes a larger padY so the floating stage chrome (HUD, zoom cluster)
// never covers paintable pixels. Unknown viewport sizes (hidden view) → max.
export function fitZoom(viewW, viewH, display, { padding = 16, padX = padding, padY = padding, max = 1, min = 0.25 } = {}) {
    if (!(viewW > 0) || !(viewH > 0) || !(display > 0)) return max;
    const z = Math.min((viewW - 2 * padX) / display, (viewH - 2 * padY) / display);
    return Math.max(min, Math.min(max, z));
}

// Header queue meter from /api/tiles/queue-stats ({total, reviewed,
// classified}; `classified` already includes reviewed tiles). Returns the
// stacked-bar segment widths (percent of total) and the pt-BR texts.
export function queueSummary(q) {
    const total = Math.max(0, Number(q?.total) || 0);
    const reviewed = Math.max(0, Math.min(total, Number(q?.reviewed) || 0));
    const classified = Math.max(reviewed, Math.min(total, Number(q?.classified) || 0));
    const pct = (n) => (total ? (100 * n) / total : 0);
    return {
        total,
        reviewedPct: pct(reviewed),
        classifiedOnlyPct: pct(classified - reviewed),
        text: `${classified}/${total} classificados`,
        title: `${reviewed}/${total} revisados · ${classified}/${total} classificados`,
    };
}

// Up to two uppercase initials for the avatar: "maria.silva" → "MS",
// "op1" → "OP", "" → "?".
export function initials(name) {
    const s = String(name ?? "").trim();
    if (!s) return "?";
    const parts = s.split(/[\s._@-]+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return s.replace(/[^\p{L}\p{N}]/gu, "").slice(0, 2).toUpperCase() || "?";
}

// What the footer submit button shows for a raster tile.
//   mode: "submit" | "review" | "incomplete" (drives the icon/tone classes)
export function rasterSubmitState({ missing, maskCompleteRequired, isReview }) {
    if (missing > 0 && maskCompleteRequired) {
        return {
            mode: "incomplete",
            incomplete: true,
            label: `Faltam ${missing.toLocaleString("pt-BR")} px`,
            title: `Complete a máscara — faltam ${missing} pixels`,
        };
    }
    return isReview
        ? { mode: "review", incomplete: false, label: "Aprovar revisão", title: "Aprovar esta revisão (Ctrl+S)" }
        : { mode: "submit", incomplete: false, label: "Submeter", title: "Submeter classificação (Ctrl+S)" };
}

// Canvas colors come from CSS custom properties on #view-editor so both
// themes stay token-driven. `getVar(name)` returns the computed value (or "").
// Alphas live in JS (numbers), never in the color strings.
export const CANVAS_PALETTE_VARS = Object.freeze({
    ink: "--canvas-ink",          // grid lines, dark cursor stroke, map shade
    halo: "--canvas-halo",        // light cursor halo
    missing: "--canvas-missing",  // F: persistent unfilled-pixel highlight
    error: "--canvas-error",      // submit-blocked flash
    accent: "--canvas-accent",    // paint-bounds / tile frame
});
export function readCanvasPalette(getVar) {
    const out = {};
    for (const [k, v] of Object.entries(CANVAS_PALETTE_VARS)) {
        out[k] = String(getVar(v) ?? "").trim() || null;
    }
    return out;
}

// "Detalhes do tile" rows (pt-BR formatted): tile center and ground footprint.
// `project` carries tile_px / meters_per_pixel. Returns null without a tile.
export function tileFacts(tile, project) {
    if (!tile) return null;
    const nf = (v, d) => Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
    const lat = (Number(tile.bbox_south) + Number(tile.bbox_north)) / 2;
    const lon = (Number(tile.bbox_west) + Number(tile.bbox_east)) / 2;
    const px = Number(project?.tile_px) || 256;
    const mpp = Number(project?.meters_per_pixel) > 0 ? Number(project.meters_per_pixel) : 2.5;
    const meters = px * mpp;
    const mDigits = Number.isInteger(meters) ? 0 : 1;
    const mppDigits = Number.isInteger(mpp) ? 0 : (Number.isInteger(mpp * 10) ? 1 : 2);
    return {
        lat: Number.isFinite(lat) ? nf(lat, 5) : "—",
        lon: Number.isFinite(lon) ? nf(lon, 5) : "—",
        footprint: `${nf(meters, mDigits)} × ${nf(meters, mDigits)} m`,
        resolution: `${nf(mpp, mppDigits)} m/px`,
    };
}
