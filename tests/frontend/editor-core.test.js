// Pure editor decisions extracted from editor.js (shortcut mapping, grid
// geometry, preload reuse, heartbeat recovery, modal stacking).
import { describe, it, expect, beforeEach } from "vitest";
import {
    resolveShortcut, gridGeometry, canReusePreload, heartbeatNeedsResume,
    topmostOpenModal, isAnyModalOpen, modalCancelControl, KEY_TO_OVERLAY,
    fitZoom, queueSummary, initials, rasterSubmitState, readCanvasPalette, CANVAS_PALETTE_VARS, tileFacts,
} from "../../frontend/js/editor-core.js";

const k = (key, mods = {}) => ({ key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...mods });

describe("resolveShortcut", () => {
    it("maps plain letters to their tool/brush/opacity actions", () => {
        expect(resolveShortcut(k("q"))).toEqual({ action: "tool", arg: "brush" });
        expect(resolveShortcut(k("w"))).toEqual({ action: "tool", arg: "eraser" });
        expect(resolveShortcut(k("e"))).toEqual({ action: "tool", arg: "fill" });
        expect(resolveShortcut(k("a"))).toEqual({ action: "brush", arg: -1 });
        expect(resolveShortcut(k("s"))).toEqual({ action: "brush", arg: 1 });
        expect(resolveShortcut(k("z"))).toEqual({ action: "opacity", arg: -0.1 });
        expect(resolveShortcut(k("x"))).toEqual({ action: "opacity", arg: 0.1 });
        expect(resolveShortcut(k("c"))).toEqual({ action: "next-missing" });
        expect(resolveShortcut(k("f"))).toEqual({ action: "toggle-missing" });
        expect(resolveShortcut(k(" "))).toEqual({ action: "hide-mask" });
    });

    it("uppercase (Shift/CapsLock) letters behave like lowercase", () => {
        expect(resolveShortcut(k("Q", { shiftKey: true }))).toEqual({ action: "tool", arg: "brush" });
    });

    it("digits 1..6 select a class by position; 7+ are ignored", () => {
        expect(resolveShortcut(k("1"))).toEqual({ action: "class", arg: 0 });
        expect(resolveShortcut(k("6"))).toEqual({ action: "class", arg: 5 });
        expect(resolveShortcut(k("7"))).toBeNull();
    });

    it("overlay keys map to their layer", () => {
        for (const [key, layer] of Object.entries(KEY_TO_OVERLAY)) {
            expect(resolveShortcut(k(key))).toEqual({ action: "overlay", arg: layer });
        }
    });

    it("Ctrl/Meta+Z undo, Ctrl+Shift+Z / Ctrl+Y redo", () => {
        expect(resolveShortcut(k("z", { ctrlKey: true }))).toEqual({ action: "undo" });
        expect(resolveShortcut(k("Z", { ctrlKey: true, shiftKey: true }))).toEqual({ action: "redo" });
        expect(resolveShortcut(k("y", { ctrlKey: true }))).toEqual({ action: "redo" });
        expect(resolveShortcut(k("z", { metaKey: true }))).toEqual({ action: "undo" });
    });

    it("Ctrl/Meta+S submits; Ctrl+P switches project", () => {
        expect(resolveShortcut(k("s", { ctrlKey: true }))).toEqual({ action: "submit" });
        expect(resolveShortcut(k("S", { metaKey: true }))).toEqual({ action: "submit" });
        expect(resolveShortcut(k("p", { ctrlKey: true }))).toEqual({ action: "switch-project" });
    });

    it("letter/digit shortcuts never fire with Ctrl, Meta or Alt held", () => {
        for (const key of ["a", "c", "f", "q", "w", "e", "x", "d", "r", "t", "1", "3", " "]) {
            for (const mod of ["ctrlKey", "metaKey", "altKey"]) {
                expect(resolveShortcut(k(key, { [mod]: true })), `${mod}+${key}`).toBeNull();
            }
        }
        // Alt alone never turns S/Z/Y into submit/undo/redo.
        expect(resolveShortcut(k("s", { altKey: true }))).toBeNull();
        expect(resolveShortcut(k("z", { altKey: true }))).toBeNull();
        // Ctrl+Alt (AltGr on some layouts) is not a chord we own.
        expect(resolveShortcut(k("z", { ctrlKey: true, altKey: true }))).toBeNull();
    });

    it("? opens the shortcut reference; Ctrl+? does not", () => {
        expect(resolveShortcut(k("?", { shiftKey: true }))).toEqual({ action: "help" });
        expect(resolveShortcut(k("?", { ctrlKey: true, shiftKey: true }))).toBeNull();
    });

    it("unknown keys resolve to null", () => {
        expect(resolveShortcut(k("Enter"))).toBeNull();
        expect(resolveShortcut(k("F5"))).toBeNull();
    });
});

