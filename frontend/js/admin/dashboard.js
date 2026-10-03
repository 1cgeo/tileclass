// Admin "Dashboard" tab.
//  - Hero: scope name + completion + status distribution (stacked bar).
//  - KPI grid: volume, review backlog, pace, ETA, cycle times.
//  - Charts: reviewed-per-day (30 days) + class distribution (single project)
//    or per-kind summary (cross-project).
//  - Tables: per operator, and per project in the cross-project view.
// The project scope picker lives in the admin top bar (wired by admin.js).
import { apiGet } from "../api.js";
import { withProjectParam, kindLabel } from "../utils.js";
import {
    statusBreakdown, fillDailySeries, fmtInt, fmtPct, fmtCompact, fmtDecimal,
    fmtEtaDays, trailingAverage, addDaysIso,
} from "../admin-core.js";
import { h, card, kpi, avatar, progressBar, emptyState, sortHeader, icon } from "./ui.js";
import { statusStack, hbarList, dailyBarChart } from "./charts.js";

let opSortKey = "classified";
let opSortDir = "desc";
let projSortKey = "name";
let projSortDir = "asc";

// "Por classe" breakdown per kind: endpoint + count field + unit.
const _KIND_BREAKDOWN = {
    raster: { endpoint: "/api/admin/class-distribution", field: "pixels", unit: "px" },
    classification: { endpoint: "/api/admin/tile-class-distribution", field: "count", unit: "tiles" },
};
const _KIND_ORDER = ["raster", "classification"];

const DATE_SHORT = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", timeZone: "UTC" });

export async function renderDashboard(root, {
    projectId = null, projectsById = {}, onSelectProject = null, onOpenStatus = null,
} = {}) {
    const scoped = projectId != null;
    const kindGuess = scoped ? projectsById[projectId]?.kind : null;
    const [d, projectsStats, classDist] = await Promise.all([
        apiGet(withProjectParam("/api/admin/dashboard", projectId)),
        scoped ? Promise.resolve(null) : apiGet("/api/admin/projects-stats").catch(() => []),
        scoped && _KIND_BREAKDOWN[kindGuess]
            ? apiGet(withProjectParam(_KIND_BREAKDOWN[kindGuess].endpoint, projectId)).catch(() => [])
            : Promise.resolve(null),
    ]);
    if (!root.isConnected) return;  // tab changed while loading
    root.textContent = "";
    const page = h("div", { class: "dash" });
    root.appendChild(page);

    const t = d.totals_by_status || {};
    const paused = d.paused_by_status || {};
    const classified = (t.classified || 0) + (t.in_review || 0) + (t.reviewed || 0);
    const total = d.total_tiles || 0;
    const segments = statusBreakdown(t, paused);
    const today = new Date().toISOString().slice(0, 10);
    const series = fillDailySeries(d.daily_completed, today, 30);
    const reviewed30 = series.reduce((a, r) => a + r.count, 0);

    page.appendChild(renderHero({ d, scoped, projectId, projectsById, segments,
                                  classified, total, onOpenStatus }));

    if (!total) {
        page.appendChild(card({},
            emptyState({
                icon: "inbox", title: "Nenhum tile ainda",
                text: "Importe tiles pela linha de comando (python -m backend.scripts.import_points) para acompanhar o progresso aqui.",
            })));
        return;
    }

    // ---- KPIs ----
    const aggregated = !scoped
        ? "Métrica agregada de todos os projetos — pode misturar perfis diferentes. Escolha um projeto no seletor para ver o número real."
        : null;
    const awaiting = (t.classified || 0) + (t.in_review || 0);
    const inReviewNow = (t.in_review || 0) - (paused.in_review || 0);
    const activeIp = (t.in_progress || 0) - (paused.in_progress || 0);
    const etaDate = d.eta_days ? addDaysIso(today, Math.ceil(d.eta_days)) : null;
    page.appendChild(h("div", { class: "kpi-grid" },
        kpi({ icon: "layout-grid", label: "Total de tiles", value: fmtInt(total),
              sub: `${fmtInt(t.pending || 0)} pendentes`, tone: "neutral" }),
        kpi({ icon: "circle-check", label: "Revisados", value: fmtInt(t.reviewed || 0),
              sub: `${fmtPct(total ? (100 * (t.reviewed || 0)) / total : 0)} do total`, tone: "ok" }),
        kpi({ icon: "user-check", label: "Aguardando revisão", value: fmtInt(awaiting),
              sub: inReviewNow > 0 ? `${fmtInt(inReviewNow)} em revisão agora` : "fila de revisão", tone: "info" }),
        kpi({ icon: "activity", label: "Em andamento", value: fmtInt(activeIp + inReviewNow),
              sub: d.paused_count ? `${fmtInt(d.paused_count)} pausados` : "nenhum pausado", tone: "accent" }),
        kpi({ icon: "gauge", label: "Ritmo", value: `${fmtDecimal(d.rate_per_day, 1)}/dia`,
              sub: `${aggregated ? "agregado · " : ""}classificados, média de 7 dias`, tone: "accent",
              title: aggregated }),
        kpi({ icon: "calendar", label: "Previsão de término", value: fmtEtaDays(d.eta_days),
              sub: `${aggregated ? "agregado · " : ""}${etaDate ? `por volta de ${DATE_SHORT.format(new Date(`${etaDate}T00:00:00Z`)).replace(".", "")}` : "sem ritmo recente"}`,
              tone: "warn", title: aggregated }),
        kpi({ icon: "timer", label: "Tempo médio · classificação", value: fmtDuration(d.avg_classify_seconds),
              sub: "por tile, sem pausas", tone: "neutral", title: aggregated }),
        kpi({ icon: "clock", label: "Tempo médio · revisão", value: fmtDuration(d.avg_review_seconds),
              sub: "por tile, sem pausas", tone: "neutral", title: aggregated }),
    ));

    // ---- Charts row ----
    const avg7 = trailingAverage(series, 7);
    const daily = card({
        title: "Tiles revisados por dia", icon: "chart-column",
        subtitle: `${fmtInt(reviewed30)} nos últimos 30 dias · média de ${fmtDecimal(avg7, 1)}/dia na última semana`,
        cls: "dash-daily",
    }, reviewed30 ? dailyBarChart(series, { label: "Tiles revisados por dia", unit: "tiles" })
                  : emptyState({ icon: "chart-column", title: "Sem revisões nos últimos 30 dias" }));
    const side = scoped
        ? renderClassCard(classDist, d.project_kind || kindGuess)
        : renderKindCard(d);
    page.appendChild(h("div", { class: "dash-row" }, daily, side));

    if (!scoped && projectsStats && projectsStats.length) {
        page.appendChild(renderPerProjectCard(projectsStats, onSelectProject));
    }
    page.appendChild(renderPerOperatorCard(d.per_operator || []));
}

