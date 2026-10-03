// Admin "Dashboard" tab. Two layers:
//  - Generic: totals, completion %, ritmo/ETA, médias, per-operator, daily.
//    These apply to every project kind so they always render.
//  - Kind-specific: rendered only when a single project is selected. Each kind
//    has its own "Por classe" breakdown sourced from the matching
//    distribution endpoint. Cross-project view skips it — palettes and units
//    (pixels vs tiles) don't share a denominator.
import { apiGet } from "../api.js";
import { withProjectParam, KIND_LABELS, kindLabel } from "../utils.js";

let opSortKey = "username";
let opSortDir = "asc";

// What "classificado" means per kind. Hits a single endpoint and renders one
// card per class. `field` is the count key the backend returns;
// `valueSuffix` is appended to the number inside each card (e.g. " px" for
// raster pixel counts).
const _KIND_BREAKDOWN = {
    raster: {
        endpoint: "/api/admin/class-distribution",
        field: "pixels",
        sectionTitle: "Distribuição por classe",
        valueSuffix: " px",
    },
    classification: {
        endpoint: "/api/admin/tile-class-distribution",
        field: "count",
        sectionTitle: "Distribuição por classe",
        valueSuffix: "",
    },
};

// `_KIND_ORDER` controls the rendering order of the "by-kind" cards
// (lifecycle: most common raster first, niche kinds last). Labels come
// from utils.js (`KIND_LABELS` / `kindLabel`) so editor, dashboard, and
// projects stay in sync.
const _KIND_ORDER = ["raster", "classification"];

