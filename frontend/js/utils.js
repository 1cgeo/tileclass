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

// Human-readable byte size: 0 → "0 B", 1500 → "1.5 KB", 2.6e9 → "2.4 GB".
export function fmtBytes(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v < 0) return "—";
    if (v < 1024) return `${v | 0} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let i = -1;
    let x = v;
    do { x /= 1024; i++; } while (x >= 1024 && i < units.length - 1);
    return `${x.toFixed(x < 10 ? 2 : 1)} ${units[i]}`;
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

// Append `?project_id=<id>` (or `&project_id=...` if the URL already has a
// query string). Returns the URL unchanged when projectId is null/undefined —
// admin endpoints treat that as "all projects".
export function withProjectParam(url, projectId) {
    if (projectId == null) return url;
    return `${url}${url.includes("?") ? "&" : "?"}project_id=${projectId}`;
}

// pt-BR display labels for tile statuses. The DB/API value stays in English
// (`pending`, `in_review`, …) — only the UI swaps. `paused` is a virtual
// status (tile is in_progress|in_review with paused_at != NULL); pass the
// paused flag so the chip reads "Pausado (revisão)" for paused reviews.
const _STATUS_LABELS = {
    pending: "Pendente",
    in_progress: "Em andamento",
    classified: "Classificado",
    in_review: "Em revisão",
    reviewed: "Revisado",
    problem: "Problema",
    blocked: "Bloqueado",
    paused: "Pausado",
};
export function statusLabel(status, paused = false) {
    if (paused) {
        return status === "in_review" ? "Pausado (revisão)" : "Pausado";
    }
    return _STATUS_LABELS[status] || status;
}

// pt-BR display names for project kinds. DB stays in English; only the UI
// swaps. Centralised so editor/admin/dashboard never drift on label spelling
// (we had "Vector" vs "Vetorial" inconsistencies before).
export const KIND_LABELS = {
    raster: "Segmentação",
    vector: "Vetorial",
    classification: "Classificação",
    detection: "Detecção",
};
export function kindLabel(kind) { return KIND_LABELS[kind] || kind || ""; }


// Project-picker dropdown shared by the operator editor and the admin panel.
// Both have the same DOM shape (`<label><select>`) wrapped in an element that
// hides when only one project exists. Caller passes an onChange that receives
// the new project id; we wire the `change` event and skip no-op selections.
//
// `allowAll: true` prepends a "(todos os projetos)" option whose value is
// the empty string, and `onChange` receives `null` when that's selected.
// Useful for cross-project views (admin Tiles/Problemas filters).
export function renderProjectPicker({
    wrapId, selectId, projects, activeId, onChange, allowAll = false,
    allLabel = "(todos os projetos)", alwaysShow = false,
}) {
    const wrap = document.getElementById(wrapId);
    const sel = document.getElementById(selectId);
    if (!wrap || !sel) return;
    sel.innerHTML = "";
    if (allowAll) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = allLabel;
        if (activeId == null) opt.selected = true;
        sel.appendChild(opt);
    }
    for (const p of projects) {
        const opt = document.createElement("option");
        opt.value = String(p.id);
        opt.textContent = p.name;
        if (p.id === activeId) opt.selected = true;
        sel.appendChild(opt);
    }
    // Default visibility rule: hide only when there's nothing meaningful to
    // pick. `alwaysShow` overrides it for placements where the picker is
    // intentional UX furniture (e.g. the map tab's bottom strip — the admin
    // expects to see "current project" even if it's the only one).
    const hide = alwaysShow
        ? projects.length === 0
        : (allowAll ? projects.length === 0 : projects.length <= 1);
    wrap.classList.toggle("hidden", hide);
    sel.onchange = () => {
        const raw = sel.value;
        const newId = raw === "" ? null : parseInt(raw, 10);
        if (newId === activeId) return;
        onChange(newId);
    };
}
