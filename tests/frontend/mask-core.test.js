import { describe, it, expect } from "vitest";
import {
    TILE, PIXELS, EMPTY,
    paintAt, paintLine, floodFill,
    toUndoEntry, applyPatch, countFilled,
    screenToLogical, validateSubmission, pushBounded,
} from "../../frontend/js/mask-core.js";

const newMask = () => new Uint8Array(PIXELS).fill(EMPTY);

describe("paintAt", () => {
    it("paints a single pixel with r=0", () => {
        const m = newMask();
        const { touched, deltaFilled, region } = paintAt(m, 10, 20, 3, 0);
        expect(m[20 * TILE + 10]).toBe(3);
        expect(touched.size).toBe(1);
        expect(deltaFilled).toBe(1);
        expect(region).toEqual([10, 20, 11, 21]);
    });

    it("paints a 3x3 square with r=1", () => {
        const m = newMask();
        const { touched, deltaFilled } = paintAt(m, 50, 50, 2, 1);
        expect(touched.size).toBe(9);
        expect(deltaFilled).toBe(9);
        for (let y = 49; y <= 51; y++)
            for (let x = 49; x <= 51; x++) expect(m[y * TILE + x]).toBe(2);
    });

    it("clamps brush at canvas edges (top-left corner)", () => {
        const m = newMask();
        const { touched } = paintAt(m, 0, 0, 1, 1);
        expect(touched.size).toBe(4); // 2x2 visible
    });

    it("clamps brush at bottom-right corner (255,255)", () => {
        const m = newMask();
        const { touched } = paintAt(m, 255, 255, 1, 1);
        expect(touched.size).toBe(4);
        expect(m[255 * TILE + 255]).toBe(1);
    });

    it("no-op when painting same value (touched unchanged)", () => {
        const m = newMask();
        paintAt(m, 10, 10, 5, 0);
        const { touched, deltaFilled } = paintAt(m, 10, 10, 5, 0);
        expect(touched.size).toBe(0);
        expect(deltaFilled).toBe(0);
    });

    it("erasing with EMPTY decrements deltaFilled", () => {
        const m = newMask();
        paintAt(m, 10, 10, 3, 0);
        const { deltaFilled } = paintAt(m, 10, 10, EMPTY, 0);
        expect(deltaFilled).toBe(-1);
        expect(m[10 * TILE + 10]).toBe(EMPTY);
    });

    it("touched preserves ORIGINAL prev value, not intermediate", () => {
        const m = newMask();
        const shared = new Map();
        paintAt(m, 10, 10, 3, 0, shared);  // touched[i] = 255
        paintAt(m, 10, 10, 4, 0, shared);  // value differs but prev in map kept
        const idx = 10 * TILE + 10;
        expect(shared.get(idx)).toBe(EMPTY);
        expect(m[idx]).toBe(4);
    });
});

describe("paintLine (Bresenham)", () => {
    it("produces unbroken line from (10,10) to (200,200)", () => {
        const m = newMask();
        paintLine(m, 10, 10, 200, 200, 1, 0);
        // Every integer step along the line should be painted
        for (let i = 0; i <= 190; i++) {
            const x = 10 + i, y = 10 + i;
            expect(m[y * TILE + x]).toBe(1);
        }
    });

    it("fills gaps in a fast horizontal drag", () => {
        const m = newMask();
        paintLine(m, 0, 100, 255, 100, 2, 0);
        for (let x = 0; x < 256; x++) expect(m[100 * TILE + x]).toBe(2);
    });

    it("diagonal line has no row skipped (orthogonal or diagonal stepping)", () => {
        const m = newMask();
        paintLine(m, 0, 0, 10, 20, 3, 0);
        // Every y in [0..20] must have at least one painted pixel
        for (let y = 0; y <= 20; y++) {
            let any = false;
            for (let x = 0; x <= 10; x++) {
                if (m[y * TILE + x] === 3) { any = true; break; }
            }
            expect(any).toBe(true);
        }
    });

    it("single-point line == paintAt", () => {
        const m = newMask();
        paintLine(m, 5, 5, 5, 5, 4, 0);
        expect(m[5 * TILE + 5]).toBe(4);
    });
});