describe("gridGeometry", () => {
    it("default 256 px × 2.5 m keeps the historical 0.5 m line (1/5 cell)", () => {
        const g = gridGeometry({ tilePx: 256, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 2 });
        expect(g.cell).toBe(3);
        expect(g.lineWidth).toBeCloseTo(0.6);
        expect(g.screenPx).toBe(6);
        expect(g.visible).toBe(true);
    });

    it("line width scales with tile_px so 1024 px tiles aren't covered by lines", () => {
        const g = gridGeometry({ tilePx: 1024, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 16 });
        expect(g.cell).toBe(0.75);
        expect(g.lineWidth).toBeLessThan(g.cell / 4);
    });

    it("uses meters_per_pixel: a 0.5 m pixel does not get a 0.5 m (full-cell) line", () => {
        const g = gridGeometry({ tilePx: 256, metersPerPixel: 0.5, display: 768, cssSize: 768, zoom: 4 });
        expect(g.lineWidth).toBeLessThan(g.cell);
        expect(g.lineWidth).toBeGreaterThan(0);
        const coarse = gridGeometry({ tilePx: 256, metersPerPixel: 10, display: 768, cssSize: 768, zoom: 4 });
        // 0.5 m line on a 10 m cell is thinner (relative) than on a 2.5 m cell.
        expect(coarse.lineWidth / coarse.cell).toBeCloseTo(0.05);
    });

    it("hides the grid while a pixel is under ~3 CSS px on screen", () => {
        expect(gridGeometry({ tilePx: 512, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 1 }).visible).toBe(false);
        expect(gridGeometry({ tilePx: 1024, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 3 }).visible).toBe(false);
        // Default 256-px tile at 100% zoom (3 CSS px per pixel) keeps its grid.
        expect(gridGeometry({ tilePx: 256, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 1 }).visible).toBe(true);
        expect(gridGeometry({ tilePx: 1024, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 6 }).visible).toBe(true);
        expect(gridGeometry({ tilePx: 256, metersPerPixel: 2.5, display: 768, cssSize: 768, zoom: 1.5 }).visible).toBe(true);
    });
});

describe("canReusePreload", () => {
    const cached = { tile: { id: 5, version: 3, paused_at: null }, maskBytes: new Uint8Array(4) };
    it("reuses only when id AND version match an unpaused tile", () => {
        expect(canReusePreload(cached, { id: 5, version: 3, paused_at: null })).toBe(true);
        expect(canReusePreload(cached, { id: 5, version: 4, paused_at: null })).toBe(false);
        expect(canReusePreload(cached, { id: 6, version: 3, paused_at: null })).toBe(false);
        expect(canReusePreload(cached, { id: 5, version: 3, paused_at: "2026-01-01" })).toBe(false);
    });
    it("rejects missing cache / mask / tile", () => {
        expect(canReusePreload(null, { id: 5, version: 3 })).toBe(false);
        expect(canReusePreload({ tile: cached.tile, maskBytes: null }, { id: 5, version: 3 })).toBe(false);
        expect(canReusePreload(cached, null)).toBe(false);
    });
});

describe("heartbeatNeedsResume", () => {
    it("only a not_active answer triggers a resume", () => {
        expect(heartbeatNeedsResume({ ok: false, reason: "not_active" })).toBe(true);
        expect(heartbeatNeedsResume({ ok: true })).toBe(false);
        expect(heartbeatNeedsResume(null)).toBe(false);
        expect(heartbeatNeedsResume({ ok: false, reason: "other" })).toBe(false);
    });
});