export async function renderDashboard(root, {
    projectId = null, projectsById = {}, onSelectProject = null,
} = {}) {
    // Cross-project view also renders a per-project table — kick off both
    // fetches in parallel so the page paints in one round-trip instead of two
    // serial. Scoped view skips the second fetch entirely.
    const needsProjectsStats = projectId == null;
    const [d, projectsStats] = await Promise.all([
        apiGet(withProjectParam("/api/admin/dashboard", projectId)),
        needsProjectsStats
            ? apiGet("/api/admin/projects-stats").catch(() => [])
            : Promise.resolve(null),
    ]);
    root.innerHTML = "";
    // Local project filter — every tab carries its own scope picker now that
    // the global header dropdown was removed. The select itself is wired
    // outside the dashboard module (renderDashboard would need to import
    // admin.js otherwise — kept layered: admin.js builds + wires the picker
    // around our content).
    if (onSelectProject) {
        const filterBar = document.createElement("div");
        filterBar.className = "filter-bar dashboard-filter-bar";
        filterBar.innerHTML = `
            <label id="dashboard-project-filter-wrap" class="hidden">Projeto
                <select id="dashboard-project-filter"></select>
            </label>
        `;
        root.appendChild(filterBar);
    }
    const grid = document.createElement("div");
    grid.className = "stats-grid";

    // Cross-project warning suffix for metrics that average across projects
    // with different kinds/scales. Shown as a title tooltip on the value —
    // the number is still computed correctly, it's just that "average ETA
    // across raster + classification" is rarely what the admin
    // really wants to know.
    const aggregatedTooltip = projectId == null
        ? "Métrica agregada de todos os projetos — pode misturar perfis diferentes (kinds, velocidades). Filtre um projeto na barra superior para ver o número real."
        : "";
    // Cumulative classified count: every tile that finished the classify step.
    // status='classified' awaits review, 'in_review' is under review, 'reviewed'
    // completed it — all three imply the classify happened.
    const classifiedTotal =
        (d.totals_by_status.classified || 0)
        + (d.totals_by_status.in_review || 0)
        + (d.totals_by_status.reviewed || 0);
    // "Em andamento" counts only ACTIVE tiles. Paused tiles (operator left
    // mid-work) get their own card so the team sees stalled work distinctly.
    const pausedByStatus = d.paused_by_status || {};
    const activeInProgress = (d.totals_by_status.in_progress || 0) - (pausedByStatus.in_progress || 0);
    const activeInReview = (d.totals_by_status.in_review || 0) - (pausedByStatus.in_review || 0);
    // Portfolio card: always visible. When project-scoped, shows the kind of
    // the current project as a secondary cue (admins jump between projects
    // and the label confirms "what am I looking at").
    const portfolioValue = projectId != null
        ? kindLabel(d.project_kind || projectsById[projectId]?.kind || "")
        : (d.project_count ?? 0);
    const portfolioLabel = projectId != null ? "Tipo do projeto" : "Projetos ativos";
    grid.append(
        statCard(portfolioLabel, portfolioValue),
        statCard("Total de tiles", d.total_tiles),
        statCard("% concluído", `${d.completion_percent}%`),
        statCard("Classificados", classifiedTotal),
        statCard("Revisados", d.totals_by_status.reviewed || 0),
        statCard("Pendentes", d.totals_by_status.pending || 0),
        statCard("Em andamento", activeInProgress + activeInReview),
        statCard("Pausados", d.paused_count || 0),
        statCard("Bloqueados", d.totals_by_status.blocked || 0),
        statCard("Problemas", d.totals_by_status.problem || 0),
        statCard("Ritmo (tiles/dia)", d.rate_per_day, { tooltip: aggregatedTooltip }),
        statCard("ETA (dias)", d.eta_days ?? "—", { tooltip: aggregatedTooltip }),
        statCard("Tempo médio / classificação", fmtDuration(d.avg_classify_seconds), { tooltip: aggregatedTooltip }),
        statCard("Tempo médio / revisão", fmtDuration(d.avg_review_seconds), { tooltip: aggregatedTooltip }),
    );
    root.appendChild(grid);

    // Cross-project portfolio summary: per-kind project + tile counts. Hidden
    // when scoped because both figures would degenerate to the selected
    // project (1 project, total_tiles).
    if (projectId == null) {
        renderPortfolioByKind(root, d);
        // Per-project breakdown — uses the stats fetched in parallel above.
        if (projectsStats && projectsStats.length) {
            renderPerProjectTable(root, projectsStats, onSelectProject);
        }
    }

    // Kind-specific breakdown sits between the generic totals and the
    // "Por operador" / "Tiles revisados por dia" sections. Only renders when
    // a single project is picked — cross-project mixes incompatible palettes.
    if (projectId != null) {
        const kind = d.project_kind || projectsById[projectId]?.kind;
        if (_KIND_BREAKDOWN[kind]) {
            await renderClassBreakdown(root, kind, projectId);
        }
    }

    const opH = document.createElement("h3");
    opH.textContent = "Por operador";
    opH.style.marginTop = "16px";
    root.appendChild(opH);
    renderPerOperator(root, d.per_operator);

    const dayH = document.createElement("h3");
    dayH.textContent = "Tiles revisados por dia";
    dayH.style.marginTop = "16px";
    root.appendChild(dayH);
    const maxDaily = Math.max(1, ...d.daily_completed.map(r => r.count));
    for (const r of d.daily_completed) {
        const row = document.createElement("div");
        row.className = "bar-row";
        const name = document.createElement("span"); name.className = "name"; name.textContent = r.date;
        const wrap = document.createElement("span"); wrap.className = "bar-wrap";
        const bar = document.createElement("span"); bar.className = "bar";
        bar.style.width = `${(r.count / maxDaily) * 100}%`;
        wrap.appendChild(bar);
        const count = document.createElement("span"); count.className = "count"; count.textContent = r.count;
        row.append(name, wrap, count);
        root.appendChild(row);
    }
}

function renderPerOperator(root, rows) {
    // Remove previous table if any (re-sort re-renders in place)
    const prev = root.querySelector(".per-op-wrap");
    if (prev) prev.remove();
    const wrap = document.createElement("div");
    wrap.className = "per-op-wrap";
    const table = document.createElement("table");
    table.className = "admin-table";
    const cols = [
        ["username", "Usuário"],
        ["classified", "Classificados"],
        ["reviewed", "Revisados"],
        ["problems", "Problemas"],
        ["avg_classify_seconds", "Tempo médio classificação"],
        ["avg_review_seconds", "Tempo médio revisão"],
    ];
    const thead = document.createElement("thead");
    const trh = document.createElement("tr");
    for (const [k, lbl] of cols) {
        const th = document.createElement("th");
        th.textContent = lbl;
        th.style.cursor = "pointer";
        th.title = "Clique para ordenar";
        if (opSortKey === k) th.textContent += opSortDir === "asc" ? " ▲" : " ▼";
        th.addEventListener("click", () => {
            if (opSortKey === k) opSortDir = opSortDir === "asc" ? "desc" : "asc";
            else { opSortKey = k; opSortDir = "asc"; }
            renderPerOperator(root, rows);
        });
        trh.appendChild(th);
    }
    thead.appendChild(trh);
    table.appendChild(thead);
    const sorted = [...rows].sort((a, b) => {
        const va = a[opSortKey] ?? 0, vb = b[opSortKey] ?? 0;
        if (va < vb) return opSortDir === "asc" ? -1 : 1;
        if (va > vb) return opSortDir === "asc" ? 1 : -1;
        return 0;
    });
    const tbody = document.createElement("tbody");
    for (const op of sorted) {
        const tr = document.createElement("tr");
        [op.username, op.classified || 0, op.reviewed || 0, op.problems || 0,
         fmtDuration(op.avg_classify_seconds), fmtDuration(op.avg_review_seconds)]
            .forEach(v => { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); });
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    root.appendChild(wrap);
}