describe("floodFill", () => {
    it("fills the entire mask from a single seed when all EMPTY", () => {
        const m = newMask();
        const { touched, deltaFilled } = floodFill(m, 128, 128, 1);
        expect(touched.size).toBe(PIXELS);
        expect(deltaFilled).toBe(PIXELS);
        for (let i = 0; i < PIXELS; i++) expect(m[i]).toBe(1);
    });

    it("no-op when target == replacement", () => {
        const m = newMask();
        paintAt(m, 10, 10, 2, 0);
        const { touched } = floodFill(m, 10, 10, 2);
        expect(touched.size).toBe(0);
    });

    it("respects boundaries — does not cross a wall of different color", () => {
        const m = newMask();
        // Horizontal wall at y=100
        for (let x = 0; x < TILE; x++) m[100 * TILE + x] = 5;
        floodFill(m, 50, 50, 3);
        // Above wall should be 3
        expect(m[50 * TILE + 50]).toBe(3);
        // Below wall should still be EMPTY
        expect(m[150 * TILE + 50]).toBe(EMPTY);
    });
});

describe("undo/redo via toUndoEntry + applyPatch", () => {
    it("undo restores exactly the touched pixels, leaves others untouched", () => {
        const m = newMask();
        paintAt(m, 5, 5, 1, 0);  // seed
        const touched = new Map();
        paintAt(m, 10, 10, 2, 1, touched);  // 3x3 block
        const entry = toUndoEntry(touched);

        const { inverse } = applyPatch(m, entry);

        // Seed at (5,5) is untouched
        expect(m[5 * TILE + 5]).toBe(1);
        // Block at (10,10) is back to EMPTY
        for (let y = 9; y <= 11; y++)
            for (let x = 9; x <= 11; x++)
                expect(m[y * TILE + x]).toBe(EMPTY);
        // Inverse can redo
        expect(inverse.positions.length).toBe(9);
    });

    it("redo after undo is byte-identical to pre-undo state", () => {
        const m = newMask();
        const touched = new Map();
        paintAt(m, 50, 50, 4, 2, touched);
        const entry = toUndoEntry(touched);

        const snapshot = new Uint8Array(m);  // after paint
        const { inverse } = applyPatch(m, entry);  // undo
        applyPatch(m, inverse);  // redo
        expect(Array.from(m)).toEqual(Array.from(snapshot));
    });

    it("stores typed arrays, not full mask copies", () => {
        const touched = new Map();
        touched.set(0, 255);
        touched.set(100, 2);
        const entry = toUndoEntry(touched);
        expect(entry.positions).toBeInstanceOf(Uint32Array);
        expect(entry.prevValues).toBeInstanceOf(Uint8Array);
        expect(entry.positions.length).toBe(2);
        // Memory: 2 entries * 5 bytes = 10 bytes — not 65536
        expect(entry.positions.byteLength + entry.prevValues.byteLength).toBe(10);
    });
});

describe("pushBounded (undo stack cap)", () => {
    it("drops oldest when over MAX_UNDO", () => {
        const stack = [];
        for (let i = 0; i < 51; i++) pushBounded(stack, { id: i }, 50);
        expect(stack.length).toBe(50);
        expect(stack[0].id).toBe(1);  // id=0 dropped
        expect(stack[49].id).toBe(50);
    });
});

describe("countFilled", () => {
    it("counts non-255 pixels", () => {
        const m = newMask();
        expect(countFilled(m)).toBe(0);
        m[0] = 1; m[1] = 2; m[2] = EMPTY;
        expect(countFilled(m)).toBe(2);
    });
});

describe("screenToLogical — THE most fragile mapping", () => {
    const rect = { left: 0, top: 0, width: 768, height: 768 };  // DISPLAY=768

    it("top-left (0,0) → (0,0)", () => {
        expect(screenToLogical(rect, 0, 0)).toEqual([0, 0]);
    });

    it("bottom-right (767.99, 767.99) → (255, 255)", () => {
        // Exactly 768 would overflow to 256; test just inside
        expect(screenToLogical(rect, 767.9, 767.9)).toEqual([255, 255]);
    });

    it("center (384, 384) → (128, 128)", () => {
        expect(screenToLogical(rect, 384, 384)).toEqual([128, 128]);
    });

    it("out-of-bounds clamps to [0, TILE-1]", () => {
        expect(screenToLogical(rect, -50, -50)).toEqual([0, 0]);
        expect(screenToLogical(rect, 10000, 10000)).toEqual([255, 255]);
    });

    it("respects non-zero rect.left/top (scrolled canvas)", () => {
        const offsetRect = { left: 100, top: 200, width: 768, height: 768 };
        expect(screenToLogical(offsetRect, 100, 200)).toEqual([0, 0]);
        expect(screenToLogical(offsetRect, 484, 584)).toEqual([128, 128]);
    });

    it("stretched canvas (non-square or scaled) still maps to 256 grid", () => {
        const stretched = { left: 0, top: 0, width: 1536, height: 1536 };
        expect(screenToLogical(stretched, 768, 768)).toEqual([128, 128]);
        expect(screenToLogical(stretched, 1535.9, 1535.9)).toEqual([255, 255]);
    });
});

