// Pure helpers for the admin panel (no DOM). Formatting, chart data shaping,
// tile row actions and history labels — covered by tests/frontend/admin-core.test.js.

const NUM_FMT = new Intl.NumberFormat("pt-BR");

// Status order used by every status visualization (stacked bar, legends, map
// legend): finished work first, then the pipeline, then exceptions.
export const STATUS_ORDER = [
    "reviewed", "in_review", "classified", "in_progress", "paused",
    "pending", "blocked", "problem",
];

// Tile counts per display status. `paused` is virtual: paused tiles are still
// counted under in_progress/in_review in `totals`, so they are moved out of
// those buckets here. Returns only non-empty buckets, in STATUS_ORDER, with
// `pct` relative to the grand total (0..100, not rounded).
export function statusBreakdown(totals = {}, pausedByStatus = {}) {
    const t = { ...totals };
    const pausedIp = pausedByStatus.in_progress || 0;
    const pausedIr = pausedByStatus.in_review || 0;
    t.in_progress = Math.max(0, (t.in_progress || 0) - pausedIp);
    t.in_review = Math.max(0, (t.in_review || 0) - pausedIr);
    t.paused = pausedIp + pausedIr;
    const total = STATUS_ORDER.reduce((s, k) => s + (t[k] || 0), 0);
    return STATUS_ORDER
        .filter(k => (t[k] || 0) > 0)
        .map(k => ({ key: k, count: t[k], pct: total ? (100 * t[k]) / total : 0 }));
}

