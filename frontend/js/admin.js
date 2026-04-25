// Admin panel: dashboard, tiles (list+grid+bulk+filters+viewer), problems, users.
import { apiGet, apiGetBlob, apiGetWithHeaders, apiPostJson, apiJson, logout as apiLogout } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap } from "./maplib.js";
import { hexToRgb, blobToImage, escapeHtml as escape, fmtDate } from "./utils.js";

let tileserverUrl = "";
let tileserverMaxZoom = 22;
let classes = [];
let classesById = {};
let selectedIds = new Set();
let currentTab = "dashboard";
let listView = "table"; // "table" | "grid"

// Map tab — rectangle-selection state. Lives outside renderMap so the click
// handler on tile layers can suppress the viewer while the tool is active.
let mapSelectedIds = new Set();
let mapTilePropsById = new Map();   // id -> feature properties (for status filtering)
let mapRectSelectActive = false;

// Sort + pagination state for the tiles tab
let sortKey = "id";
let sortDir = "asc";
let page = 0;
const PAGE_SIZE = 100;
let totalTiles = 0;

// Sort state for dashboard per-operator table
let opSortKey = "username";
let opSortDir = "asc";

// Active MapLibre instance for the "Mapa" tab, disposed on tab change.
let mapView = null;

const BLOCKABLE_STATES = new Set(["pending"]);
const isBlockable = t => BLOCKABLE_STATES.has(t.status);

export async function initAdmin(user) {
    document.getElementById("admin-user-label").textContent = `${user.username} (admin)`;
    document.getElementById("btn-admin-logout").addEventListener("click", async () => {
        await apiLogout(); location.reload();
    });
    document.querySelectorAll(".admin-nav button").forEach(btn => {
        btn.addEventListener("click", () => selectTab(btn.dataset.tab));
    });
    const [cfg, cls] = await Promise.all([
        apiGet("/api/config/tileserver"),
        apiGet("/api/config/classes"),
    ]);
    tileserverUrl = cfg.url_template;
    tileserverMaxZoom = cfg.max_zoom ?? 22;
    classes = cls;
    classesById = Object.fromEntries(classes.map(c => [c.id, c]));
    const vtModal = document.getElementById("modal-view-tile");
    const closeVt = () => vtModal.classList.add("hidden");
    document.getElementById("view-tile-close").addEventListener("click", closeVt);
    // Click on the backdrop (outside the modal-box) closes the viewer.
    vtModal.addEventListener("click", (ev) => { if (ev.target === vtModal) closeVt(); });
    document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && !vtModal.classList.contains("hidden")) closeVt();
    });
    wireConfirmModal();
    await selectTab("dashboard");
}

async function selectTab(tab) {
    currentTab = tab;
    selectedIds.clear();
    page = 0;
    if (mapView) { mapView.remove(); mapView = null; }
    document.querySelectorAll(".admin-nav button").forEach(b => {
        b.classList.toggle("active", b.dataset.tab === tab);
    });
    const content = document.getElementById("admin-content");
    content.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando...</div>`;
    try {
        if (tab === "dashboard") await renderDashboard(content);
        else if (tab === "tiles") await renderTiles(content);
        else if (tab === "map") await renderMap(content);
        else if (tab === "problems") await renderProblems(content);
        else if (tab === "users") await renderUsers(content);
    } catch (e) {
        renderError(content, e);
    }
}

function renderError(target, e) {
    target.textContent = "";
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = `Erro: ${e.message}`;
    target.appendChild(p);
}

async function renderDashboard(root) {
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
    grid.append(
        statCard("Total de tiles", d.total_tiles),
        statCard("% concluído", `${d.completion_percent}%`),
        statCard("Classificados", classifiedTotal),
        statCard("Revisados", d.totals_by_status.reviewed || 0),
        statCard("Pendentes", d.totals_by_status.pending || 0),
        statCard("Em andamento", (d.totals_by_status.in_progress || 0) + (d.totals_by_status.in_review || 0)),
        statCard("Pausados", d.paused_count || 0),
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
    for (const [k, v] of Object.entries(d.totals_by_status)) {
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

function fmtDuration(sec) {
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

async function renderTiles(root) {
    root.innerHTML = `
        <div class="filter-bar">
            <label>Buscar <input type="search" id="filter-q" placeholder="ID ou nome (parcial)" autocomplete="off"></label>
            <label>Status <select id="filter-status">
                <option value="">(todos)</option>
                <option>pending</option><option>in_progress</option><option>classified</option>
                <option>in_review</option><option>reviewed</option><option>problem</option>
                <option>blocked</option>
            </select></label>
            <label>De <input type="date" id="filter-from"></label>
            <label>Até <input type="date" id="filter-to"></label>
            <button id="btn-filter">Filtrar</button>
            <div class="view-mode-toggle">
                <button id="view-table" class="${listView==='table'?'active':''}">Tabela</button>
                <button id="view-grid" class="${listView==='grid'?'active':''}">Grade</button>
            </div>
        </div>
        <div id="bulk-bar" class="bulk-bar hidden">
            <span><span id="bulk-count">0</span> selecionados</span>
            <button id="bulk-assign">Atribuir a operador…</button>
            <button id="bulk-reset">Resetar</button>
            <button id="bulk-rereview">Re-revisar</button>
            <button id="bulk-report-problem">Reportar problema…</button>
            <button id="bulk-block">Bloquear</button>
            <button id="bulk-unblock">Desbloquear</button>
            <button id="bulk-clear">Limpar seleção</button>
        </div>
        <div id="pager-top" class="pager"></div>
        <div id="tiles-list"></div>
        <div id="pager-bottom" class="pager"></div>
    `;
    document.getElementById("view-table").addEventListener("click", () => { listView = "table"; loadAndRender(); });
    document.getElementById("view-grid").addEventListener("click", () => { listView = "grid"; loadAndRender(); });
    document.getElementById("btn-filter").addEventListener("click", () => { page = 0; loadAndRender(); });
    const filterQ = document.getElementById("filter-q");
    let qTimer;
    filterQ.addEventListener("input", () => {
        clearTimeout(qTimer);
        qTimer = setTimeout(() => { page = 0; loadAndRender(); }, 250);
    });
    filterQ.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { clearTimeout(qTimer); page = 0; loadAndRender(); }
    });
    document.getElementById("bulk-assign").addEventListener("click", bulkAssign);
    document.getElementById("bulk-reset").addEventListener("click", bulkReset);
    document.getElementById("bulk-rereview").addEventListener("click", bulkReReview);
    document.getElementById("bulk-report-problem").addEventListener("click", bulkReportProblem);
    document.getElementById("bulk-block").addEventListener("click", bulkBlock);
    document.getElementById("bulk-unblock").addEventListener("click", bulkUnblock);
    document.getElementById("bulk-clear").addEventListener("click", () => {
        selectedIds.clear();
        const target = document.getElementById("tiles-list");
        target.querySelectorAll(".selected").forEach(el => el.classList.remove("selected"));
        target.querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = false; });
        updateBulkBar();
    });
    await loadAndRender();
}

async function loadAndRender() {
    const status = document.getElementById("filter-status").value;
    const df = document.getElementById("filter-from").value;
    const dt = document.getElementById("filter-to").value;
    const q = document.getElementById("filter-q")?.value.trim();
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (df) params.set("date_from", df);
    if (dt) params.set("date_to", dt);
    if (q) params.set("q", q);
    params.set("limit", PAGE_SIZE);
    params.set("offset", page * PAGE_SIZE);
    const target = document.getElementById("tiles-list");
    target.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando tiles...</div>`;
    const { json: tiles, headers: h } = await apiGetWithHeaders(`/api/admin/tiles?${params}`);
    totalTiles = Number(h.get("X-Total-Count") || tiles.length);
    document.querySelectorAll(".view-mode-toggle button").forEach(b => {
        b.classList.toggle("active", b.id === `view-${listView}`);
    });
    target.innerHTML = "";
    const sorted = sortTiles(tiles);
    if (listView === "table") renderTable(target, sorted);
    else renderGrid(target, sorted);
    updateBulkBar();
    renderPager();
}