function renderHero({ d, scoped, projectId, projectsById, segments,
                      classified, total, onOpenStatus }) {
    const proj = scoped ? projectsById[projectId] : null;
    const kind = d.project_kind || proj?.kind;
    const t = d.totals_by_status || {};
    const eyebrow = scoped
        ? h("div", { class: "dash-hero-eyebrow" },
            h("span", { class: "chip accent", text: kindLabel(kind) }),
            proj && proj.active === false ? h("span", { class: "chip warn", text: "Inativo" }) : null)
        : h("div", { class: "dash-hero-eyebrow" },
            h("span", { class: "chip accent", text: "Visão consolidada" }),
            h("span", { class: "muted", text: `${fmtInt(d.project_count || 0)} projetos ativos` }));
    const title = h("h2", { class: "dash-hero-title", text: scoped ? (proj?.name || `Projeto #${projectId}`) : "Todos os projetos" });
    const desc = scoped && proj?.description ? h("p", { class: "dash-hero-desc", text: proj.description }) : null;

    const pct = Number(d.completion_percent) || 0;
    const big = h("div", { class: "dash-hero-metric" },
        h("div", { class: "dash-hero-pct tabular" }, fmtDecimal(pct, 1), h("span", { text: "%" })),
        h("div", { class: "dash-hero-pct-label", text: `${fmtInt(classified)} de ${fmtInt(total)} tiles classificados` }));

    // Exceptions that need the admin's attention, as quick links.
    const attention = h("div", { class: "dash-attention" });
    const addFlag = (count, label, status, ic, tone) => {
        if (!count) return;
        const b = h("button", { type: "button", class: `attention-pill ap-${tone}`, title: "Ver estes tiles" },
            icon(ic, "icon-sm"), h("strong", { class: "tabular", text: fmtInt(count) }), ` ${label}`);
        if (onOpenStatus) b.addEventListener("click", () => onOpenStatus(status));
        else b.disabled = true;
        attention.appendChild(b);
    };
    addFlag(t.problem || 0, (t.problem || 0) === 1 ? "problema" : "problemas", "problem", "triangle-alert", "err");
    addFlag(d.paused_count || 0, (d.paused_count || 0) === 1 ? "pausado" : "pausados", "paused", "circle-pause", "paused");
    addFlag(t.blocked || 0, (t.blocked || 0) === 1 ? "bloqueado" : "bloqueados", "blocked", "lock", "neutral");
    if (!attention.children.length && total) {
        attention.appendChild(h("span", { class: "attention-pill ap-ok is-static" },
            icon("circle-check", "icon-sm"), "Nada pendente de atenção"));
    }

    const hero = h("section", { class: "card dash-hero" },
        h("div", { class: "dash-hero-top" },
            h("div", { class: "dash-hero-id" }, eyebrow, title, desc),
            big),
        total ? statusStack(segments, { onSelect: onOpenStatus }) : null,
        total ? attention : null);
    return hero;
}