describe("modal stacking", () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="modal-a" class="modal hidden"><button id="a-cancel">x</button></div>
            <div id="modal-request-changes" class="modal hidden"><button id="request-changes-cancel">x</button></div>
            <div id="modal-shell" class="modal hidden"><button id="shell-cancel">x</button></div>
            <div id="modal-view" class="modal hidden"><button id="view-tile-close">x</button></div>`;
    });
    const show = (id) => document.getElementById(id).classList.remove("hidden");

    it("no visible modal → nothing open", () => {
        expect(isAnyModalOpen(document)).toBe(false);
        expect(topmostOpenModal(document)).toBeNull();
    });

    it("any visible .modal counts as open (request-changes, project switch shell)", () => {
        show("modal-request-changes");
        expect(isAnyModalOpen(document)).toBe(true);
        expect(topmostOpenModal(document).id).toBe("modal-request-changes");
    });

    it("topmost is the last visible one in DOM order", () => {
        show("modal-a"); show("modal-shell");
        expect(topmostOpenModal(document).id).toBe("modal-shell");
    });

    it("finds the cancel/close control of a modal", () => {
        expect(modalCancelControl(document.getElementById("modal-shell")).id).toBe("shell-cancel");
        expect(modalCancelControl(document.getElementById("modal-view")).id).toBe("view-tile-close");
    });
});

describe("fitZoom", () => {
    it("never upscales past 1:1 on a large stage", () => {
        expect(fitZoom(1600, 1200, 768)).toBe(1);
    });
    it("shrinks to the limiting side minus padding", () => {
        // 760 px tall stage, 16 px padding each side → (760-32)/768
        expect(fitZoom(1200, 760, 768)).toBeCloseTo(728 / 768, 6);
        expect(fitZoom(700, 1200, 768, { padding: 0 })).toBeCloseTo(700 / 768, 6);
        // Vertical room for floating chrome only: width is not the limit here.
        expect(fitZoom(1200, 900, 768, { padX: 16, padY: 56 })).toBe(1);
        expect(fitZoom(1200, 800, 768, { padX: 16, padY: 56 })).toBeCloseTo(688 / 768, 6);
    });
    it("clamps to min and falls back to max for an unmeasured (hidden) stage", () => {
        expect(fitZoom(100, 100, 768, { min: 0.25 })).toBe(0.25);
        expect(fitZoom(0, 0, 768)).toBe(1);
        expect(fitZoom(NaN, 500, 768, { max: 0.8 })).toBe(0.8);
    });
});

describe("queueSummary", () => {
    it("stacks reviewed and classified-only segments (classified includes reviewed)", () => {
        const s = queueSummary({ total: 10, reviewed: 2, classified: 5 });
        expect(s.reviewedPct).toBe(20);
        expect(s.classifiedOnlyPct).toBe(30);
        expect(s.text).toBe("5/10 classificados");
        expect(s.title).toBe("2/10 revisados · 5/10 classificados");
    });
    it("empty project and inconsistent counters stay within 0..100", () => {
        expect(queueSummary({ total: 0, reviewed: 0, classified: 0 }).reviewedPct).toBe(0);
        const s = queueSummary({ total: 4, reviewed: 9, classified: 1 });
        expect(s.reviewedPct).toBe(100);
        expect(s.classifiedOnlyPct).toBe(0);
        expect(queueSummary(null).text).toBe("0/0 classificados");
    });
});

describe("initials", () => {
    it("takes the first letters of two name parts", () => {
        expect(initials("maria.silva")).toBe("MS");
        expect(initials("João Pedro")).toBe("JP");
    });
    it("single token → first two alphanumerics; empty → ?", () => {
        expect(initials("op1")).toBe("OP");
        expect(initials("a")).toBe("A");
        expect(initials("")).toBe("?");
        expect(initials(null)).toBe("?");
    });
});

describe("rasterSubmitState", () => {
    it("incomplete mask on a strict project → 'Faltam N px' (pt-BR grouping)", () => {
        const s = rasterSubmitState({ missing: 65536, maskCompleteRequired: true, isReview: false });
        expect(s.incomplete).toBe(true);
        expect(s.mode).toBe("incomplete");
        expect(s.label).toBe("Faltam 65.536 px");
    });
    it("loose projects never block on missing pixels", () => {
        const s = rasterSubmitState({ missing: 10, maskCompleteRequired: false, isReview: false });
        expect(s).toMatchObject({ incomplete: false, mode: "submit", label: "Submeter" });
    });
    it("complete review tile → 'Aprovar revisão'", () => {
        const s = rasterSubmitState({ missing: 0, maskCompleteRequired: true, isReview: true });
        expect(s).toMatchObject({ incomplete: false, mode: "review", label: "Aprovar revisão" });
    });
});

describe("readCanvasPalette", () => {
    it("reads every palette variable and maps blanks to null", () => {
        const vars = { "--canvas-ink": " #09090b ", "--canvas-halo": "#fafafa", "--canvas-missing": "" };
        const p = readCanvasPalette((name) => vars[name]);
        expect(p.ink).toBe("#09090b");
        expect(p.halo).toBe("#fafafa");
        expect(p.missing).toBeNull();
        expect(Object.keys(p).sort()).toEqual(Object.keys(CANVAS_PALETTE_VARS).sort());
    });
});

describe("tileFacts", () => {
    const tile = { bbox_west: -47.43, bbox_east: -47.41, bbox_south: -15.56, bbox_north: -15.54 };
    it("formats the center and footprint in pt-BR", () => {
        const f = tileFacts(tile, { tile_px: 256, meters_per_pixel: 2.5 });
        expect(f.lat).toBe("-15,55000");
        expect(f.lon).toBe("-47,42000");
        expect(f.footprint).toBe("640 × 640 m");
        expect(f.resolution).toBe("2,5 m/px");
    });
    it("follows arbitrary tile geometry; defaults when the project lacks it", () => {
        expect(tileFacts(tile, { tile_px: 512, meters_per_pixel: 0.3 }).footprint).toBe("153,6 × 153,6 m");
        expect(tileFacts(tile, { tile_px: 100, meters_per_pixel: 10 }).resolution).toBe("10 m/px");
        expect(tileFacts(tile, null).footprint).toBe("640 × 640 m");
        expect(tileFacts(null, {})).toBeNull();
    });
});