// Shift an ISO date (YYYY-MM-DD) by `days`, staying in UTC so the result
// matches the backend's `substr(reviewed_at, 1, 10)` buckets.
export function addDaysIso(isoDate, days) {
    const d = new Date(`${isoDate}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
}

// The API returns only days with activity (newest first). Charts need a
// contiguous window: `days` entries ending at `todayIso`, oldest first,
// zero-filled.
export function fillDailySeries(rows, todayIso, days = 30) {
    const byDate = new Map((rows || []).map(r => [r.date, Number(r.count) || 0]));
    const out = [];
    for (let i = days - 1; i >= 0; i--) {
        const date = addDaysIso(todayIso, -i);
        out.push({ date, count: byDate.get(date) || 0 });
    }
    return out;
}

// Smallest "nice" number >= n (1, 2, 2.5, 5 × 10^k). Axis maximum for charts.
export function niceCeil(n) {
    if (!(n > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(n)));
    for (const m of [1, 2, 2.5, 5, 10]) {
        if (m * p >= n - 1e-9) return m * p;
    }
    return 10 * p;
}

// Smallest step from {1, 2, 5} × 10^k that is >= n (integer-friendly).
export function niceStep(n) {
    if (!(n > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(n)));
    for (const m of [1, 2, 5, 10]) {
        if (m * p >= n - 1e-9) return Math.max(1, m * p);
    }
    return 10 * p;
}

// `count` evenly spaced ticks above 0 with a nice integer step, covering max.
export function axisTicks(max, count = 4) {
    const step = niceStep(Math.max(max, 1) / count);
    return Array.from({ length: count + 1 }, (_, i) => i * step);
}

// "ana.souza" -> "AS", "admin" -> "AD", "Maria Clara" -> "MC".
export function initials(name) {
    const s = String(name || "").trim();
    if (!s) return "?";
    const parts = s.split(/[\s._-]+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return s.slice(0, 2).toUpperCase();
}

// Stable 1..6 bucket per name — picks a --chart-N tint for avatars so the
// same person keeps the same color everywhere.
export function avatarTone(name) {
    let h = 0;
    for (const ch of String(name || "")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return (h % 6) + 1;
}

export function fmtInt(n) {
    const v = Number(n);
    return Number.isFinite(v) ? NUM_FMT.format(Math.round(v)) : "—";
}

// Compact pt-BR magnitude: 950 -> "950", 12_300 -> "12,3 mil", 4.2e6 -> "4,2 mi".
export function fmtCompact(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    const abs = Math.abs(v);
    const fmt1 = (x) => x.toLocaleString("pt-BR", { maximumFractionDigits: x < 10 ? 1 : 0 });
    if (abs >= 1e9) return `${fmt1(v / 1e9)} bi`;
    if (abs >= 1e6) return `${fmt1(v / 1e6)} mi`;
    if (abs >= 1e4) return `${fmt1(v / 1e3)} mil`;
    return NUM_FMT.format(Math.round(v));
}

export function fmtPct(p, digits = 1) {
    const v = Number(p);
    if (!Number.isFinite(v)) return "—";
    return `${v.toLocaleString("pt-BR", { minimumFractionDigits: 0, maximumFractionDigits: digits })}%`;
}

export function fmtDecimal(n, digits = 1) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    return v.toLocaleString("pt-BR", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

// ETA in days -> short pt-BR text. null/undefined/<=0 means "no estimate".
export function fmtEtaDays(days) {
    const v = Number(days);
    if (days == null || !Number.isFinite(v) || v <= 0) return "—";
    if (v < 1) return "< 1 dia";
    if (v < 1.5) return "1 dia";
    if (v < 60) return `${Math.round(v)} dias`;
    return `${fmtDecimal(v / 30, 1)} meses`;
}

// "há 5 min" style relative time. `nowMs` injectable for tests.
export function relativeTime(iso, nowMs = Date.now()) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, Math.round((nowMs - t) / 1000));
    if (s < 60) return "agora";
    const m = Math.round(s / 60);
    if (m < 60) return `há ${m} min`;
    const h = Math.round(m / 60);
    if (h < 24) return `há ${h} h`;
    const d = Math.round(h / 24);
    if (d < 30) return d === 1 ? "há 1 dia" : `há ${d} dias`;
    const mo = Math.round(d / 30);
    return mo === 1 ? "há 1 mês" : `há ${mo} meses`;
}

// Mirrors backend `_BLOCKABLE_STATES` (admin tiles_mutations).
export const BLOCKABLE_STATES = new Set(["pending", "classified", "reviewed"]);

// Secondary actions available for one tile row (the primary "Ver" is always
// shown separately). Keys map to handlers in admin.js; order = menu order.
export function tileRowActions(t) {
    if (!t) return [];
    const out = [];
    if (t.status === "pending" || t.status === "classified") out.push("assign");
    if (t.status === "reviewed") out.push("rereview");
    if (t.status === "in_progress" || t.status === "in_review") {
        if (!t.paused_at) out.push("pause");
        out.push("unassign");
    }
    if (BLOCKABLE_STATES.has(t.status)) out.push("block");
    if (t.status === "blocked") out.push("unblock");
    return out;
}

// pt-BR labels for action_log entries shown in the tile history timeline.
const _ACTION_LABELS = {
    assign_classify: "Recebeu para classificar",
    assign_review: "Recebeu para revisar",
    classify: "Classificou",
    review: "Aprovou a revisão",
    request_changes: "Pediu ajustes",
    pause: "Pausou",
    resume: "Retomou",
    report_problem: "Reportou problema",
    reset: "Resetou o tile",
    re_review: "Enviou para nova revisão",
    admin_assign: "Atribuiu (admin)",
    admin_pause: "Pausou (admin)",
    unassign: "Liberou o operador",
    block: "Bloqueou",
    unblock: "Desbloqueou",
};
export function actionLabel(action) {
    return _ACTION_LABELS[action] || String(action || "");
}

// Tone of a history entry (drives the timeline dot color).
export function actionTone(action) {
    if (action === "classify" || action === "review") return "ok";
    if (action === "report_problem" || action === "reset") return "err";
    if (action === "request_changes" || action === "block" || action === "re_review") return "warn";
    if (action === "pause" || action === "admin_pause") return "paused";
    return "neutral";
}

// Compact page-number window: always 1 and last, current ± 1, "…" in gaps.
// Pages are 0-indexed; returns numbers or the literal "…".
export function pageWindow(current, totalPages) {
    if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i);
    const set = new Set([0, totalPages - 1, current]);
    if (current - 1 >= 0) set.add(current - 1);
    if (current + 1 <= totalPages - 1) set.add(current + 1);
    if (current <= 2) { set.add(1); set.add(2); set.add(3); }
    if (current >= totalPages - 3) {
        set.add(totalPages - 2); set.add(totalPages - 3); set.add(totalPages - 4);
    }
    const sorted = [...set].filter(n => n >= 0 && n < totalPages).sort((a, b) => a - b);
    const out = [];
    for (let i = 0; i < sorted.length; i++) {
        out.push(sorted[i]);
        if (i < sorted.length - 1 && sorted[i + 1] !== sorted[i] + 1) out.push("…");
    }
    return out;
}

// Average of the last `n` entries of a daily series (for "média 7 dias").
export function trailingAverage(series, n = 7) {
    const tail = (series || []).slice(-n);
    if (!tail.length) return 0;
    return tail.reduce((s, r) => s + (Number(r.count) || 0), 0) / tail.length;
}
