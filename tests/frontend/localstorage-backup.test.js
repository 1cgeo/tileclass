// Round-trip Uint8Array → base64 → JSON → back, exercising the real
// frontend/js/backup.js module so the test can't drift from production.
// Backups are keyed by (user, tile) and carry the tile version they were
// taken against, so a stale backup never resurfaces after reassignment.
import { describe, it, expect, beforeEach } from "vitest";
import {
    LS_BACKUP_KEY, PIXELS, backupKey,
    saveBackup, loadBackup, clearBackup,
} from "../../frontend/js/backup.js";

const ID = (tileId, { userId = 1, version = 1 } = {}) => ({ userId, tileId, version });

describe("localStorage backup round-trip", () => {
    beforeEach(() => localStorage.clear());

    it("round-trips a full mask byte-exactly", () => {
        const mask = new Uint8Array(PIXELS);
        for (let i = 0; i < PIXELS; i++) mask[i] = (i % 6) + 1;
        saveBackup(ID(42), mask);
        const restored = loadBackup(ID(42));
        expect(restored).not.toBeNull();
        expect(restored.length).toBe(PIXELS);
        for (let i = 0; i < PIXELS; i++) expect(restored[i]).toBe(mask[i]);
    });

    it("refuses to restore a backup from a different tileId", () => {
        saveBackup(ID(42), new Uint8Array(PIXELS).fill(1));
        expect(loadBackup(ID(99))).toBeNull();
    });

    it("returns null when no backup exists", () => {
        expect(loadBackup(ID(42))).toBeNull();
    });

    it("returns null on corrupt JSON", () => {
        localStorage.setItem(backupKey(1, 42), "not-json");
        expect(loadBackup(ID(42))).toBeNull();
    });

    it("returns null when restored size != PIXELS", () => {
        localStorage.setItem(
            backupKey(1, 1),
            JSON.stringify({ userId: 1, tileId: 1, version: 1, mask: btoa("short") })
        );
        expect(loadBackup(ID(1))).toBeNull();
    });

    it("preserves 255 (empty) pixels exactly", () => {
        const mask = new Uint8Array(PIXELS);
        mask[0] = 1; mask[1] = 255; mask[2] = 6;
        for (let i = 3; i < PIXELS; i++) mask[i] = 255;
        saveBackup(ID(7), mask);
        const restored = loadBackup(ID(7));
        expect(restored[0]).toBe(1);
        expect(restored[1]).toBe(255);
        expect(restored[2]).toBe(6);
        expect(restored[PIXELS - 1]).toBe(255);
    });

    it("clearBackup removes the entry", () => {
        saveBackup(ID(1), new Uint8Array(PIXELS));
        clearBackup(ID(1));
        expect(localStorage.getItem(backupKey(1, 1))).toBeNull();
        expect(loadBackup(ID(1))).toBeNull();
    });

    it("is keyed per user: another user's backup of the same tile is never restored", () => {
        saveBackup(ID(5, { userId: 1 }), new Uint8Array(PIXELS).fill(3));
        expect(loadBackup(ID(5, { userId: 2 }))).toBeNull();
        // The original owner still gets it back.
        expect(loadBackup(ID(5, { userId: 1 }))).not.toBeNull();
        expect(backupKey(1, 5)).not.toBe(backupKey(2, 5));
    });

    it("drops a backup taken against an older tile version (earlier cycle)", () => {
        saveBackup(ID(5, { version: 3 }), new Uint8Array(PIXELS).fill(3));
        expect(loadBackup(ID(5, { version: 7 }))).toBeNull();
        // Stale entry is removed, not just ignored.
        expect(localStorage.getItem(backupKey(1, 5))).toBeNull();
    });

    it("restores when the saved version equals the server version", () => {
        saveBackup(ID(5, { version: 7 }), new Uint8Array(PIXELS).fill(3));
        expect(loadBackup(ID(5, { version: 7 }))).not.toBeNull();
    });

    it("ignores the legacy un-keyed backup slot", () => {
        const mask = new Uint8Array(PIXELS).fill(2);
        let s = ""; for (let i = 0; i < 16; i++) s += String.fromCharCode(2);
        localStorage.setItem(LS_BACKUP_KEY, JSON.stringify({ tileId: 9, mask: btoa(s) }));
        expect(loadBackup(ID(9))).toBeNull();
        saveBackup(ID(9), mask);
        expect(localStorage.getItem(LS_BACKUP_KEY)).toBeNull();
    });

    it("clearing one tile's backup leaves other users' backups intact", () => {
        saveBackup(ID(5, { userId: 1 }), new Uint8Array(PIXELS).fill(3));
        saveBackup(ID(6, { userId: 2 }), new Uint8Array(PIXELS).fill(4));
        clearBackup(ID(5, { userId: 1 }));
        expect(loadBackup(ID(6, { userId: 2 }))).not.toBeNull();
    });
});
