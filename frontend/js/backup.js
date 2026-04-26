// localStorage backup of an in-progress mask. Lives in its own module so the
// pure round-trip can be unit-tested without dragging editor.js's DOM deps.
// Saved during active painting and cleared on submit; only differs from the
// server blob when the user has unsynced work (F5, browser crash).

export const LS_BACKUP_KEY = "tileclass_backup";
export const PIXELS = 65536;

function uint8ToBase64(arr) {
    let s = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < arr.length; i += CHUNK) {
        s += String.fromCharCode.apply(null, arr.subarray(i, i + CHUNK));
    }
    return btoa(s);
}

export function saveBackup(tileId, mask) {
    try {
        const b64 = uint8ToBase64(mask);
        localStorage.setItem(LS_BACKUP_KEY, JSON.stringify({ tileId, mask: b64 }));
    } catch {}
}

// Returns the restored Uint8Array if a backup exists for `currentTileId` and
// decodes to PIXELS bytes; null otherwise (missing, wrong tile, corrupt JSON,
// wrong size).
export function loadBackup(currentTileId) {
    try {
        const raw = localStorage.getItem(LS_BACKUP_KEY);
        if (!raw) return null;
        const b = JSON.parse(raw);
        if (b.tileId !== currentTileId || !b.mask) return null;
        const restored = Uint8Array.from(atob(b.mask), c => c.charCodeAt(0));
        if (restored.length !== PIXELS) return null;
        return restored;
    } catch {
        return null;
    }
}

export function clearBackup() {
    localStorage.removeItem(LS_BACKUP_KEY);
}