function renderClassCard(items, kind) {
    const cfg = _KIND_BREAKDOWN[kind];
    const subtitle = kind === "raster" ? "Pixels por classe nos tiles classificados" : "Tiles por classe atribuída";
    if (!cfg || !items || !items.length) {
        return card({ title: "Distribuição por classe", icon: "palette", subtitle, cls: "dash-side" },
            emptyState({ icon: "palette", title: "Sem dados ainda", text: "Aparece quando houver tiles classificados." }));
    }
    const rows = items.map(e => ({
        name: e.name, color: e.color, value: e[cfg.field], pct: e.pct,
        valueText: cfg.unit === "px" ? `${fmtCompact(e[cfg.field])} px` : fmtInt(e[cfg.field]),
    }));
    return card({ title: "Distribuição por classe", icon: "palette", subtitle, cls: "dash-side" }, hbarList(rows));
}

function renderKindCard(d) {
    const projects = d.projects_by_kind || {};
    const tiles = d.tiles_by_kind || {};
    const kinds = _KIND_ORDER.filter(k => (projects[k] || 0) > 0);
    const body = kinds.length
        ? h("ul", { class: "kind-list" }, kinds.map(k => h("li", { class: "kind-row" },
            h("span", { class: "kind-icon" }, icon(k === "raster" ? "paintbrush" : "tag")),
            h("span", { class: "kind-name", text: kindLabel(k) }),
            h("span", { class: "kind-stat" }, h("strong", { class: "tabular", text: fmtInt(projects[k]) }),
                h("span", { class: "muted", text: projects[k] === 1 ? " projeto" : " projetos" })),
            h("span", { class: "kind-stat" }, h("strong", { class: "tabular", text: fmtInt(tiles[k] || 0) }),
                h("span", { class: "muted", text: " tiles" })))))
        : emptyState({ icon: "folder", title: "Nenhum projeto ativo" });
    return card({ title: "Por tipo de projeto", icon: "layers", subtitle: "Projetos ativos e tiles por tipo", cls: "dash-side" }, body);
}

function renderPerOperatorCard(rows) {
    const host = h("div", { class: "table-wrap" });
    const active = rows.filter(r => (r.classified || 0) + (r.reviewed || 0) + (r.problems || 0) > 0);
    const hidden = rows.length - active.length;
    const draw = () => {
        host.textContent = "";
        if (!active.length) {
            host.appendChild(emptyState({ icon: "users", title: "Sem atividade registrada" }));
            return;
        }
        const maxCls = Math.max(1, ...active.map(r => r.classified || 0));
        const sorted = [...active].sort((a, b) => {
            const va = a[opSortKey] ?? 0, vb = b[opSortKey] ?? 0;
            const cmp = typeof va === "string" ? va.localeCompare(vb, "pt-BR") : va - vb;
            return opSortDir === "asc" ? cmp : -cmp;
        });
        const cols = [
            ["username", "Usuário", false], ["classified", "Classificados", true],
            ["reviewed", "Revisados", true], ["problems", "Problemas", true],
            ["avg_classify_seconds", "Tempo médio · classif.", true],
            ["avg_review_seconds", "Tempo médio · revisão", true],
        ];
        const onSort = (k) => {
            if (opSortKey === k) opSortDir = opSortDir === "asc" ? "desc" : "asc";
            else { opSortKey = k; opSortDir = k === "username" ? "asc" : "desc"; }
            draw();
        };
        const table = h("table", { class: "table admin-table op-table" },
            h("thead", {}, h("tr", {}, cols.map(([k, l, num]) =>
                sortHeader(l, { key: k, activeKey: opSortKey, dir: opSortDir, onSort, num })))),
            h("tbody", {}, sorted.map(op => {
                const share = progressBar((100 * (op.classified || 0)) / maxCls, { label: "participação" });
                share.classList.add("progress-inline");
                return h("tr", {},
                    h("td", {}, h("span", { class: "user-cell" }, avatar(op.username, { size: "sm" }),
                        h("span", { class: "user-cell-name", text: op.username }))),
                    h("td", { class: "num" }, h("span", { class: "num-with-bar" },
                        share, h("span", { class: "tabular", text: fmtInt(op.classified || 0) }))),
                    h("td", { class: "num", text: fmtInt(op.reviewed || 0) }),
                    h("td", { class: `num${op.problems ? " text-err" : " dim"}`, text: fmtInt(op.problems || 0) }),
                    h("td", { class: "num", text: fmtDuration(op.avg_classify_seconds) }),
                    h("td", { class: "num", text: fmtDuration(op.avg_review_seconds) }));
            })));
        host.appendChild(table);
    };
    draw();
    return card({
        title: "Por operador", icon: "users",
        subtitle: hidden ? `${fmtInt(active.length)} com atividade · ${fmtInt(hidden)} sem atividade ocultos` : `${fmtInt(active.length)} com atividade`,
        cls: "card-flush",
    }, host);
}