describe("validateSubmission — mirrors backend", () => {
    it("accepts fully-painted mask of valid classes", () => {
        const m = new Uint8Array(PIXELS).fill(3);
        const r = validateSubmission(m);
        expect(r.ok).toBe(true);
        expect(r.missing).toBe(0);
    });

    it("reports missing count when 255s present", () => {
        const m = new Uint8Array(PIXELS).fill(1);
        m[0] = EMPTY; m[1] = EMPTY; m[2] = EMPTY;
        const r = validateSubmission(m);
        expect(r.ok).toBe(false);
        expect(r.missing).toBe(3);
    });

    it("throws on class 0 or 7", () => {
        const m = new Uint8Array(PIXELS).fill(1);
        m[42] = 0;
        expect(() => validateSubmission(m)).toThrow(/invalid class/);
        const m2 = new Uint8Array(PIXELS).fill(1);
        m2[42] = 7;
        expect(() => validateSubmission(m2)).toThrow(/invalid class/);
    });

    it("throws on wrong size", () => {
        expect(() => validateSubmission(new Uint8Array(100))).toThrow(/expected 65536/);
    });

    it("honours a restricted per-project palette (not just the 1..6 default)", () => {
        // Project allows only {1,2,3}; a pixel of class 4 must be rejected even
        // though it's valid under the default palette.
        const m = new Uint8Array(PIXELS).fill(1);
        m[100] = 4;
        expect(() => validateSubmission(m, [1, 2, 3])).toThrow(/invalid class/);
        // A mask using only the allowed subset passes.
        const ok = new Uint8Array(PIXELS).fill(2);
        expect(validateSubmission(ok, [1, 2, 3]).ok).toBe(true);
    });

    it("accepts all 6 valid classes", () => {
        for (let c = 1; c <= 6; c++) {
            const m = new Uint8Array(PIXELS).fill(c);
            expect(validateSubmission(m).ok).toBe(true);
        }
    });
});

describe("non-default tile_px", () => {
    it("paintAt respects the supplied tilePx for indexing and bounds", () => {
        const TP = 128;
        const m = new Uint8Array(TP * TP).fill(EMPTY);
        // Paint near the right edge at (TP-1, 5). With tilePx=128 the brush
        // clips at column TP-1; with the legacy 256 default the index would
        // be wrong (5*256 + 127 ≠ 5*128 + 127).
        paintAt(m, TP - 1, 5, 3, 0, new Map(), TP);
        expect(m[5 * TP + (TP - 1)]).toBe(3);
        // Adjacent pixel must remain untouched.
        expect(m[5 * TP + (TP - 2)]).toBe(EMPTY);
    });

    it("floodFill walks the correct row stride for arbitrary tilePx", () => {
        const TP = 64;
        const m = new Uint8Array(TP * TP).fill(EMPTY);
        const { deltaFilled } = floodFill(m, 0, 0, 1, TP);
        expect(deltaFilled).toBe(TP * TP);  // entire mask filled
        expect(m.every(v => v === 1)).toBe(true);
    });

    it("validateSubmission accepts the matching tile_px size", () => {
        const TP = 512;
        const big = new Uint8Array(TP * TP).fill(1);
        expect(validateSubmission(big, [1, 2, 3, 4, 5, 6], TP).ok).toBe(true);
    });

    it("validateSubmission rejects size ≠ tile_px²", () => {
        const TP = 128;
        const wrong = new Uint8Array(64 * 64).fill(1);  // 64² instead of 128²
        expect(() => validateSubmission(wrong, [1, 2, 3, 4, 5, 6], TP))
            .toThrow(/expected 16384/);
    });

    it("screenToLogical clamps to tilePx-1 when given a non-default tile", () => {
        const TP = 128;
        const rect = { left: 0, top: 0, width: 100, height: 100 };
        // Click at the bottom-right corner of the rect → (TP-1, TP-1).
        const [x, y] = screenToLogical(rect, 99.999, 99.999, TP);
        expect(x).toBe(TP - 1);
        expect(y).toBe(TP - 1);
    });
});