function sortTiles(tiles) {
    return [...tiles].sort((a, b) => {
        const va = a[sortKey] ?? "", vb = b[sortKey] ?? "";
        if (va < vb) return sortDir === "asc" ? -1 : 1;
        if (va > vb) return sortDir === "asc" ? 1 : -1;
        return 0;
    });
}

function setSort(key) {
    if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
    else { sortKey = key; sortDir = "asc"; }
    loadAndRender();
}

function renderPager() {
    for (const id of ["pager-top", "pager-bottom"]) {
        const el = document.getElementById(id);
        if (el) buildPagerInto(el);
    }
}

// Compact page-number window: always show 1 and last; show current ± 1;
// insert "…" placeholders where there are gaps. Returns an array of either
// page numbers (0-indexed) or the literal string "…".
function pageWindow(current, totalPages) {
    if (totalPages <= 7) {
        return Array.from({ length: totalPages }, (_, i) => i);
    }
    const set = new Set([0, totalPages - 1, current]);
    if (current - 1 >= 0) set.add(current - 1);
    if (current + 1 <= totalPages - 1) set.add(current + 1);
    // Pad the ends so the window doesn't shrink at the edges.
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

function buildPagerInto(el) {
    el.innerHTML = "";
    const totalPages = Math.max(1, Math.ceil(totalTiles / PAGE_SIZE));
    const goTo = (p) => {
        const clamped = Math.max(0, Math.min(totalPages - 1, p));
        if (clamped === page) return;
        page = clamped;
        loadAndRender();
    };
    const navBtn = (label, disabled, onClick, title) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pager-btn";
        b.textContent = label;
        b.disabled = disabled;
        if (title) b.title = title;
        b.addEventListener("click", onClick);
        return b;
    };

    el.append(
        navBtn("«", page === 0, () => goTo(0), "Primeira página"),
        navBtn("←", page === 0, () => goTo(page - 1), "Página anterior"),
    );

    for (const item of pageWindow(page, totalPages)) {
        if (item === "…") {
            const span = document.createElement("span");
            span.className = "pager-ellipsis";
            span.textContent = "…";
            el.appendChild(span);
        } else {
            const b = navBtn(String(item + 1), false, () => goTo(item));
            b.classList.add("pager-num");
            if (item === page) b.classList.add("active");
            el.appendChild(b);
        }
    }

    el.append(
        navBtn("→", page >= totalPages - 1, () => goTo(page + 1), "Próxima página"),
        navBtn("»", page >= totalPages - 1, () => goTo(totalPages - 1), "Última página"),
    );

    const info = document.createElement("span");
    info.className = "pager-info";
    const start = page * PAGE_SIZE + 1;
    const end = Math.min(totalTiles, (page + 1) * PAGE_SIZE);
    info.textContent = totalTiles === 0
        ? "Nenhum tile"
        : `${start}–${end} de ${totalTiles}`;
    el.appendChild(info);
}

function updateBulkBar() {
    const bar = document.getElementById("bulk-bar");
    document.getElementById("bulk-count").textContent = selectedIds.size;
    bar.classList.toggle("hidden", selectedIds.size === 0);
}

function buildTableRow(t) {
    const tr = document.createElement("tr");
    tr.dataset.id = t.id;
    if (selectedIds.has(t.id)) tr.classList.add("selected");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = selectedIds.has(t.id);
    cb.addEventListener("change", () => toggleSelect(t.id, cb.checked, tr));
    const tdCb = document.createElement("td"); tdCb.appendChild(cb);
    const tdStatus = document.createElement("td");
    const chip = document.createElement("span");
    if (t.paused_at) {
        chip.className = "chip paused";
        chip.textContent = t.status === "in_review" ? "pausado (revisão)" : "pausado";
        chip.title = `Pausado em ${t.paused_at}`;
    } else {
        chip.className = `chip ${t.status}`;
        chip.textContent = t.status;
        if (t.status === "blocked" && t.blocked_from) chip.title = `Antes: ${t.blocked_from}`;
    }
    tdStatus.appendChild(chip);
    const tdAct = document.createElement("td");
    tdAct.append(btn("Ver", () => openViewer(t.id)));
    if (t.status === "reviewed") tdAct.append(btn("Re-revisar", () => reReviewOne(t.id)));
    if (t.status === "pending" || t.status === "classified") {
        tdAct.append(btn("Atribuir", () => assignOne(t)));
    }
    if (t.status === "in_progress" || t.status === "in_review") {
        if (!t.paused_at) {
            tdAct.append(btn("Pausar", () => adminPauseOne(t.id)));
        }
        tdAct.append(btn("Liberar operador", () => unassignOne(t.id)));
    }
    if (isBlockable(t)) {
        tdAct.append(btn("Bloquear", () => blockAction([t.id])));
    }
    if (t.status === "blocked") {
        tdAct.append(btn("Desbloquear", () => blockAction([t.id], { unblock: true })));
    }
    tr.append(tdCb, td(t.id), td(t.name), tdStatus,
              td(classifierCell(t)), td(reviewerCell(t)),
              td(finishedCell(t.classified_at, t.status === "in_progress")),
              td(finishedCell(t.reviewed_at, t.status === "in_review")),
              tdAct);
    return tr;
}

function renderTable(root, tiles) {
    const wrap = document.createElement("div");
    wrap.className = "admin-table-wrap";
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    const sortArrow = (k) => sortKey === k ? (sortDir === "asc" ? " ▲" : " ▼") : "";
    thead.innerHTML = `<tr><th><input type="checkbox" id="check-all"></th>
        <th data-sort="id">ID${sortArrow("id")}</th>
        <th data-sort="name">Nome${sortArrow("name")}</th>
        <th data-sort="status">Status${sortArrow("status")}</th>
        <th data-sort="classified_by_username">Classificado por${sortArrow("classified_by_username")}</th>
        <th data-sort="reviewed_by_username">Revisado por${sortArrow("reviewed_by_username")}</th>
        <th data-sort="classified_at">Classificado${sortArrow("classified_at")}</th>
        <th data-sort="reviewed_at">Revisado${sortArrow("reviewed_at")}</th>
        <th>Ações</th></tr>`;
    thead.querySelectorAll("th[data-sort]").forEach(th => {
        th.style.cursor = "pointer";
        th.addEventListener("click", () => setSort(th.dataset.sort));
    });
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const t of tiles) tbody.appendChild(buildTableRow(t));
    table.appendChild(tbody);
    wrap.appendChild(table);
    root.appendChild(wrap);
    document.getElementById("check-all").addEventListener("change", (ev) => {
        tbody.querySelectorAll("tr").forEach(tr => {
            const id = Number(tr.dataset.id);
            if (ev.target.checked) selectedIds.add(id); else selectedIds.delete(id);
            tr.classList.toggle("selected", ev.target.checked);
            tr.querySelector("input[type=checkbox]").checked = ev.target.checked;
        });
        updateBulkBar();
    });
}

