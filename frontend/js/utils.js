// Shared DOM/data helpers used by editor and admin.
export function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

export function blobToImage(blob) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => { URL.revokeObjectURL(img.src); resolve(img); };
        img.onerror = reject;
        img.src = URL.createObjectURL(blob);
    });
}

// Format an ISO-8601 timestamp (UTC or with offset) in pt-BR / America/Sao_Paulo.
// Returns "" for null/undefined. Falls back to the raw string if parsing fails.
const _FMT = new Intl.DateTimeFormat("pt-BR", {
    timeZone: "America/Sao_Paulo",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
});
export function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return _FMT.format(d);
}

export function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, c =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
}

// Clip a tile name for inline display ("Tile: X (#42)", etc.). Imported CSVs
// can carry long descriptive names that blow up header/idle-screen layouts.
// Uses an actual ellipsis char to keep the visual width closer to `max`.
export function truncateName(name, max = 32) {
    if (name == null) return "";
    const s = String(name);
    if (s.length <= max) return s;
    return s.slice(0, Math.max(0, max - 1)) + "…";
}
