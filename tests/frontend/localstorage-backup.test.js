// Round-trip Uint8Array → base64 → JSON → back, exercising the real
// frontend/js/backup.js module so the test can't drift from production.
import { describe, it, expect, beforeEach } from "vitest";
import {
    LS_BACKUP_KEY, PIXELS,
    saveBackup, loadBackup, clearBackup,
} from "../../frontend/js/backup.js";

describe("localStorage backup round-trip", () => {
    beforeEach(() => localStorage.clear());

    it("round-trips a full mask byte-exactly", () => {
        const mask = new Uint8Array(PIXELS);
        for (let i = 0; i < PIXELS; i++) mask[i] = (i % 6) + 1;
        saveBackup(42, mask);
        const restored = loadBackup(42);
        expect(restored).not.toBeNull();
        expect(restored.length).toBe(PIXELS);
        for (let i = 0; i < PIXELS; i++) expect(restored[i]).toBe(mask[i]);
    });

    it("refuses to restore a backup from a different tileId", () => {
        const mask = new Uint8Array(PIXELS).fill(1);
        saveBackup(42, mask);
        expect(loadBackup(99)).toBeNull();
    });

    it("returns null when no backup exists", () => {
        expect(loadBackup(42)).toBeNull();
    });

    it("returns null on corrupt JSON", () => {
        localStorage.setItem(LS_BACKUP_KEY, "not-json");
        expect(loadBackup(42)).toBeNull();
    });

    it("returns null when restored size != PIXELS", () => {
        localStorage.setItem(
            LS_BACKUP_KEY,
            JSON.stringify({ tileId: 1, mask: btoa("short") })
        );
        expect(loadBackup(1)).toBeNull();
    });

    it("preserves 255 (empty) pixels exactly", () => {
        const mask = new Uint8Array(PIXELS);
        mask[0] = 1; mask[1] = 255; mask[2] = 6;
        for (let i = 3; i < PIXELS; i++) mask[i] = 255;
        saveBackup(7, mask);
        const restored = loadBackup(7);
        expect(restored[0]).toBe(1);
        expect(restored[1]).toBe(255);
        expect(restored[2]).toBe(6);
        expect(restored[PIXELS - 1]).toBe(255);
    });

    it("clearBackup removes the entry", () => {
        saveBackup(1, new Uint8Array(PIXELS));
        clearBackup();
        expect(localStorage.getItem(LS_BACKUP_KEY)).toBeNull();
    });
});