function buildGridCard(t) {
    const card = document.createElement("div");
    card.className = "thumb-card" + (selectedIds.has(t.id) ? " selected" : "");
    card.dataset.id = t.id;
    const img = document.createElement("img");
    img.alt = `Tile ${t.id}`;
    img.className = "thumb-skeleton";
    // Backend picks mask vs satellite based on whether the tile has any
    // painted pixels, so empty masks (pending / problem / freshly-assigned)
    // still show something meaningful without frontend branching.
    apiGetBlob(`/api/admin/tiles/${t.id}/thumbnail?size=128`)
        .then(b => { img.src = URL.createObjectURL(b); img.classList.remove("thumb-skeleton"); })
        .catch(() => { img.alt = "?"; img.classList.remove("thumb-skeleton"); });
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `#${t.id} · ${t.status}`;
    const whoText =
        t.reviewed_by_username ? `revisado por ${t.reviewed_by_username}` :
        t.classified_by_username ? `classificado por ${t.classified_by_username}` :
        t.assigned_to_username ? `atribuído a ${t.assigned_to_username}` : "";
    card.append(img, meta);
    if (whoText) {
        const who = document.createElement("div");
        who.className = "meta-dim";
        who.textContent = whoText;
        card.append(who);
    }
    if (t.status === "pending" || t.status === "classified") {
        const assignBtn = document.createElement("button");
        assignBtn.className = "card-action";
        assignBtn.textContent = t.status === "classified" ? "Atribuir revisor" : "Atribuir";
        assignBtn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            assignOne(t);
        });
        card.appendChild(assignBtn);
    }
    if (isBlockable(t)) {
        card.appendChild(btn("Bloquear", () => blockAction([t.id]), "card-action"));
    } else if (t.status === "blocked") {
        card.appendChild(btn("Desbloquear", () => blockAction([t.id], { unblock: true }), "card-action"));
    }
    card.addEventListener("click", (ev) => {
        if (ev.shiftKey) toggleSelect(t.id, !selectedIds.has(t.id), card);
        else openViewer(t.id);
    });
    return card;
}

function renderGrid(root, tiles) {
    const grid = document.createElement("div");
    grid.className = "thumb-grid";
    for (const t of tiles) grid.appendChild(buildGridCard(t));
    root.appendChild(grid);
}

// In-place swap of a row/card for one tile — avoids the full list reload on
// single-tile actions (assign, reset, unassign, block…). Silent no-op if the
// row isn't currently rendered (e.g. user changed filters meanwhile).
async function refreshTileInPlace(id) {
    try {
        const fresh = await apiGet(`/api/tiles/${id}`);
        if (listView === "table") {
            const oldTr = document.querySelector(`tr[data-id="${id}"]`);
            if (oldTr) oldTr.replaceWith(buildTableRow(fresh));
        } else {
            const oldCard = document.querySelector(`.thumb-card[data-id="${id}"]`);
            if (oldCard) oldCard.replaceWith(buildGridCard(fresh));
        }
    } catch {
        // Fallback: if the single-tile fetch fails, fall back to the full reload
        // so the UI doesn't get stuck showing stale state.
        loadAndRender();
    }
}

async function refreshTilesInPlace(ids) {
    await Promise.all(ids.map(refreshTileInPlace));
}

function toggleSelect(id, on, el) {
    if (on) selectedIds.add(id); else selectedIds.delete(id);
    el?.classList.toggle("selected", on);
    updateBulkBar();
}

function td(v) { const el = document.createElement("td"); el.textContent = v ?? ""; return el; }

function classifierCell(t) {
    return t.classified_by_username
        || (t.status === "in_progress" ? (t.assigned_to_username || "") : "");
}

function reviewerCell(t) {
    return t.reviewed_by_username
        || (t.status === "in_review" ? (t.assigned_to_username || "") : "");
}

function finishedCell(timestamp, inProgress) {
    if (timestamp) return `✓ ${fmtDate(timestamp)}`;
    if (inProgress) return "em andamento";
    return "";
}
function btn(label, fn, cls) {
    const b = document.createElement("button"); b.textContent = label;
    if (cls) b.className = cls;
    b.style.marginRight = "4px";
    b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
    return b;
}

// ---------- Custom confirm modal for destructive bulk ops ----------
function wireConfirmModal() {
    const modal = document.getElementById("modal-confirm");
    if (!modal) return;
    const reason = document.getElementById("confirm-reason");
    if (reason) reason.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            document.getElementById("confirm-ok")?.click();
        }
    });
}

function confirmDestructive({
    title, description, ids, confirmLabel = "Confirmar", danger = true,
    reasonLabel, reasonPlaceholder, reasonRequired = false, reasonMaxLength = 500,
}) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-confirm");
        document.getElementById("confirm-title").textContent = title;
        document.getElementById("confirm-description").textContent = description;
        const list = document.getElementById("confirm-ids");
        list.innerHTML = "";
        const preview = ids.slice(0, 20);
        list.textContent = `IDs: ${preview.join(", ")}${ids.length > 20 ? ` … (+${ids.length - 20})` : ""}`;
        const reason = document.getElementById("confirm-reason");
        const reasonLabelEl = modal.querySelector(".confirm-reason-label");
        // Remember original label/placeholder/maxlength so we can restore them
        // on close — other callers share this modal and expect the defaults.
        const origLabelText = reasonLabelEl?.firstChild?.nodeValue;
        const origPlaceholder = reason.placeholder;
        const origMaxLength = reason.maxLength;
        if (reasonLabel && reasonLabelEl?.firstChild) reasonLabelEl.firstChild.nodeValue = reasonLabel;
        if (reasonPlaceholder !== undefined) reason.placeholder = reasonPlaceholder;
        if (reasonMaxLength) reason.maxLength = reasonMaxLength;
        reason.value = "";
        const ok = document.getElementById("confirm-ok");
        ok.textContent = confirmLabel;
        ok.classList.toggle("danger", !!danger);
        const cancel = document.getElementById("confirm-cancel");
        const updateOkState = () => {
            ok.disabled = reasonRequired && !reason.value.trim();
        };
        updateOkState();

        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            reason.removeEventListener("input", updateOkState);
            ok.disabled = false;
            if (origLabelText && reasonLabelEl?.firstChild) reasonLabelEl.firstChild.nodeValue = origLabelText;
            reason.placeholder = origPlaceholder;
            reason.maxLength = origMaxLength;
            resolve(result);
        };
        const onOk = () => {
            if (reasonRequired && !reason.value.trim()) return;
            cleanup({ confirmed: true, reason: reason.value.trim() });
        };
        const onCancel = () => cleanup({ confirmed: false });
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        reason.addEventListener("input", updateOkState);
        modal.classList.remove("hidden");
        setTimeout(() => reason.focus(), 50);
    });
}