function renderPerProjectCard(projects, onSelectProject) {
    const host = h("div", { class: "table-wrap" });
    for (const p of projects) p.pending_tiles = (p.total_tiles || 0) - (p.classified_tiles || 0);
    const cols = [
        ["name", "Projeto", false], ["kind", "Tipo", false], ["total_tiles", "Tiles", true],
        ["completion_percent", "Progresso", false], ["rate_per_day", "Ritmo", true], ["eta_days", "Previsão", true],
    ];
    const draw = () => {
        host.textContent = "";
        const sorted = [...projects].sort((a, b) => {
            const va = a[projSortKey] ?? -1, vb = b[projSortKey] ?? -1;
            const cmp = typeof va === "string" ? va.localeCompare(vb, "pt-BR") : va - vb;
            return projSortDir === "asc" ? cmp : -cmp;
        });
        const onSort = (k) => {
            if (projSortKey === k) projSortDir = projSortDir === "asc" ? "desc" : "asc";
            else { projSortKey = k; projSortDir = k === "name" ? "asc" : "desc"; }
            draw();
        };
        host.appendChild(h("table", { class: "table admin-table" },
            h("thead", {}, h("tr", {}, cols.map(([k, l, num]) =>
                sortHeader(l, { key: k, activeKey: projSortKey, dir: projSortDir, onSort, num })))),
            h("tbody", {}, sorted.map(p => {
                const tr = h("tr", { class: `${p.active ? "" : "is-inactive"}${onSelectProject ? " is-clickable" : ""}`,
                                     title: onSelectProject ? "Abrir o dashboard deste projeto" : null },
                    h("td", {}, h("span", { class: "cell-inline" }, h("span", { class: "cell-strong", text: p.name }),
                        p.active ? null : h("span", { class: "chip warn chip-xs", text: "inativo" }))),
                    h("td", {}, h("span", { class: "chip", text: kindLabel(p.kind) })),
                    h("td", { class: "num", text: fmtInt(p.total_tiles) }),
                    h("td", {}, h("span", { class: "progress-cell" },
                        progressBar(p.completion_percent, { label: "concluído" }),
                        h("span", { class: "tabular progress-cell-pct", text: fmtPct(p.completion_percent) }))),
                    h("td", { class: "num", text: p.rate_per_day ? `${fmtDecimal(p.rate_per_day, 1)}/dia` : "—" }),
                    h("td", { class: "num", text: fmtEtaDays(p.eta_days) }));
                if (onSelectProject) tr.addEventListener("click", () => onSelectProject(p.id));
                return tr;
            }))));
    };
    draw();
    return card({ title: "Por projeto", icon: "folder", subtitle: "Clique numa linha para focar o dashboard no projeto", cls: "card-flush" }, host);
}

// Exported so other admin tables render compatible duration strings.
export function fmtDuration(sec) {
    const s = Number(sec) || 0;
    if (s <= 0) return "—";
    if (s < 60) return `${s.toFixed(1).replace(".", ",")}s`;
    const m = Math.floor(s / 60);
    const r = Math.round(s - m * 60);
    if (m < 60) return `${m}m ${r}s`;
    const hh = Math.floor(m / 60);
    return `${hh}h ${m - hh * 60}m`;
}
