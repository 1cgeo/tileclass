// editor-classification.js lifecycle against a stubbed MapLibre + fetch.
// Locks the stale-enter guard: a tile switch while the previous tile's
// GET /classification is still in flight must not pre-select the old tile's
// class on the new tile (it would be submitted unnoticed), and an exit during
// that fetch must not create a map.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

vi.mock("../../frontend/js/toast.js", () => ({ showToast: vi.fn() }));

const maps = [];
class FakeMap {
    constructor(opts) { this.opts = opts; this.removed = false; maps.push(this); }
    on() {}
    remove() { this.removed = true; }
}

const project = {
    classes: [
        { id: 10, name: "Urbano", color: "#e41a1c" },
        { id: 20, name: "Água", color: "#377eb8" },
    ],
};
const tile = (id) => ({
    id, status: "in_progress",
    bbox_west: 0, bbox_south: 0, bbox_east: 0.1, bbox_north: 0.1,
});

// fetch stub whose responses the test releases explicitly.
function deferredFetch() {
    const pending = new Map();
    const fn = vi.fn((url) => new Promise((resolve) => {
        const id = Number(url.match(/tiles\/(\d+)\//)[1]);
        pending.set(id, resolve);
    }));
    const respond = (id, classId) => pending.get(id)(classId == null
        ? { status: 204, json: async () => null }
        : { status: 200, json: async () => ({ class_id: classId }) });
    return { fn, respond };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

let mod;
beforeEach(async () => {
    maps.length = 0;
    document.body.innerHTML = `<div class="canvas-wrap"><div id="canvas-stack"></div></div>`;
    globalThis.maplibregl = { Map: FakeMap };
    vi.resetModules();
    mod = await import("../../frontend/js/editor-classification.js");
});
afterEach(() => {
    delete globalThis.maplibregl;
    vi.unstubAllGlobals();
});

describe("enterClassificationTile", () => {
    it("restores the tile's saved class and creates one map", async () => {
        const f = deferredFetch();
        vi.stubGlobal("fetch", f.fn);
        const p = mod.enterClassificationTile(tile(1), project);
        f.respond(1, 20);
        await p;
        expect(JSON.parse(mod.getCurrentBody())).toEqual({ class_id: 20 });
        expect(document.querySelector(".classification-class.active").dataset.classId).toBe("20");
        expect(maps).toHaveLength(1);
    });

    it("ignores a stale class from the previous tile after a tile switch", async () => {
        const f = deferredFetch();
        vi.stubGlobal("fetch", f.fn);
        const pA = mod.enterClassificationTile(tile(1), project);
        mod.exitClassificationTile();
        const pB = mod.enterClassificationTile(tile(2), project);
        f.respond(2, null);      // tile 2 never classified
        await pB;
        f.respond(1, 10);        // tile 1's late response arrives afterwards
        await pA;
        await flush();
        expect(JSON.parse(mod.getCurrentBody())).toEqual({ class_id: null });
        expect(document.querySelector(".classification-class.active")).toBeNull();
        expect(mod.validateForSubmit()).toHaveLength(1);
        // Only tile 2 got a map; the superseded enter bailed before creating one.
        expect(maps).toHaveLength(1);
        expect(maps[0].opts.bounds).toEqual([[0, 0], [0.1, 0.1]]);
    });

    it("does not create a map when exited while the class fetch is in flight", async () => {
        const f = deferredFetch();
        vi.stubGlobal("fetch", f.fn);
        const p = mod.enterClassificationTile(tile(1), project);
        mod.exitClassificationTile();
        f.respond(1, 10);
        await p;
        expect(maps).toHaveLength(0);
        expect(JSON.parse(mod.getCurrentBody())).toEqual({ class_id: null });
    });
});

describe("handleKeyDown", () => {
    const key = (k, extra = {}) => ({ key: k, preventDefault: vi.fn(), ...extra });

    it("selects the Nth class by position and consumes the key", async () => {
        vi.stubGlobal("fetch", vi.fn(async () => ({ status: 204, json: async () => null })));
        await mod.enterClassificationTile(tile(1), project);
        const ev = key("2");
        expect(mod.handleKeyDown(ev)).toBe(true);
        expect(ev.preventDefault).toHaveBeenCalled();
        expect(JSON.parse(mod.getCurrentBody())).toEqual({ class_id: 20 });
    });

    it("ignores out-of-range digits, modifiers, and keys after exit", async () => {
        vi.stubGlobal("fetch", vi.fn(async () => ({ status: 204, json: async () => null })));
        await mod.enterClassificationTile(tile(1), project);
        expect(mod.handleKeyDown(key("3"))).toBe(false);
        expect(mod.handleKeyDown(key("1", { ctrlKey: true }))).toBe(false);
        expect(JSON.parse(mod.getCurrentBody())).toEqual({ class_id: null });
        mod.exitClassificationTile();
        expect(mod.handleKeyDown(key("1"))).toBe(false);
    });
});