async function resetOne(id) {
    const r = await confirmDestructive({
        title: `Resetar tile #${id}`,
        description: "Esta ação é irreversível. Toda a classificação deste tile será perdida e voltará para pendente.",
        ids: [id], confirmLabel: "Resetar",
    });
    if (!r.confirmed) return;
    await apiPostJson(`/api/admin/tiles/${id}/reset`, { reason: r.reason });
    showToast("Resetado.", "success");
    refreshTileInPlace(id);
}
async function assignOne(tile) {
    const users = await apiGet("/api/admin/users");
    const isReview = tile.status === "classified";
    const eligible = users.filter(u =>
        u.role === "operator" && u.active &&
        (!isReview || (u.can_review && u.id !== tile.classified_by))
    );
    if (!eligible.length) {
        showToast(isReview
            ? "Sem revisores habilitados (ou todos classificaram este tile)."
            : "Sem operadores ativos.", "error");
        return;
    }
    const r = await promptAssign({
        title: isReview ? `Atribuir revisão do tile #${tile.id}` : `Atribuir tile #${tile.id}`,
        description: isReview
            ? "Escolha um revisor. Revisores precisam estar habilitados e não podem revisar a própria classificação."
            : "Escolha um operador para classificar este tile.",
        users: eligible,
    });
    if (!r.confirmed) return;
    await apiPostJson(`/api/admin/tiles/${tile.id}/assign`, {
        user_id: r.user_id, reason: r.reason || null,
    });
    showToast("Tile atribuído.", "success");
    refreshTileInPlace(tile.id);
}

function promptAssign({ title, description, users }) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-assign");
        document.getElementById("assign-title").textContent = title;
        document.getElementById("assign-description").textContent = description;
        const sel = document.getElementById("assign-user");
        sel.innerHTML = "";
        for (const u of users) {
            const opt = document.createElement("option");
            opt.value = String(u.id);
            opt.textContent = u.username + (u.can_review ? " (revisor)" : "");
            sel.appendChild(opt);
        }
        const reason = document.getElementById("assign-reason"); reason.value = "";
        const ok = document.getElementById("assign-ok");
        const cancel = document.getElementById("assign-cancel");
        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            resolve(result);
        };
        const onOk = () => cleanup({ confirmed: true, user_id: Number(sel.value), reason: reason.value.trim() });
        const onCancel = () => cleanup({ confirmed: false });
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        modal.classList.remove("hidden");
        setTimeout(() => sel.focus(), 50);
    });
}

async function deleteOne(id, name) {
    const r = await confirmDestructive({
        title: `Excluir tile #${id}`,
        description: `Ação irreversível. O tile "${name}" e todo o seu histórico serão apagados permanentemente.`,
        ids: [id], confirmLabel: "Excluir permanentemente",
    });
    if (!r.confirmed) return;
    await apiJson(`/api/admin/tiles/${id}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: r.reason }),
    });
    showToast("Tile excluído.", "success");
    selectTab("problems");
}

async function unassignOne(id) {
    const r = await confirmDestructive({
        title: `Liberar operador do tile #${id}`,
        description: "O operador atual é removido e o tile volta para a fila. A máscara já pintada é preservada.",
        ids: [id], confirmLabel: "Liberar", danger: false,
    });
    if (!r.confirmed) return;
    await apiPostJson(`/api/admin/tiles/${id}/unassign`, { reason: r.reason });
    showToast("Operador liberado.", "success");
    refreshTileInPlace(id);
}
async function adminPauseOne(id) {
    const r = await confirmDestructive({
        title: `Pausar tile #${id}`,
        description: "Congela o cronômetro de classificação sem liberar o operador. Útil quando o operador foi embora sem pausar. Ele retoma o tile quando voltar.",
        ids: [id], confirmLabel: "Pausar", danger: false,
    });
    if (!r.confirmed) return;
    await apiPostJson(`/api/admin/tiles/${id}/admin-pause`, { reason: r.reason });
    showToast("Tile pausado.", "success");
    refreshTileInPlace(id);
}
async function reReviewOne(id) {
    const r = await confirmDestructive({
        title: `Enviar tile #${id} para nova revisão`,
        description: "O tile voltará ao status 'classified' e aparecerá novamente na fila de revisão.",
        ids: [id], confirmLabel: "Re-revisar", danger: false,
    });
    if (!r.confirmed) return;
    await apiPostJson(`/api/admin/tiles/${id}/re-review`, { reason: r.reason });
    showToast("Enviado para nova revisão.", "success");
    refreshTileInPlace(id);
}
// Unified block/unblock for single-id and multi-id batches. Returns true on
// success so viewer callers can skip the default in-place refresh (reload=false).
async function blockAction(ids, { unblock = false, reload = true } = {}) {
    if (!ids.length) return false;
    const verb = unblock ? "Desbloquear" : "Bloquear";
    const one = ids.length === 1;
    const title = one ? `${verb} tile #${ids[0]}` : `${verb} ${ids.length} tile(s)`;
    const description = unblock
        ? "Cada tile volta ao status anterior ao bloqueio. Tiles não bloqueados serão rejeitados."
        : "O tile deixa de ser distribuído até ser desbloqueado. A classificação atual é preservada. Tiles em execução (in_progress/in_review) ou em problema são rejeitados.";
    const r = await confirmDestructive({ title, description, ids, confirmLabel: verb, danger: false });
    if (!r.confirmed) return false;
    const resp = await apiPostJson(
        `/api/admin/tiles/bulk/${unblock ? "unblock" : "block"}`,
        { ids, reason: r.reason },
    );
    showToast(`${resp.affected} ${unblock ? "desbloqueado(s)" : "bloqueado(s)"}.`, "success");
    if (reload) refreshTilesInPlace(ids);
    return true;
}

async function bulkBlock() {
    if (!selectedIds.size) return;
    if (await blockAction([...selectedIds])) selectedIds.clear();
}

async function bulkUnblock() {
    if (!selectedIds.size) return;
    if (await blockAction([...selectedIds], { unblock: true })) selectedIds.clear();
}