// Exported so the legacy per-operator tables in admin.js (users + tiles tabs)
// can render compatible duration strings without re-implementing the formatter.
export function fmtDuration(sec) {
    const s = Number(sec) || 0;
    if (s <= 0) return "—";
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    const r = Math.round(s - m * 60);
    if (m < 60) return `${m}m ${r}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m - h * 60}m`;
}

function statCard(label, value, { tooltip = "" } = {}) {
    const d = document.createElement("div");
    d.className = "stat-card";
    if (tooltip) {
        // Visually nudge the user that the card has extra context. The full
        // explanation comes from the native title — enough for a hover-poll.
        d.classList.add("stat-card-aggregated");
        d.title = tooltip;
    }
    // Text values like "Segmentação" overflow the 28px numeric style. Detect
    // strings that aren't a number/duration ("12s", "640m", "30%") and tone
    // the size down so they fit a 180px card without truncation.
    const str = String(value);
    const numericish = /^[\d—-]/.test(str);
    if (typeof value === "string" && !numericish && str.length > 4) {
        d.classList.add("stat-card-text");
    }
    const v = document.createElement("div"); v.className = "value"; v.textContent = value;
    const l = document.createElement("div"); l.className = "label"; l.textContent = label;
    d.append(v, l);
    return d;
}

// One card per class, with a color swatch on the left edge. Used for raster
// (pixels) and classification (tiles) breakdowns.
function classStatCard(name, color, count, pct, valueSuffix = "") {
    const card = document.createElement("div");
    card.className = "stat-card stat-card-class";
    if (color) card.style.setProperty("--swatch-color", color);
    const v = document.createElement("div");
    v.className = "value";
    v.textContent = `${count.toLocaleString("pt-BR")}${valueSuffix}`;
    const pctEl = document.createElement("div");
    pctEl.className = "stat-card-pct";
    pctEl.textContent = `${pct}%`;
    const l = document.createElement("div");
    l.className = "label";
    l.textContent = name;
    card.append(v, pctEl, l);
    return card;
}

// Sort state for the per-project table. Lives at module scope so re-renders
// (e.g. after the admin clicks a column header) keep the choice.
let projSortKey = "name";
let projSortDir = "asc";

function renderPerProjectTable(root, projects, onSelectProject) {
    if (!projects.length) return;

    const h = document.createElement("h3");
    h.textContent = "Por projeto";
    h.style.marginTop = "16px";
    root.appendChild(h);
    const note = document.createElement("p");
    note.className = "muted";
    note.style.fontSize = "12px";
    note.textContent = onSelectProject
        ? "Clique numa linha para filtrar o dashboard naquele projeto."
        : "";
    root.appendChild(note);
    const host = document.createElement("div");
    host.className = "per-project-wrap";
    root.appendChild(host);
    drawPerProjectTable(host, projects, onSelectProject);
}

