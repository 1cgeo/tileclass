// localStorage backup of an in-progress mask. Lives in its own module so the
// pure round-trip can be unit-tested without dragging editor.js's DOM deps.
// Saved during active painting and cleared on submit; only differs from the
// server blob when the user has unsynced work (F5, browser crash).
//
// Identity: one entry per (user, tile), and each entry remembers the tile
// version it was taken against. A backup from another user of the same
// browser, or from an earlier assignment cycle of the tile (version bumped
// since), is never restored — it is dropped instead.

// Legacy single-slot key (pre user/tile keying). Never read; removed on save.
export const LS_BACKUP_KEY = "tileclass_backup";
// Default tile pixel count (legacy 256×256). Per-project sizes are passed
// explicitly to loadBackup so the editor can validate against the current
// project's tile_px instead of this fallback.
export const PIXELS = 65536;
// Bound on how many per-tile entries survive (oldest dropped first) so
// abandoned backups can't fill the origin's localStorage quota.
const MAX_ENTRIES = 5;

export function backupKey(userId, tileId) {
    return `${LS_BACKUP_KEY}:${userId}:${tileId}`;
}

function uint8ToBase64(arr) {
    let s = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < arr.length; i += CHUNK) {
        s += String.fromCharCode.apply(null, arr.subarray(i, i + CHUNK));
    }
    return btoa(s);
}

function pruneOldEntries(keepKey) {
    const entries = [];
    for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || !k.startsWith(`${LS_BACKUP_KEY}:`) || k === keepKey) continue;
        let savedAt = 0;
        try { savedAt = JSON.parse(localStorage.getItem(k))?.savedAt || 0; } catch {}
        entries.push({ k, savedAt });
    }
    entries.sort((a, b) => a.savedAt - b.savedAt);
    while (entries.length > MAX_ENTRIES - 1) localStorage.removeItem(entries.shift().k);
}

// `id` = { userId, tileId, version }.
export function saveBackup({ userId, tileId, version }, mask) {
    try {
        const key = backupKey(userId, tileId);
        localStorage.removeItem(LS_BACKUP_KEY);
        pruneOldEntries(key);
        localStorage.setItem(key, JSON.stringify({
            userId, tileId, version: version ?? null, savedAt: Date.now(),
            mask: uint8ToBase64(mask),
        }));
    } catch {}
}

// Returns the restored Uint8Array if this user has a backup for this tile,
// taken at a version not older than the server's, decoding to
// `expectedPixels` bytes; null otherwise (missing, other user, stale
// version, corrupt JSON, wrong size). Stale/corrupt entries are removed.
export function loadBackup({ userId, tileId, version }, expectedPixels = PIXELS) {
    const key = backupKey(userId, tileId);
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return null;
        const b = JSON.parse(raw);
        if (b.userId !== userId || b.tileId !== tileId || !b.mask) return null;
        if (version != null && (b.version == null || b.version < version)) {
            localStorage.removeItem(key);
            return null;
        }
        const restored = Uint8Array.from(atob(b.mask), c => c.charCodeAt(0));
        if (restored.length !== expectedPixels) return null;
        return restored;
    } catch {
        try { localStorage.removeItem(key); } catch {}
        return null;
    }
}

export function clearBackup({ userId, tileId }) {
    try { localStorage.removeItem(backupKey(userId, tileId)); } catch {}
}