async function bulkReset() {
    if (selectedIds.size === 0) return;
    const ids = [...selectedIds];
    const r = await confirmDestructive({
        title: `Resetar ${ids.length} tiles`,
        description: "Esta ação é irreversível. Toda a classificação destes tiles será perdida.",
        ids, confirmLabel: `Resetar ${ids.length} tiles`,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/reset", { ids, reason: r.reason });
    showToast(`${resp.affected} resetados.`, "success");
    selectedIds.clear();
    updateBulkBar();
    refreshTilesInPlace(ids);
}
async function bulkReReview() {
    if (selectedIds.size === 0) return;
    const ids = [...selectedIds];
    const r = await confirmDestructive({
        title: `Re-revisar ${ids.length} tiles`,
        description: "Os tiles revisados voltarão ao status 'classified' e reaparecerão na fila de revisão.",
        ids, confirmLabel: `Enviar ${ids.length} para revisão`, danger: false,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/re-review", { ids, reason: r.reason });
    showToast(`${resp.affected} enviados.`, "success");
    selectedIds.clear();
    updateBulkBar();
    refreshTilesInPlace(ids);
}

async function bulkReportProblem() {
    if (selectedIds.size === 0) return;
    const ids = [...selectedIds];
    const r = await confirmDestructive({
        title: `Reportar problema em ${ids.length} tile(s)`,
        description: "Os tiles selecionados vão para o status 'problem' com a nota abaixo. A máscara é apagada e a atribuição liberada.",
        ids, confirmLabel: `Reportar ${ids.length}`, danger: true,
        reasonLabel: "Descrição do problema (obrigatória):",
        reasonPlaceholder: "Ex: imagem com nuvem, bbox incorreta, tile fora da área de interesse...",
        reasonRequired: true, reasonMaxLength: 2000,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/report-problem", {
        ids, note: r.reason,
    });
    showToast(`${resp.affected} tile(s) reportados.`, "success");
    selectedIds.clear();
    updateBulkBar();
    refreshTilesInPlace(ids);
}

async function bulkAssign() {
    if (selectedIds.size === 0) return;
    const ids = [...selectedIds];
    const [users, tileRows] = await Promise.all([
        apiGet("/api/admin/users"),
        Promise.all(ids.map(id => apiGet(`/api/tiles/${id}`))),
    ]);
    // Backend rejects any tile that isn't pending or classified — filter up
    // front so we can build the eligible-reviewers list and give a clean error.
    const bad = tileRows.filter(t => t.status !== "pending" && t.status !== "classified");
    if (bad.length) {
        showToast(
            `Seleção inválida: ${bad.length} tile(s) não estão em pending/classified.`,
            "error",
        );
        return;
    }
    const hasReview = tileRows.some(t => t.status === "classified");
    const classifierIds = new Set(
        tileRows.filter(t => t.status === "classified" && t.classified_by)
                .map(t => t.classified_by),
    );
    const eligible = users.filter(u =>
        u.role === "operator" && u.active &&
        (!hasReview || (u.can_review && !classifierIds.has(u.id)))
    );
    if (!eligible.length) {
        showToast(hasReview
            ? "Sem revisores habilitados (ou todos já classificaram algum tile do lote)."
            : "Sem operadores ativos.", "error");
        return;
    }
    const r = await promptAssign({
        title: `Atribuir ${ids.length} tile(s) a um operador`,
        description: hasReview
            ? "Os tiles vão para a fila pessoal do usuário como pausados. Ao terminar o atual, ele recebe o próximo automaticamente. Tiles classified exigem revisor habilitado, e o revisor não pode ter classificado o tile."
            : "Os tiles vão para a fila pessoal do usuário como pausados. Ao terminar o atual, ele recebe o próximo automaticamente.",
        users: eligible,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/assign", {
        tile_ids: ids, user_id: r.user_id, reason: r.reason || null,
    });
    showToast(`${resp.affected} tile(s) atribuídos (pausados).`, "success");
    selectedIds.clear();
    updateBulkBar();
    refreshTilesInPlace(ids);
}

async function openViewer(tileId) {
    const modal = document.getElementById("modal-view-tile");
    const body = document.getElementById("view-tile-body");
    document.getElementById("view-tile-title").textContent = `Tile #${tileId}`;
    body.innerHTML = `
        <div class="loading-text"><span class="loading"></span> Carregando tile...</div>
        <div class="viewer-skeleton"></div>`;
    modal.classList.remove("hidden");
    try {
        const [t, history, blob] = await Promise.all([
            apiGet(`/api/tiles/${tileId}`),
            apiGet(`/api/tiles/${tileId}/history`),
            apiGetBlob(`/api/tiles/${tileId}/image`),
        ]);
        const img = await blobToImage(blob);
        body.innerHTML = "";
        const meta = document.createElement("div");
        const statusLine = document.createElement("p");
        statusLine.innerHTML = `<b>Status:</b> `;
        const chip = document.createElement("span");
        if (t.paused_at) {
            chip.className = "chip paused";
            chip.textContent = t.status === "in_review" ? "pausado (revisão)" : "pausado";
            chip.title = `Pausado em ${t.paused_at}`;
        } else {
            chip.className = `chip ${t.status}`;
            chip.textContent = t.status;
        }
        statusLine.appendChild(chip);
        if (t.status === "blocked" && t.blocked_from) {
            statusLine.append(document.createTextNode(` (antes: ${t.blocked_from})`));
        }
        if (t.classified_by_username) {
            statusLine.append(document.createTextNode(" · classificado por "));
            const b = document.createElement("b"); b.textContent = t.classified_by_username;
            statusLine.append(b);
        }
        meta.appendChild(statusLine);
        const viewerActions = document.createElement("div");
        viewerActions.className = "viewer-actions";
        if (t.status === "pending" || t.status === "classified") {
            viewerActions.appendChild(btn(
                t.status === "classified" ? "Atribuir revisor" : "Atribuir operador",
                () => assignOne(t),
            ));
        }
        if (t.status === "reviewed") {
            viewerActions.appendChild(btn("Re-revisar", async () => {
                await reReviewOne(t.id);
                openViewer(t.id);
            }));
        }
        if (isBlockable(t)) {
            viewerActions.appendChild(btn("Bloquear", async () => {
                if (await blockAction([t.id], { reload: false })) openViewer(t.id);
            }));
        } else if (t.status === "blocked") {
            viewerActions.appendChild(btn("Desbloquear", async () => {
                if (await blockAction([t.id], { unblock: true, reload: false })) openViewer(t.id);
            }));
        }
        if (viewerActions.children.length) meta.appendChild(viewerActions);
        body.appendChild(meta);

        if (history.length) {
            const h = document.createElement("details");
            h.open = true;
            const summary = document.createElement("summary");
            summary.textContent = `Histórico (${history.length} ações)`;
            summary.style.cursor = "pointer";
            h.appendChild(summary);
            const ul = document.createElement("ul");
            ul.className = "history-list";
            for (const ev of history) {
                const li = document.createElement("li");
                const act = document.createElement("span"); act.className = "act"; act.textContent = ev.action;
                const by = document.createTextNode(`por ${ev.username || "?"}`);
                const when = document.createElement("div"); when.className = "when"; when.textContent = fmtDate(ev.created_at);
                li.append(act, by);
                if (ev.detail) li.append(document.createTextNode(` — ${ev.detail}`));
                li.append(when);
                ul.appendChild(li);
            }
            h.appendChild(ul);
            body.appendChild(h);
        }
        const stack = document.createElement("div");
        stack.className = "viewer-stack";
        const mapDiv = document.createElement("div");
        mapDiv.id = `viewer-map-${tileId}`;
        const maskCnv = document.createElement("canvas");
        maskCnv.width = 512; maskCnv.height = 512;
        stack.append(mapDiv, maskCnv);
        body.appendChild(stack);
        setTimeout(() => {
            createLockedMap(`viewer-map-${tileId}`, tileserverUrl,
                [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north], tileserverMaxZoom);
        }, 0);
        const tmp = document.createElement("canvas");
        tmp.width = 256; tmp.height = 256;
        const tctx = tmp.getContext("2d");
        tctx.drawImage(img, 0, 0, 256, 256);
        const data = tctx.getImageData(0, 0, 256, 256);
        const out = maskCnv.getContext("2d").createImageData(256, 256);
        for (let i = 0; i < 256*256; i++) {
            const v = data.data[i*4];
            const c = classesById[v];
            if (c) {
                const [r,g,b] = hexToRgb(c.color);
                out.data[i*4] = r; out.data[i*4+1] = g; out.data[i*4+2] = b; out.data[i*4+3] = 180;
            } else { out.data[i*4+3] = 0; }
        }
        const off = document.createElement("canvas"); off.width = 256; off.height = 256;
        off.getContext("2d").putImageData(out, 0, 0);
        const mctx = maskCnv.getContext("2d");
        mctx.imageSmoothingEnabled = false;
        mctx.drawImage(off, 0, 0, 512, 512);
    } catch (e) {
        renderError(body, e);
    }
}


// Status → fill color for the map polygons. Kept in sync with the chip colors
// in style.css so the map legend matches the tiles-tab status chips.
const MAP_STATUS_COLORS = {
    pending:     "#9ca3af",
    in_progress: "#3b82f6",
    classified:  "#eab308",
    in_review:   "#f97316",
    reviewed:    "#22c55e",
    problem:     "#ef4444",
    blocked:     "#4b5563",
};

async function renderMap(root) {
    const tiles = await apiGet("/api/admin/tiles/map");
    mapSelectedIds = new Set();
    mapTilePropsById = new Map();
    mapRectSelectActive = false;
    root.innerHTML = `
        <div class="map-tab">
            <div class="map-tab-toolbar" id="map-toolbar">
                <button id="map-tool-rect" class="map-tool-btn" type="button"
                        title="Arrastar para selecionar. Shift = adicionar; Alt = remover; Esc = sair.">
                    <span class="map-tool-icon">▭</span> Seleção retangular
                </button>
                <span id="map-sel-info" class="map-sel-info hidden">
                    <b id="map-sel-count">0</b> tile(s) selecionado(s)
                </span>
                <span class="map-toolbar-spacer"></span>
                <button id="map-bulk-assign" type="button" disabled>Atribuir operador</button>
                <button id="map-bulk-rereview" type="button" disabled>Re-revisar selecionados</button>
                <button id="map-sel-clear" type="button" disabled>Limpar seleção</button>
            </div>
            <div class="map-tab-legend" id="map-legend"></div>
            <div class="map-tab-container" id="admin-map">
                <div id="map-rect-overlay" class="map-rect-overlay hidden"></div>
            </div>
        </div>
    `;
    // All statuses enabled by default. Clicking a legend item toggles — the
    // filter on the three tile layers is recomputed from this set.
    const enabledStatuses = new Set(Object.keys(MAP_STATUS_COLORS));
    const legend = document.getElementById("map-legend");
    const legendButtons = new Map();
    for (const [status, color] of Object.entries(MAP_STATUS_COLORS)) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "map-legend-item active";
        item.dataset.status = status;
        const sw = document.createElement("span");
        sw.className = "map-legend-swatch";
        sw.style.background = color;
        const lbl = document.createElement("span");
        lbl.textContent = status;
        item.append(sw, lbl);
        item.addEventListener("click", () => toggleStatus(status));
        legend.appendChild(item);
        legendButtons.set(status, item);
    }

    const applyFilter = () => {
        if (!mapView) return;
        const allowed = [...enabledStatuses];
        const filter = ["in", ["get", "status"], ["literal", allowed]];
        for (const id of ["tiles-fill", "tiles-outline", "tiles-dot"]) {
            mapView.setFilter(id, filter);
        }
    };
    const toggleStatus = (status) => {
        if (enabledStatuses.has(status)) enabledStatuses.delete(status);
        else enabledStatuses.add(status);
        legendButtons.get(status).classList.toggle("active", enabledStatuses.has(status));
        applyFilter();
    };

    if (!tiles.length) {
        document.getElementById("admin-map").innerHTML =
            `<p class="empty-state">Nenhum tile cadastrado.</p>`;
        return;
    }

    const polyFeatures = [];
    const pointFeatures = [];
    for (const t of tiles) {
        const props = {
            id: t.id,
            name: t.name,
            status: t.status,
            paused: !!t.paused_at,
            blocked_from: t.blocked_from,
        };
        mapTilePropsById.set(t.id, props);
        polyFeatures.push({
            type: "Feature",
            properties: props,
            geometry: {
                type: "Polygon",
                coordinates: [[
                    [t.bbox_west, t.bbox_south],
                    [t.bbox_east, t.bbox_south],
                    [t.bbox_east, t.bbox_north],
                    [t.bbox_west, t.bbox_north],
                    [t.bbox_west, t.bbox_south],
                ]],
            },
        });
        pointFeatures.push({
            type: "Feature",
            properties: props,
            geometry: {
                type: "Point",
                coordinates: [
                    (t.bbox_west + t.bbox_east) / 2,
                    (t.bbox_south + t.bbox_north) / 2,
                ],
            },
        });
    }
    const polyGeojson = { type: "FeatureCollection", features: polyFeatures };
    const pointGeojson = { type: "FeatureCollection", features: pointFeatures };
    console.log(`[admin/map] rendering ${tiles.length} tiles`);

    // Overall extent for fitBounds.
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    for (const t of tiles) {
        if (t.bbox_west  < w) w = t.bbox_west;
        if (t.bbox_south < s) s = t.bbox_south;
        if (t.bbox_east  > e) e = t.bbox_east;
        if (t.bbox_north > n) n = t.bbox_north;
    }

    const matchExpr = ["match", ["get", "status"]];
    for (const [status, color] of Object.entries(MAP_STATUS_COLORS)) {
        matchExpr.push(status, color);
    }
    matchExpr.push("#888");

    mapView = new maplibregl.Map({
        container: "admin-map",
        style: {
            version: 8,
            sources: {
                basemap: {
                    type: "raster",
                    tiles: [
                        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
                    ],
                    tileSize: 256,
                    attribution: "Tiles © Esri",
                    maxzoom: 19,
                },
                tiles: { type: "geojson", data: polyGeojson },
                "tile-points": { type: "geojson", data: pointGeojson },
            },
            layers: [
                { id: "basemap-layer", type: "raster", source: "basemap" },
                {
                    id: "tiles-fill", type: "fill", source: "tiles",
                    paint: { "fill-color": matchExpr, "fill-opacity": 0.55 },
                },
                {
                    id: "tiles-outline", type: "line", source: "tiles",
                    paint: { "line-color": matchExpr, "line-width": 2 },
                },
                {
                    // Circle marker per tile so they stay visible when the
                    // polygon is sub-pixel at wide zoom levels.
                    id: "tiles-dot", type: "circle", source: "tile-points",
                    paint: {
                        "circle-color": matchExpr,
                        "circle-radius": 5,
                        "circle-stroke-color": "#111",
                        "circle-stroke-width": 1,
                    },
                },
                {
                    // Selection highlight: thick white outline. Filter is
                    // updated by updateMapSelectionHighlight().
                    id: "tiles-selected-outline", type: "line", source: "tiles",
                    paint: {
                        "line-color": "#ffffff",
                        "line-width": 3,
                        "line-opacity": 0.95,
                    },
                    filter: ["in", ["get", "id"], ["literal", []]],
                },
                {
                    id: "tiles-selected-glow", type: "circle", source: "tile-points",
                    paint: {
                        "circle-color": "rgba(0,0,0,0)",
                        "circle-radius": 9,
                        "circle-stroke-color": "#ffffff",
                        "circle-stroke-width": 2,
                    },
                    filter: ["in", ["get", "id"], ["literal", []]],
                },
            ],
        },
        bounds: [[w, s], [e, n]],
        fitBoundsOptions: { padding: 40, animate: false, maxZoom: 14 },
    });
    mapView.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    for (const layer of ["tiles-fill", "tiles-dot"]) {
        mapView.on("mouseenter", layer, () => {
            if (mapRectSelectActive) return;
            mapView.getCanvas().style.cursor = "pointer";
        });
        mapView.on("mouseleave", layer, () => {
            if (mapRectSelectActive) return;
            mapView.getCanvas().style.cursor = "";
        });
        mapView.on("click", layer, (ev) => {
            if (mapRectSelectActive) return;  // suppress viewer while selecting
            const f = ev.features && ev.features[0];
            if (!f) return;
            openViewer(f.properties.id);
        });
    }

    wireMapSelectionTools();
}

// ---------- Map tab: rectangle selection + bulk actions ----------

function wireMapSelectionTools() {
    const toolBtn = document.getElementById("map-tool-rect");
    const clearBtn = document.getElementById("map-sel-clear");
    const reReviewBtn = document.getElementById("map-bulk-rereview");
    const container = document.getElementById("admin-map");
    const overlay = document.getElementById("map-rect-overlay");

    const assignBtn = document.getElementById("map-bulk-assign");
    toolBtn.addEventListener("click", () => setRectSelectActive(!mapRectSelectActive));
    clearBtn.addEventListener("click", () => clearMapSelection());
    reReviewBtn.addEventListener("click", () => mapBulkReReview());
    assignBtn.addEventListener("click", () => mapBulkAssign());

    // Esc clears tool/selection. Listener is attached to document but scoped:
    // it bails if the map tab isn't active anymore.
    const onKey = (ev) => {
        if (currentTab !== "map") return;
        if (ev.key === "Escape") {
            if (mapRectSelectActive) setRectSelectActive(false);
            else if (mapSelectedIds.size) clearMapSelection();
        }
    };
    document.addEventListener("keydown", onKey);
    // When mapView is replaced (tab switch), the old listener becomes a no-op
    // via the currentTab guard, so no explicit cleanup is needed.

    // Drag-to-select. We track on the container in pixel coords, then ask
    // MapLibre for features in the screen bbox at mouseup.
    let start = null;
    let modifier = "replace";  // "replace" | "add" | "remove"

    const containerPoint = (ev) => {
        const r = container.getBoundingClientRect();
        return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    };

    container.addEventListener("mousedown", (ev) => {
        if (!mapRectSelectActive) return;
        if (ev.button !== 0) return;
        ev.preventDefault();
        ev.stopPropagation();
        start = containerPoint(ev);
        modifier = ev.shiftKey ? "add" : (ev.altKey ? "remove" : "replace");
        overlay.classList.remove("hidden");
        overlay.style.left = `${start.x}px`;
        overlay.style.top = `${start.y}px`;
        overlay.style.width = "0px";
        overlay.style.height = "0px";
    });

    window.addEventListener("mousemove", (ev) => {
        if (!start) return;
        const cur = containerPoint(ev);
        const x = Math.min(start.x, cur.x);
        const y = Math.min(start.y, cur.y);
        const w = Math.abs(cur.x - start.x);
        const h = Math.abs(cur.y - start.y);
        overlay.style.left = `${x}px`;
        overlay.style.top = `${y}px`;
        overlay.style.width = `${w}px`;
        overlay.style.height = `${h}px`;
    });

    window.addEventListener("mouseup", (ev) => {
        if (!start) return;
        const end = containerPoint(ev);
        const s = start; start = null;
        overlay.classList.add("hidden");
        if (!mapView) return;

        const dx = Math.abs(end.x - s.x), dy = Math.abs(end.y - s.y);
        // A pure click (tiny drag) inside the tool: treat as single-tile pick
        // at the click point so users don't have to draw a rectangle for one.
        const bbox = (dx < 3 && dy < 3)
            ? [[s.x - 3, s.y - 3], [s.x + 3, s.y + 3]]
            : [[Math.min(s.x, end.x), Math.min(s.y, end.y)],
               [Math.max(s.x, end.x), Math.max(s.y, end.y)]];
        const feats = mapView.queryRenderedFeatures(bbox, {
            layers: ["tiles-fill", "tiles-dot"],
        });
        const hitIds = new Set(feats.map(f => f.properties.id));

        if (modifier === "replace") mapSelectedIds = new Set(hitIds);
        else if (modifier === "add") for (const id of hitIds) mapSelectedIds.add(id);
        else for (const id of hitIds) mapSelectedIds.delete(id);

        updateMapSelectionHighlight();
    });
}

function setRectSelectActive(on) {
    mapRectSelectActive = on;
    const btn = document.getElementById("map-tool-rect");
    if (btn) btn.classList.toggle("active", on);
    if (!mapView) return;
    if (on) {
        mapView.dragPan.disable();
        mapView.boxZoom.disable();
        mapView.doubleClickZoom.disable();
        mapView.getCanvas().style.cursor = "crosshair";
    } else {
        mapView.dragPan.enable();
        mapView.boxZoom.enable();
        mapView.doubleClickZoom.enable();
        mapView.getCanvas().style.cursor = "";
    }
}

function clearMapSelection() {
    mapSelectedIds.clear();
    updateMapSelectionHighlight();
}

function updateMapSelectionHighlight() {
    const ids = [...mapSelectedIds];
    if (mapView) {
        const filter = ["in", ["get", "id"], ["literal", ids]];
        mapView.setFilter("tiles-selected-outline", filter);
        mapView.setFilter("tiles-selected-glow", filter);
    }
    const info = document.getElementById("map-sel-info");
    const count = document.getElementById("map-sel-count");
    const clearBtn = document.getElementById("map-sel-clear");
    const reReviewBtn = document.getElementById("map-bulk-rereview");
    if (count) count.textContent = ids.length;
    if (info) info.classList.toggle("hidden", ids.length === 0);
    const reviewedCount = ids.filter(id => mapTilePropsById.get(id)?.status === "reviewed").length;
    const assignableCount = ids.filter(id => {
        const s = mapTilePropsById.get(id)?.status;
        return s === "pending" || s === "classified";
    }).length;
    const assignBtn = document.getElementById("map-bulk-assign");
    if (clearBtn) clearBtn.disabled = ids.length === 0;
    if (reReviewBtn) {
        reReviewBtn.disabled = reviewedCount === 0;
        reReviewBtn.textContent = reviewedCount
            ? `Re-revisar selecionados (${reviewedCount})`
            : "Re-revisar selecionados";
    }
    if (assignBtn) {
        assignBtn.disabled = assignableCount === 0;
        assignBtn.textContent = assignableCount
            ? `Atribuir operador (${assignableCount})`
            : "Atribuir operador";
    }
}

async function mapBulkReReview() {
    const ids = [...mapSelectedIds];
    if (!ids.length) return;
    // Only reviewed tiles are eligible — backend silently skips others, but
    // we surface the split so admins know what they're confirming.
    const eligible = ids.filter(id => mapTilePropsById.get(id)?.status === "reviewed");
    if (!eligible.length) {
        showToast("Nenhum tile selecionado está em 'reviewed'.", "error");
        return;
    }
    const skipped = ids.length - eligible.length;
    const r = await confirmDestructive({
        title: `Re-revisar ${eligible.length} tile(s)`,
        description: skipped > 0
            ? `Os tiles 'reviewed' voltarão para 'classified' e reaparecerão na fila de revisão. ${skipped} tile(s) selecionado(s) não estão em 'reviewed' e serão ignorados.`
            : "Os tiles voltarão para 'classified' e reaparecerão na fila de revisão.",
        ids: eligible, confirmLabel: `Enviar ${eligible.length} para revisão`, danger: false,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/re-review", {
        ids: eligible, reason: r.reason,
    });
    showToast(`${resp.affected} enviados para nova revisão.`, "success");
    // Refresh the affected features in-place: flip their status to 'classified'
    // in the cached props + geojson sources so the map recolors without a
    // full reload.
    for (const id of eligible) {
        const p = mapTilePropsById.get(id);
        if (p) p.status = "classified";
    }
    if (mapView) {
        const polySrc = mapView.getSource("tiles");
        const ptSrc = mapView.getSource("tile-points");
        for (const src of [polySrc, ptSrc]) {
            if (!src) continue;
            const data = src._data;
            if (!data) continue;
            for (const f of data.features) {
                if (eligible.includes(f.properties.id)) f.properties.status = "classified";
            }
            src.setData(data);
        }
    }
    clearMapSelection();
}

async function mapBulkAssign() {
    const ids = [...mapSelectedIds];
    if (!ids.length) return;
    // Eligible = pending|classified by cached props. Other statuses are
    // ignored (the backend would 409 on the whole batch otherwise).
    const eligibleIds = ids.filter(id => {
        const s = mapTilePropsById.get(id)?.status;
        return s === "pending" || s === "classified";
    });
    if (!eligibleIds.length) {
        showToast("Nenhum tile selecionado está em 'pending' ou 'classified'.", "error");
        return;
    }
    const skipped = ids.length - eligibleIds.length;
    // Need fresh per-tile records: classified tiles carry classified_by which
    // determines reviewer eligibility (a reviewer can't review their own work).
    const [users, tileRows] = await Promise.all([
        apiGet("/api/admin/users"),
        Promise.all(eligibleIds.map(id => apiGet(`/api/tiles/${id}`))),
    ]);
    // Re-check status against fresh data — the cached props snapshot may be
    // stale after other admins acted concurrently.
    const fresh = tileRows.filter(t => t.status === "pending" || t.status === "classified");
    if (!fresh.length) {
        showToast("Os tiles selecionados mudaram de status. Recarregue o mapa.", "error");
        return;
    }
    const hasReview = fresh.some(t => t.status === "classified");
    const classifierIds = new Set(
        fresh.filter(t => t.status === "classified" && t.classified_by)
              .map(t => t.classified_by),
    );
    const eligibleUsers = users.filter(u =>
        u.role === "operator" && u.active &&
        (!hasReview || (u.can_review && !classifierIds.has(u.id)))
    );
    if (!eligibleUsers.length) {
        showToast(hasReview
            ? "Sem revisores habilitados (ou todos já classificaram algum tile do lote)."
            : "Sem operadores ativos.", "error");
        return;
    }
    const r = await promptAssign({
        title: `Atribuir ${fresh.length} tile(s) a um operador`,
        description: (hasReview
            ? "Os tiles vão para a fila pessoal do usuário como pausados. Tiles 'classified' exigem revisor habilitado e o revisor não pode ter classificado o tile."
            : "Os tiles vão para a fila pessoal do usuário como pausados. Ao terminar o atual, ele recebe o próximo automaticamente.")
            + (skipped > 0 ? ` ${skipped} tile(s) selecionado(s) não estão em 'pending'/'classified' e foram ignorados.` : ""),
        users: eligibleUsers,
    });
    if (!r.confirmed) return;
    const assignIds = fresh.map(t => t.id);
    const resp = await apiPostJson("/api/admin/tiles/assign", {
        tile_ids: assignIds, user_id: r.user_id, reason: r.reason || null,
    });
    showToast(`${resp.affected} tile(s) atribuídos (pausados).`, "success");
    // Optimistic recolor: pending -> in_progress, classified -> in_review.
    for (const t of fresh) {
        const newStatus = t.status === "pending" ? "in_progress" : "in_review";
        const p = mapTilePropsById.get(t.id);
        if (p) p.status = newStatus;
    }
    if (mapView) {
        const polySrc = mapView.getSource("tiles");
        const ptSrc = mapView.getSource("tile-points");
        for (const src of [polySrc, ptSrc]) {
            if (!src) continue;
            const data = src._data;
            if (!data) continue;
            for (const f of data.features) {
                const p = mapTilePropsById.get(f.properties.id);
                if (p) f.properties.status = p.status;
            }
            src.setData(data);
        }
    }
    clearMapSelection();
}


async function renderProblems(root) {
    const problems = await apiGet("/api/admin/tiles/problems");
    root.innerHTML = "<h3>Tiles com problema</h3>";
    if (!problems.length) {
        const p = document.createElement("p");
        p.className = "empty-state";
        p.textContent = "Nenhum problema reportado. 🎉";
        root.appendChild(p);
        return;
    }
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>ID</th><th>Nome</th><th>Nota</th><th>Reportado em</th><th>Ações</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const p of problems) {
        const tr = document.createElement("tr");
        const tdAct = document.createElement("td");
        tdAct.append(
            btn("Ver", () => openViewer(p.id)),
            btn("Resetar", () => resetOne(p.id)),
            btn("Excluir", () => deleteOne(p.id, p.name), "danger"),
        );
        tr.append(td(p.id), td(p.name), td(p.problem_note || ""), td(fmtDate(p.reported_at)), tdAct);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);
}

async function renderUsers(root) {
    const users = await apiGet("/api/admin/users");
    root.innerHTML = `
        <h3>Criar usuário</h3>
        <form id="form-user" style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;">
            <input type="text" id="nu-username" placeholder="username" autocomplete="username" required>
            <input type="password" id="nu-password" placeholder="senha (>= 6)" autocomplete="new-password" required minlength="6">
            <select id="nu-role"><option value="operator">operator</option><option value="admin">admin</option></select>
            <button type="submit" class="primary">Criar</button>
        </form>
        <h3>Usuários</h3>
    `;
    document.getElementById("form-user").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const btn = ev.target.querySelector("button[type=submit]");
        if (btn.disabled) return;
        btn.disabled = true;
        try {
            await apiPostJson("/api/admin/users", {
                username: document.getElementById("nu-username").value,
                password: document.getElementById("nu-password").value,
                role: document.getElementById("nu-role").value,
            });
            showToast("Usuário criado.", "success");
            selectTab("users");
        } catch (e) { showToast(`Erro: ${e.message}`, "error"); btn.disabled = false; }
    });
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>ID</th><th>Usuário</th><th>Role</th><th>Ativo</th><th>Revisor</th><th>Criado em</th><th>Ações</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const u of users) {
        const tr = document.createElement("tr");
        const tdAct = document.createElement("td");
        const actLabel = u.active ? "Desativar" : "Ativar";
        tdAct.append(btn(actLabel, () => toggleActive(u.id, !u.active)));
        if (u.role !== "admin") {
            const rvLabel = u.can_review ? "Revogar revisão" : "Permitir revisão";
            tdAct.append(btn(rvLabel, () => toggleCanReview(u.id, !u.can_review)));
        }
        const reviewerCell = u.role === "admin" ? "—" : (u.can_review ? "sim" : "não");
        tr.append(td(u.id), td(u.username), td(u.role),
                  td(u.active ? "sim" : "não"), td(reviewerCell),
                  td(fmtDate(u.created_at)), tdAct);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);
}

async function toggleCanReview(userId, canReview) {
    try {
        await apiJson(`/api/admin/users/${userId}/can-review`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ can_review: canReview }),
        });
        showToast(canReview ? "Revisor habilitado." : "Revisor revogado.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}

async function toggleActive(userId, active) {
    try {
        await apiJson(`/api/admin/users/${userId}/active`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ active }),
        });
        showToast(active ? "Usuário ativado." : "Usuário desativado.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}