function drawPerProjectTable(host, projects, onSelectProject) {
    const cols = [
        ["name", "Projeto"],
        ["kind", "Tipo"],
        ["total_tiles", "Total"],
        ["pending_tiles", "Pendentes"],     // derived (total - classified)
        ["classified_tiles", "Classificados"],
        ["completion_percent", "% Concluído"],
        ["rate_per_day", "Ritmo (t/dia)"],
        ["eta_days", "ETA (dias)"],
    ];
    // Derive `pending_tiles` so the sort key works without backend changes.
    for (const p of projects) {
        p.pending_tiles = (p.total_tiles || 0) - (p.classified_tiles || 0);
    }
    const sorted = [...projects].sort((a, b) => {
        const va = a[projSortKey] ?? 0, vb = b[projSortKey] ?? 0;
        if (typeof va === "string") {
            const cmp = va.localeCompare(vb, "pt-BR");
            return projSortDir === "asc" ? cmp : -cmp;
        }
        if (va < vb) return projSortDir === "asc" ? -1 : 1;
        if (va > vb) return projSortDir === "asc" ? 1 : -1;
        return 0;
    });
    host.innerHTML = "";
    const table = document.createElement("table");
    table.className = "admin-table per-project-table";
    const thead = document.createElement("thead");
    const trh = document.createElement("tr");
    for (const [k, lbl] of cols) {
        const th = document.createElement("th");
        th.textContent = lbl + (projSortKey === k ? (projSortDir === "asc" ? " ▲" : " ▼") : "");
        th.style.cursor = "pointer";
        th.onclick = () => {
            if (projSortKey === k) projSortDir = projSortDir === "asc" ? "desc" : "asc";
            else { projSortKey = k; projSortDir = k === "name" ? "asc" : "desc"; }
            drawPerProjectTable(host, projects, onSelectProject);
        };
        trh.appendChild(th);
    }
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const p of sorted) {
        const tr = document.createElement("tr");
        if (!p.active) tr.classList.add("muted");
        if (onSelectProject) {
            tr.style.cursor = "pointer";
            tr.onclick = () => onSelectProject(p.id);
        }
        const cells = [
            p.name,
            kindLabel(p.kind),
            p.total_tiles,
            p.pending_tiles,
            p.classified_tiles,
            `${p.completion_percent}%`,
            p.rate_per_day,
            p.eta_days ?? "—",
        ];
        for (const v of cells) {
            const td = document.createElement("td");
            td.textContent = v;
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    host.appendChild(table);
}


function renderPortfolioByKind(root, d) {
    const projects = d.projects_by_kind || {};
    const tiles = d.tiles_by_kind || {};
    const kinds = _KIND_ORDER.filter(k => (projects[k] || 0) > 0);
    if (!kinds.length) return;
    const h = document.createElement("h3");
    h.textContent = "Por tipo de projeto";
    h.style.marginTop = "16px";
    root.appendChild(h);

    const subProjects = document.createElement("h4");
    subProjects.className = "dashboard-subhead";
    subProjects.textContent = "Projetos";
    root.appendChild(subProjects);
    const projectsGrid = document.createElement("div");
    projectsGrid.className = "stats-grid";
    for (const k of kinds) {
        projectsGrid.appendChild(statCard(kindLabel(k), projects[k]));
    }
    root.appendChild(projectsGrid);

    // Tile counts only if the backend provided them (cross-project view) and
    // at least one kind has any tile — skip the section entirely otherwise so
    // a fresh deploy doesn't show a row of zeros.
    const tileKinds = _KIND_ORDER.filter(k => (tiles[k] || 0) > 0);
    if (!tileKinds.length) return;
    const subTiles = document.createElement("h4");
    subTiles.className = "dashboard-subhead";
    subTiles.textContent = "Tiles";
    root.appendChild(subTiles);
    const tilesGrid = document.createElement("div");
    tilesGrid.className = "stats-grid";
    for (const k of tileKinds) {
        tilesGrid.appendChild(statCard(kindLabel(k), tiles[k]));
    }
    root.appendChild(tilesGrid);
}


async function renderClassBreakdown(root, kind, projectId) {
    const cfg = _KIND_BREAKDOWN[kind];
    let items = [];
    try { items = await apiGet(withProjectParam(cfg.endpoint, projectId)); } catch { return; }
    if (!items.length) return;

    const h = document.createElement("h3");
    h.textContent = cfg.sectionTitle;
    h.style.marginTop = "16px";
    root.appendChild(h);

    const grid = document.createElement("div");
    grid.className = "stats-grid";
    for (const e of items) {
        grid.appendChild(
            classStatCard(e.name, e.color, e[cfg.field], e.pct, cfg.valueSuffix),
        );
    }
    root.appendChild(grid);
}
