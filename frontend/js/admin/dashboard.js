// Admin "Dashboard" tab: totals, completion %, daily reviews, per-operator stats.
// Self-contained: only consumes /api/admin/dashboard, owns its own per-operator
// sort state, builds DOM into the passed root.
import { apiGet } from "../api.js";

let opSortKey = "username";
let opSortDir = "asc";

export async function renderDashboard(root) {
    const d = await apiGet("/api/admin/dashboard");
    root.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "stats-grid";
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
    grid.append(
        statCard("Total de tiles", d.total_tiles),
        statCard("% concluído", `${d.completion_percent}%`),
        statCard("Classificados", classifiedTotal),
        statCard("Revisados", d.totals_by_status.reviewed || 0),
        statCard("Pendentes", d.totals_by_status.pending || 0),
        statCard("Em andamento", activeInProgress + activeInReview),
        statCard("Pausados", d.paused_count || 0),
        statCard("Bloqueados", d.totals_by_status.blocked || 0),
        statCard("Problemas", d.totals_by_status.problem || 0),
        statCard("Ritmo (tiles/dia)", d.rate_per_day),
        statCard("ETA (dias)", d.eta_days ?? "—"),
        statCard("Tempo médio / classificação", fmtDuration(d.avg_classify_seconds)),
        statCard("Tempo médio / revisão", fmtDuration(d.avg_review_seconds)),
    );
    root.appendChild(grid);

    const total = d.total_tiles || 1;
    const statusH = document.createElement("h3");
    statusH.textContent = "Distribuição por status";
    root.appendChild(statusH);
    // Build a display-status breakdown that pulls paused tiles out of
    // in_progress/in_review and lists them under their own bar. Order matches
    // the lifecycle (pending → ... → reviewed) plus exception states at the end.
    const displayCounts = {};
    for (const [k, v] of Object.entries(d.totals_by_status)) {
        if (k === "in_progress" || k === "in_review") {
            displayCounts[k] = v - (pausedByStatus[k] || 0);
        } else {
            displayCounts[k] = v;
        }
    }
    if (d.paused_count) displayCounts.paused = d.paused_count;
    const order = ["pending", "in_progress", "paused", "classified", "in_review", "reviewed", "problem", "blocked"];
    const sortedKeys = [
        ...order.filter(k => k in displayCounts),
        ...Object.keys(displayCounts).filter(k => !order.includes(k)),
    ];
    for (const k of sortedKeys) {
        const v = displayCounts[k];
        const row = document.createElement("div");
        row.className = "bar-row";
        const name = document.createElement("span"); name.className = "name"; name.textContent = k;
        const wrap = document.createElement("span"); wrap.className = "bar-wrap";
        const bar = document.createElement("span"); bar.className = "bar";
        bar.style.width = `${(v / total) * 100}%`;
        wrap.appendChild(bar);
        const count = document.createElement("span"); count.className = "count"; count.textContent = v;
        row.append(name, wrap, count);
        root.appendChild(row);
    }

    await renderClassDistribution(root);
    await renderFeatureDistribution(root);

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

function statCard(label, value) {
    const d = document.createElement("div");
    d.className = "stat-card";
    const v = document.createElement("div"); v.className = "value"; v.textContent = value;
    const l = document.createElement("div"); l.className = "label"; l.textContent = label;
    d.append(v, l);
    return d;
}


async function renderClassDistribution(root) {
    let dist = [];
    try { dist = await apiGet("/api/admin/class-distribution"); } catch { return; }
    if (!dist.length) return;
    const h = document.createElement("h3");
    h.textContent = "Distribuição por classe (pixels classificados)";
    root.appendChild(h);
    const note = document.createElement("p");
    note.className = "muted";
    note.style.fontSize = "12px";
    note.textContent = "Tiles antigos sem cache aparecem zerados — rode `python -m backend.scripts.recompute_class_counts` para preencher.";
    root.appendChild(note);
    const total = dist.reduce((s, c) => s + c.pixels, 0) || 1;
    for (const c of dist) {
        const row = document.createElement("div");
        row.className = "bar-row";
        const name = document.createElement("span");
        name.className = "name";
        const swatch = document.createElement("span");
        swatch.style.cssText =
            `display:inline-block;width:10px;height:10px;background:${c.color};margin-right:6px;border-radius:2px;border:1px solid rgba(0,0,0,.2);vertical-align:middle`;
        name.appendChild(swatch);
        name.append(document.createTextNode(c.name));
        const wrap = document.createElement("span");
        wrap.className = "bar-wrap";
        const bar = document.createElement("span");
        bar.className = "bar";
        bar.style.width = `${(c.pixels / total) * 100}%`;
        bar.style.background = c.color;
        wrap.appendChild(bar);
        const count = document.createElement("span");
        count.className = "count";
        count.textContent = `${c.pct}% (${c.pixels.toLocaleString("pt-BR")})`;
        row.append(name, wrap, count);
        root.appendChild(row);
    }
}


async function renderFeatureDistribution(root) {
    let dist = [];
    try { dist = await apiGet("/api/admin/feature-distribution"); } catch { return; }
    if (!dist.length) return;
    const h = document.createElement("h3");
    h.textContent = "Distribuição por atributo (features classificadas)";
    root.appendChild(h);
    // Group by (project_id, attribute_key) so each attribute gets a section.
    const groups = new Map();
    for (const e of dist) {
        const key = `${e.project_id}:${e.attribute_key}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(e);
    }
    for (const [key, entries] of groups) {
        const [pid, attrKey] = key.split(":");
        const sub = document.createElement("h4");
        sub.style.cssText = "margin-top:12px;font-size:13px;font-weight:600;";
        sub.textContent = `Projeto ${pid} · ${attrKey}`;
        root.appendChild(sub);
        const total = entries.reduce((s, e) => s + e.count, 0) || 1;
        for (const e of entries) {
            const row = document.createElement("div");
            row.className = "bar-row";
            const name = document.createElement("span");
            name.className = "name";
            name.textContent = e.value;
            const wrap = document.createElement("span");
            wrap.className = "bar-wrap";
            const bar = document.createElement("span");
            bar.className = "bar";
            bar.style.width = `${(e.count / total) * 100}%`;
            wrap.appendChild(bar);
            const count = document.createElement("span");
            count.className = "count";
            count.textContent = `${e.pct}% (${e.count.toLocaleString("pt-BR")})`;
            row.append(name, wrap, count);
            root.appendChild(row);
        }
    }
}
