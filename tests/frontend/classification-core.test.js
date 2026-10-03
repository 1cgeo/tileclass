import { describe, it, expect } from "vitest";
import { classIdForKey, tileFrameGeoJSON } from "../../frontend/js/classification-core.js";

const classes = [
    { id: 7, name: "urbano", color: "#e41a1c" },
    { id: 3, name: "agua", color: "#377eb8" },
    { id: 12, name: "solo", color: "#ff7f00" },
];

describe("classIdForKey", () => {
    it("maps digit N to the Nth class id in display order (not the id itself)", () => {
        expect(classIdForKey("1", classes)).toBe(7);
        expect(classIdForKey("2", classes)).toBe(3);
        expect(classIdForKey("3", classes)).toBe(12);
    });

    it("returns null past the last class", () => {
        expect(classIdForKey("4", classes)).toBeNull();
        expect(classIdForKey("9", classes)).toBeNull();
    });

    it("ignores non-shortcut keys", () => {
        for (const k of ["0", "10", "a", "Enter", " ", "", undefined, null]) {
            expect(classIdForKey(k, classes)).toBeNull();
        }
    });

    it("tolerates a missing class list", () => {
        expect(classIdForKey("1", undefined)).toBeNull();
        expect(classIdForKey("1", [])).toBeNull();
    });
});

describe("tileFrameGeoJSON", () => {
    const tile = { bbox_west: -50, bbox_south: -25, bbox_east: -49.9, bbox_north: -24.9 };

    it("outline is the closed bbox ring", () => {
        const { outline } = tileFrameGeoJSON(tile);
        expect(outline.geometry.type).toBe("LineString");
        const c = outline.geometry.coordinates;
        expect(c).toHaveLength(5);
        expect(c[0]).toEqual(c[4]);
        const xs = c.map(p => p[0]), ys = c.map(p => p[1]);
        expect(Math.min(...xs)).toBe(-50);
        expect(Math.max(...xs)).toBe(-49.9);
        expect(Math.min(...ys)).toBe(-25);
        expect(Math.max(...ys)).toBe(-24.9);
    });

    it("shade is a world polygon with the tile bbox as its hole", () => {
        const { shade, outline } = tileFrameGeoJSON(tile);
        expect(shade.geometry.type).toBe("Polygon");
        const [outer, hole] = shade.geometry.coordinates;
        expect(outer[0]).toEqual(outer[outer.length - 1]);
        expect(Math.min(...outer.map(p => p[0]))).toBe(-180);
        expect(Math.max(...outer.map(p => p[0]))).toBe(180);
        expect(hole).toEqual(outline.geometry.coordinates);
    });
});
