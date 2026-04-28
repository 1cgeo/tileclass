// Admin panel: dashboard, tiles (list+grid+bulk+filters+viewer), problems, users.
// Dashboard tab + shared modals live in ./admin/ submodules; the rest stays
// here because tiles/map/actions/viewer share so much state that splitting
// them costs more readability than it gains.
import { apiGet, apiGetBlob, apiGetWithHeaders, apiPostJson, apiPatchJson, apiJson, logout as apiLogout } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap, disposeMap, tileTransformRequest } from "./maplib.js";
import { hexToRgb, blobToImage, escapeHtml as escape, fmtDate, fmtBytes } from "./utils.js";
import { renderDashboard, fmtDuration } from "./admin/dashboard.js";
import { wireConfirmModal, confirmDestructive, promptAssign } from "./admin/modals.js";

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
// GeoJSON sources held in scope so bulk actions can mutate features and call
// setData(geojson) on the public API instead of reaching into src._data.
let mapPolyGeojson = null;
let mapPointGeojson = null;
let mapRectSelectActive = false;

// Sort + pagination state for the tiles tab
let sortKey = "id";
let sortDir = "asc";
let page = 0;
const PAGE_SIZE = 100;
let totalTiles = 0;

// Active MapLibre instance for the "Mapa" tab, disposed on tab change.
let mapView = null;
// Active MapLibre instance for the tile viewer modal. Disposed on close
// and before each re-open — otherwise WebGL contexts pile up until the
// browser starts evicting them ("Too many active WebGL contexts").
let viewerMap = null;

function disposeViewerMap() { viewerMap = disposeMap(viewerMap); }

// Mirrors backend `_BLOCKABLE_STATES` in admin_service.py. Backend rejects the
// whole batch if any tile is outside this set, so the UI must filter the
// selection before sending — never trust users to know the gate.
const BLOCKABLE_STATES = new Set(["pending", "classified", "reviewed"]);
const isBlockable = t => BLOCKABLE_STATES.has(t.status);

let _adminInitialized = false;

export async function initAdmin(user) {
    document.getElementById("admin-user-label").textContent = `${user.username} (admin)`;
    if (_adminInitialized) {
        await enterAdmin();
        return;
    }
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
    const closeVt = () => {
        vtModal.classList.add("hidden");
        disposeViewerMap();
        // Clear body so any in-flight renderTileInfo's setTimeout sees the
        // mapDiv is gone and bails before creating an orphan WebGL context.
        document.getElementById("view-tile-body").innerHTML = "";
    };
    document.getElementById("view-tile-close").addEventListener("click", closeVt);
    // Click on the backdrop (outside the modal-box) closes the viewer.
    vtModal.addEventListener("click", (ev) => { if (ev.target === vtModal) closeVt(); });
    document.addEventListener("keydown", (ev) => {
        if (document.getElementById("view-admin").classList.contains("hidden")) return;
        if (ev.key === "Escape" && !vtModal.classList.contains("hidden")) closeVt();
    });
    wireConfirmModal();
    _adminInitialized = true;
    await selectTab("dashboard");
}

// Re-render the active tab so dashboards/lists reflect any work the admin
// just did while toggled into the editor view.
export async function enterAdmin() {
    await selectTab(currentTab || "dashboard");
}

async function selectTab(tab) {
    currentTab = tab;
    selectedIds.clear();
    page = 0;
    mapView = disposeMap(mapView);
    // Drop the GeoJSON references when leaving the map tab — these hold one
    // feature per tile and pin tile-row-sized objects in memory while admin
    // sessions stay open.
    mapPolyGeojson = null;
    mapPointGeojson = null;
    disposeViewerMap();
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
        else if (tab === "maintenance") await renderMaintenance(content);
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

async function renderTiles(root) {
    root.innerHTML = `
        <div class="filter-bar">
            <label>Buscar <input type="search" id="filter-q" placeholder="ID ou nome (parcial)" autocomplete="off"></label>
            <label>Status <select id="filter-status">
                <option value="">(todos)</option>
                <option>pending</option><option>in_progress</option>
                <option value="paused">paused</option>
                <option value="paused_review">paused (revisão)</option>
                <option>classified</option>
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
            <button id="bulk-unassign">Liberar operador</button>
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
    document.getElementById("bulk-unassign").addEventListener("click", bulkUnassign);
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
    // "paused" is a virtual status — backend exposes it via the `paused`
    // boolean query param, not as a real status value. Translate here so the
    // filter behaves like any other selection from the user's perspective.
    // For in_progress/in_review we must also send paused=false, otherwise
    // paused tiles (which are technically in_progress|in_review with
    // paused_at != NULL) leak into those buckets and double-count against
    // the "paused" virtual status — same split the dashboard already does.
    if (status === "paused") {
        params.set("paused", "true");
    } else if (status === "paused_review") {
        params.set("status", "in_review");
        params.set("paused", "true");
    } else if (status) {
        params.set("status", status);
        if (status === "in_progress" || status === "in_review") {
            params.set("paused", "false");
        }
    }
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
    // Selection checkbox overlaid top-left. Stops propagation so toggling it
    // doesn't also open the viewer. Shift+click on the card body still works.
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "thumb-check";
    cb.checked = selectedIds.has(t.id);
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", () => toggleSelect(t.id, cb.checked, card));
    card.appendChild(cb);
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
    if (t.status === "blocked") {
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
    // Admins are eligible too — they also classify/review. role==admin
    // implicitly grants review rights (mirrors backend `_user_can_review`).
    const eligible = users.filter(u =>
        u.active &&
        (!isReview || ((u.can_review || u.role === "admin") && u.id !== tile.classified_by))
    );
    if (!eligible.length) {
        showToast(isReview
            ? "Sem revisores habilitados (ou todos classificaram este tile)."
            : "Sem usuários ativos.", "error");
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
        u.active &&
        (!hasReview || ((u.can_review || u.role === "admin") && !classifierIds.has(u.id)))
    );
    if (!eligible.length) {
        showToast(hasReview
            ? "Sem revisores habilitados (ou todos já classificaram algum tile do lote)."
            : "Sem usuários ativos.", "error");
        return;
    }
    const r = await promptAssign({
        title: `Atribuir ${ids.length} tile(s) a um usuário`,
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

// Bulk release: same backend contract as the map-tab `mapBulkUnassign`. The
// tiles table doesn't keep a per-id status cache, so we fetch fresh rows to
// pre-filter — backend `unassign_many` is atomic and 409s the whole batch
// otherwise. Eligible states are in_progress|in_review (paused tiles count —
// `paused_at` is orthogonal to `status`).
async function bulkUnassign() {
    if (selectedIds.size === 0) return;
    const ids = [...selectedIds];
    const tileRows = await Promise.all(ids.map(id => apiGet(`/api/tiles/${id}`)));
    const eligible = tileRows
        .filter(t => t.status === "in_progress" || t.status === "in_review")
        .map(t => t.id);
    if (!eligible.length) {
        showToast("Nenhum tile selecionado tem operador atribuído (estados elegíveis: in_progress, in_review).", "error");
        return;
    }
    const skipped = ids.length - eligible.length;
    const r = await confirmDestructive({
        title: `Liberar operador de ${eligible.length} tile(s)`,
        description: (skipped > 0
            ? `O operador atual é removido e cada tile volta para a fila (in_progress→pending, in_review→classified). A máscara já pintada é preservada. ${skipped} tile(s) selecionado(s) não têm operador atribuído e serão ignorados.`
            : "O operador atual é removido e cada tile volta para a fila (in_progress→pending, in_review→classified). A máscara já pintada é preservada."),
        ids: eligible, confirmLabel: `Liberar ${eligible.length}`, danger: false,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/unassign", {
        ids: eligible, reason: r.reason,
    });
    showToast(`${resp.affected} liberado(s).`, "success");
    selectedIds.clear();
    updateBulkBar();
    refreshTilesInPlace(ids);
}

// Render the tile info (status, actions, history, map+mask preview) into
// `targetBody`. Used by both the modal viewer (Tiles tab) and the side panel
// (Mapa tab). Owns `viewerMap` lifecycle — disposes the previous map and
// installs a new one in the rendered `viewer-map-${tileId}` div. `reload`
// is the function to call when an action button needs to refresh the same
// view (e.g. after Re-revisar / Bloquear).
async function renderTileInfo(targetBody, tileId, reload) {
    disposeViewerMap();
    targetBody.innerHTML = `
        <div class="loading-text"><span class="loading"></span> Carregando tile...</div>
        <div class="viewer-skeleton"></div>`;
    try {
        const [t, history, blob] = await Promise.all([
            apiGet(`/api/tiles/${tileId}`),
            apiGet(`/api/tiles/${tileId}/history`),
            apiGetBlob(`/api/tiles/${tileId}/image`),
        ]);
        const img = await blobToImage(blob);
        targetBody.innerHTML = "";
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
                reload(t.id);
            }));
        }
        if (isBlockable(t)) {
            viewerActions.appendChild(btn("Bloquear", async () => {
                if (await blockAction([t.id], { reload: false })) reload(t.id);
            }));
        } else if (t.status === "blocked") {
            viewerActions.appendChild(btn("Desbloquear", async () => {
                if (await blockAction([t.id], { unblock: true, reload: false })) reload(t.id);
            }));
        }
        if (viewerActions.children.length) meta.appendChild(viewerActions);
        targetBody.appendChild(meta);

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
            targetBody.appendChild(h);
        }
        const stack = document.createElement("div");
        stack.className = "viewer-stack";
        const mapDiv = document.createElement("div");
        mapDiv.id = `viewer-map-${tileId}`;
        const maskCnv = document.createElement("canvas");
        maskCnv.width = 512; maskCnv.height = 512;
        stack.append(mapDiv, maskCnv);
        targetBody.appendChild(stack);
        setTimeout(() => {
            // Bail if the host cleared this body (closed modal / panel, or
            // requested a different tile) before this tick fires — otherwise
            // we'd leak a WebGL context into an orphaned div.
            if (!document.getElementById(`viewer-map-${tileId}`)) return;
            disposeViewerMap();
            viewerMap = createLockedMap(`viewer-map-${tileId}`, tileserverUrl,
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
        renderError(targetBody, e);
    }
}

async function openViewer(tileId) {
    const modal = document.getElementById("modal-view-tile");
    const body = document.getElementById("view-tile-body");
    document.getElementById("view-tile-title").textContent = `Tile #${tileId}`;
    modal.classList.remove("hidden");
    await renderTileInfo(body, tileId, openViewer);
}

const MAP_PANEL_EMPTY_HTML =
    `<p class="map-panel-empty-msg">Clique em um tile no mapa para ver detalhes aqui.</p>`;

function getMapPanelEls() {
    const panel = document.getElementById("map-panel");
    if (!panel) return null;
    return {
        panel,
        title: document.getElementById("map-panel-title"),
        body: document.getElementById("map-panel-body"),
    };
}

async function showTileInPanel(tileId) {
    const els = getMapPanelEls();
    if (!els) return;
    // Dedupe: skip if the panel is already showing this tile. Also catches
    // MapLibre firing the click handler twice for tiles-fill + tiles-dot
    // on a single click in the dot's hitbox.
    if (els.panel.dataset.tileId === String(tileId)) return;
    els.panel.dataset.tileId = String(tileId);
    els.panel.classList.remove("empty");
    els.title.textContent = `Tile #${tileId}`;
    await renderTileInfo(els.body, tileId, showTileInPanel);
}

function closeMapPanel() {
    const els = getMapPanelEls();
    if (!els) return;
    disposeViewerMap();
    delete els.panel.dataset.tileId;
    els.panel.classList.add("empty");
    els.title.textContent = "Detalhes do tile";
    els.body.innerHTML = MAP_PANEL_EMPTY_HTML;
}


// Effective category → fill color for the map polygons. The map keys on
// `display_status`, not raw `status`, so paused tiles (which are technically
// in_progress/in_review with paused_at != NULL) render distinctly. Kept in
// sync with the chip colors in style.css so the map legend matches the
// tiles-tab status chips.
const MAP_STATUS_COLORS = {
    pending:     "#9ca3af",
    in_progress: "#3b82f6",
    paused:      "#a855f7",  // overlay: in_progress|in_review with paused_at != NULL
    classified:  "#eab308",
    in_review:   "#f97316",
    reviewed:    "#22c55e",
    problem:     "#ef4444",
    blocked:     "#4b5563",
};

// Single source of truth for the rendered category on the map. Paused beats
// in_progress/in_review; otherwise the raw status wins.
const displayStatusFor = (p) => (p && p.paused ? "paused" : (p && p.status));

// Apply optimistic status (and optionally paused) to a cached props record
// and recompute display_status. All bulk-action handlers go through this so
// the matchExpr/filter wiring stays consistent.
function setMapTileProps(p, { status, paused, blocked_from } = {}) {
    if (!p) return;
    if (status !== undefined) p.status = status;
    if (paused !== undefined) p.paused = !!paused;
    if (blocked_from !== undefined) p.blocked_from = blocked_from;
    p.display_status = displayStatusFor(p);
}

// Re-sync feature properties from `mapTilePropsById` (the bulk-action source of
// truth) onto both stored GeoJSONs and push to MapLibre. Called after every bulk
// mutation so the map recolors without a full reload.
function refreshMapFeatureProps() {
    if (!mapView) return;
    const sources = [
        [mapPolyGeojson, mapView.getSource("tiles")],
        [mapPointGeojson, mapView.getSource("tile-points")],
    ];
    for (const [geojson, src] of sources) {
        if (!geojson || !src) continue;
        for (const f of geojson.features) {
            const p = mapTilePropsById.get(f.properties.id);
            if (!p) continue;
            f.properties.status = p.status;
            f.properties.paused = p.paused;
            f.properties.display_status = p.display_status;
            f.properties.blocked_from = p.blocked_from;
        }
        src.setData(geojson);
    }
}

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
                <button id="map-tool-sat" class="map-tool-btn" type="button"
                        title="Sobrepõe a imagem de satélite primária sobre o basemap.">
                    <span class="map-tool-icon">🛰️</span> Imagem de satélite
                </button>
                <button id="map-tool-classifs" class="map-tool-btn" type="button"
                        title="Sobrepõe as classificações (classified/in_review/reviewed) sobre o basemap.">
                    <span class="map-tool-icon">🎨</span> Mostrar classificações
                </button>
                <span id="map-sel-info" class="map-sel-info hidden">
                    <b id="map-sel-count">0</b> tile(s) selecionado(s)
                </span>
                <span class="map-toolbar-spacer"></span>
                <button id="map-bulk-assign" type="button" disabled>Atribuir operador</button>
                <button id="map-bulk-unassign" type="button" disabled>Liberar operador</button>
                <button id="map-bulk-rereview" type="button" disabled>Re-revisar selecionados</button>
                <button id="map-bulk-block" type="button" disabled>Bloquear selecionados</button>
                <button id="map-bulk-unblock" type="button" disabled>Desbloquear selecionados</button>
                <button id="map-sel-clear" type="button" disabled>Limpar seleção</button>
            </div>
            <div class="map-tab-legend" id="map-legend"></div>
            <div class="map-tab-main">
                <div class="map-tab-container" id="admin-map">
                    <div id="map-rect-overlay" class="map-rect-overlay hidden"></div>
                    <!-- Floating overlay so toggling the classification layer
                         doesn't push the map down. Sits inside the map
                         container; pointer-events disabled so it never blocks
                         pan/zoom or rectangle-select. -->
                    <div class="map-class-legend-floating hidden" id="map-class-legend"></div>
                </div>
                <aside class="map-panel" id="map-panel">
                    <header class="map-panel-header">
                        <h3 id="map-panel-title"></h3>
                        <button id="map-panel-close" class="map-panel-close" type="button"
                                title="Fechar painel" aria-label="Fechar">×</button>
                    </header>
                    <div class="map-panel-body" id="map-panel-body"></div>
                </aside>
            </div>
        </div>
    `;
    document.getElementById("map-panel-close").addEventListener("click", closeMapPanel);
    closeMapPanel();  // initialize empty state from the single source of truth
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
        const filter = ["in", ["get", "display_status"], ["literal", allowed]];
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
        const paused = !!t.paused_at;
        const props = {
            id: t.id,
            name: t.name,
            status: t.status,
            paused,
            blocked_from: t.blocked_from,
            display_status: paused ? "paused" : t.status,
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
    mapPolyGeojson = { type: "FeatureCollection", features: polyFeatures };
    mapPointGeojson = { type: "FeatureCollection", features: pointFeatures };

    // Overall extent for fitBounds.
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    for (const t of tiles) {
        if (t.bbox_west  < w) w = t.bbox_west;
        if (t.bbox_south < s) s = t.bbox_south;
        if (t.bbox_east  > e) e = t.bbox_east;
        if (t.bbox_north > n) n = t.bbox_north;
    }

    const matchExpr = ["match", ["get", "display_status"]];
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
                // Primary satellite (the same source operators see in the editor).
                // Toggled by `map-tool-sat`. Empty when tileserverUrl isn't
                // configured yet — addSource still needs a tiles array, so
                // fall back to a no-op data URI that 404s safely.
                "sat-overlay": {
                    type: "raster",
                    tiles: [tileserverUrl || "data:,"],
                    tileSize: 256,
                    maxzoom: tileserverMaxZoom || 19,
                },
                tiles: { type: "geojson", data: mapPolyGeojson },
                "tile-points": { type: "geojson", data: mapPointGeojson },
            },
            layers: [
                { id: "basemap-layer", type: "raster", source: "basemap" },
                // Satellite sits above the street basemap, below the mask
                // overlay (which is added dynamically by addClassOverlayLayer
                // before "tiles-fill"), below the polygons.
                {
                    id: "sat-overlay-layer", type: "raster", source: "sat-overlay",
                    layout: { visibility: "none" },
                    paint: { "raster-opacity": 1 },
                },
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
        transformRequest: tileTransformRequest,
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
            showTileInPanel(f.properties.id);
        });
    }

    wireMapSelectionTools();
    wireSatLayerToggle();
    wireClassOverlayToggle();
}

// ---------- Map tab: primary satellite overlay ----------

const SAT_LAYER_LS_KEY = "tc_admin_map_sat_on";

function satLayerEnabled() {
    return localStorage.getItem(SAT_LAYER_LS_KEY) === "1";
}

function setSatLayerEnabled(on) {
    if (on) localStorage.setItem(SAT_LAYER_LS_KEY, "1");
    else localStorage.removeItem(SAT_LAYER_LS_KEY);
}

function applySatLayerState(on) {
    const btn = document.getElementById("map-tool-sat");
    if (btn) btn.classList.toggle("active", on);
    if (mapView && mapView.getLayer("sat-overlay-layer")) {
        mapView.setLayoutProperty("sat-overlay-layer", "visibility", on ? "visible" : "none");
    }
}

function wireSatLayerToggle() {
    const btn = document.getElementById("map-tool-sat");
    if (!btn || !mapView) return;
    btn.addEventListener("click", () => {
        const on = !satLayerEnabled();
        setSatLayerEnabled(on);
        applySatLayerState(on);
    });
    const initial = satLayerEnabled();
    if (mapView.loaded()) applySatLayerState(initial);
    else mapView.once("load", () => applySatLayerState(initial));
}

// ---------- Map tab: classification overlay (server-rendered MBTiles) ----------

// Hard-coded to match config.yaml `mask_overlay.{min,max}_zoom`. Kept in sync
// manually because the values are stable; if they ever drift, MapLibre will
// fetch tiles outside the cached range and the server will 404.
const CLASS_OVERLAY_MIN_ZOOM = 8;
const CLASS_OVERLAY_MAX_ZOOM = 18;
const CLASS_OVERLAY_LS_KEY = "tc_admin_map_classifs_on";

function classOverlayEnabled() {
    return localStorage.getItem(CLASS_OVERLAY_LS_KEY) === "1";
}

function setClassOverlayEnabled(on) {
    if (on) localStorage.setItem(CLASS_OVERLAY_LS_KEY, "1");
    else localStorage.removeItem(CLASS_OVERLAY_LS_KEY);
}

function addClassOverlayLayer() {
    if (!mapView || mapView.getSource("mask-overlay")) return;
    // Cache-busting param so toggling off then on re-fetches anything the
    // browser cached during the previous session. Server-side MBTiles cache
    // is unaffected.
    const stamp = Date.now();
    mapView.addSource("mask-overlay", {
        type: "raster",
        tiles: [`/api/admin/mask-tiles/{z}/{x}/{y}.png?t=${stamp}`],
        tileSize: 256,
        minzoom: CLASS_OVERLAY_MIN_ZOOM,
        maxzoom: CLASS_OVERLAY_MAX_ZOOM,
    });
    // Place above the basemap but below the tile-status polygons so the
    // status fill stays as the topmost cue (admins still need it for bulk
    // selection by status).
    mapView.addLayer(
        {
            id: "mask-overlay-layer",
            type: "raster",
            source: "mask-overlay",
            paint: { "raster-opacity": 0.85, "raster-resampling": "nearest" },
        },
        "tiles-fill",
    );
}

function removeClassOverlayLayer() {
    if (!mapView) return;
    if (mapView.getLayer("mask-overlay-layer")) mapView.removeLayer("mask-overlay-layer");
    if (mapView.getSource("mask-overlay")) mapView.removeSource("mask-overlay");
}

function renderClassLegend(visible) {
    const legend = document.getElementById("map-class-legend");
    if (!legend) return;
    legend.classList.toggle("hidden", !visible);
    if (!visible) { legend.innerHTML = ""; return; }
    legend.innerHTML = "";
    const title = document.createElement("div");
    title.className = "map-class-legend-title";
    title.textContent = "Classes";
    legend.appendChild(title);
    for (const c of classes) {
        const row = document.createElement("div");
        row.className = "map-class-legend-row";
        const sw = document.createElement("span");
        sw.className = "map-legend-swatch";
        sw.style.background = c.color;
        const lbl = document.createElement("span");
        lbl.textContent = c.name;
        row.append(sw, lbl);
        legend.appendChild(row);
    }
}

function applyClassOverlayState(on) {
    const btn = document.getElementById("map-tool-classifs");
    if (btn) btn.classList.toggle("active", on);
    if (on) addClassOverlayLayer();
    else removeClassOverlayLayer();
    renderClassLegend(on);
    // Polygons render outline-only when the overlay is on so the
    // classification colors aren't covered. Use fill-opacity (instead of
    // visibility=none) so the layer still receives clicks and shows up in
    // queryRenderedFeatures for rectangle-select.
    if (mapView && mapView.getLayer("tiles-fill")) {
        mapView.setPaintProperty("tiles-fill", "fill-opacity", on ? 0 : 0.55);
    }
}

function wireClassOverlayToggle() {
    const btn = document.getElementById("map-tool-classifs");
    if (!btn || !mapView) return;
    btn.addEventListener("click", () => {
        const on = !classOverlayEnabled();
        setClassOverlayEnabled(on);
        applyClassOverlayState(on);
    });
    // Restore prior state once the map's initial style is loaded — adding a
    // source before `load` triggers internal MapLibre warnings.
    const initial = classOverlayEnabled();
    if (mapView.loaded()) applyClassOverlayState(initial);
    else mapView.once("load", () => applyClassOverlayState(initial));
}

// ---------- Map tab: rectangle selection + bulk actions ----------

function wireMapSelectionTools() {
    const toolBtn = document.getElementById("map-tool-rect");
    const clearBtn = document.getElementById("map-sel-clear");
    const reReviewBtn = document.getElementById("map-bulk-rereview");
    const container = document.getElementById("admin-map");
    const overlay = document.getElementById("map-rect-overlay");

    const assignBtn = document.getElementById("map-bulk-assign");
    const unassignBtn = document.getElementById("map-bulk-unassign");
    const blockBtn = document.getElementById("map-bulk-block");
    const unblockBtn = document.getElementById("map-bulk-unblock");
    toolBtn.addEventListener("click", () => setRectSelectActive(!mapRectSelectActive));
    clearBtn.addEventListener("click", () => clearMapSelection());
    reReviewBtn.addEventListener("click", () => mapBulkReReview());
    assignBtn.addEventListener("click", () => mapBulkAssign());
    unassignBtn.addEventListener("click", () => mapBulkUnassign());
    blockBtn.addEventListener("click", () => mapBulkBlock());
    unblockBtn.addEventListener("click", () => mapBulkUnblock());

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
        // tiles-outline included so rectangle-select still hits tiles when
        // the classification overlay hides tiles-fill (visibility=none drops
        // a layer out of queryRenderedFeatures results).
        const feats = mapView.queryRenderedFeatures(bbox, {
            layers: ["tiles-fill", "tiles-outline", "tiles-dot"],
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
    const blockableCount = ids.filter(id => BLOCKABLE_STATES.has(mapTilePropsById.get(id)?.status)).length;
    const blockedCount = ids.filter(id => mapTilePropsById.get(id)?.status === "blocked").length;
    const unassignableCount = ids.filter(id => {
        const s = mapTilePropsById.get(id)?.status;
        return s === "in_progress" || s === "in_review";
    }).length;
    const assignBtn = document.getElementById("map-bulk-assign");
    const unassignBtn = document.getElementById("map-bulk-unassign");
    const blockBtn = document.getElementById("map-bulk-block");
    const unblockBtn = document.getElementById("map-bulk-unblock");
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
    if (unassignBtn) {
        unassignBtn.disabled = unassignableCount === 0;
        unassignBtn.textContent = unassignableCount
            ? `Liberar operador (${unassignableCount})`
            : "Liberar operador";
    }
    if (blockBtn) {
        blockBtn.disabled = blockableCount === 0;
        blockBtn.textContent = blockableCount
            ? `Bloquear selecionados (${blockableCount})`
            : "Bloquear selecionados";
    }
    if (unblockBtn) {
        unblockBtn.disabled = blockedCount === 0;
        unblockBtn.textContent = blockedCount
            ? `Desbloquear selecionados (${blockedCount})`
            : "Desbloquear selecionados";
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
    for (const id of eligible) {
        setMapTileProps(mapTilePropsById.get(id), { status: "classified", paused: false });
    }
    refreshMapFeatureProps();
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
        u.active &&
        (!hasReview || ((u.can_review || u.role === "admin") && !classifierIds.has(u.id)))
    );
    if (!eligibleUsers.length) {
        showToast(hasReview
            ? "Sem revisores habilitados (ou todos já classificaram algum tile do lote)."
            : "Sem usuários ativos.", "error");
        return;
    }
    const r = await promptAssign({
        title: `Atribuir ${fresh.length} tile(s) a um usuário`,
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
    // Backend marks the tile as paused on assign, so display_status renders
    // as 'paused' until the operator hits play.
    for (const t of fresh) {
        const newStatus = t.status === "pending" ? "in_progress" : "in_review";
        setMapTileProps(mapTilePropsById.get(t.id), { status: newStatus, paused: true });
    }
    refreshMapFeatureProps();
    clearMapSelection();
}

// Release the assigned operator/reviewer from selected tiles without wiping
// the mask. Backend `unassign_many` is atomic — anything outside
// in_progress/in_review aborts the whole batch — so we pre-filter and surface
// the skipped count, mirroring the assign/block UX.
async function mapBulkUnassign() {
    const ids = [...mapSelectedIds];
    if (!ids.length) return;
    const eligible = ids.filter(id => {
        const s = mapTilePropsById.get(id)?.status;
        return s === "in_progress" || s === "in_review";
    });
    if (!eligible.length) {
        showToast("Nenhum tile selecionado tem operador atribuído (estados elegíveis: in_progress, in_review).", "error");
        return;
    }
    const skipped = ids.length - eligible.length;
    const r = await confirmDestructive({
        title: `Liberar operador de ${eligible.length} tile(s)`,
        description: (skipped > 0
            ? `O operador atual é removido e cada tile volta para a fila (in_progress→pending, in_review→classified). A máscara já pintada é preservada. ${skipped} tile(s) selecionado(s) não têm operador atribuído e serão ignorados.`
            : "O operador atual é removido e cada tile volta para a fila (in_progress→pending, in_review→classified). A máscara já pintada é preservada."),
        ids: eligible, confirmLabel: `Liberar ${eligible.length}`, danger: false,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/unassign", {
        ids: eligible, reason: r.reason,
    });
    showToast(`${resp.affected} liberado(s).`, "success");
    // Optimistic recolor matching the backend transitions. Backend clears
    // paused_at on unassign, so we drop the paused flag too.
    for (const id of eligible) {
        const p = mapTilePropsById.get(id);
        if (!p) continue;
        const newStatus = p.status === "in_progress" ? "pending"
                         : p.status === "in_review" ? "classified"
                         : p.status;
        setMapTileProps(p, { status: newStatus, paused: false });
    }
    refreshMapFeatureProps();
    clearMapSelection();
}

// Block selected tiles. Backend gate (`_BLOCKABLE_STATES`) is atomic: a single
// in_progress/in_review/problem/blocked tile in the batch 409s the whole call.
// We pre-filter here and surface the split so admins know what's being skipped.
async function mapBulkBlock() {
    const ids = [...mapSelectedIds];
    if (!ids.length) return;
    const eligible = ids.filter(id => BLOCKABLE_STATES.has(mapTilePropsById.get(id)?.status));
    if (!eligible.length) {
        showToast("Nenhum tile selecionado pode ser bloqueado (estados elegíveis: pending, classified, reviewed).", "error");
        return;
    }
    const skipped = ids.length - eligible.length;
    const r = await confirmDestructive({
        title: `Bloquear ${eligible.length} tile(s)`,
        description: (skipped > 0
            ? `Os tiles deixam de ser distribuídos até serem desbloqueados (status original guardado em blocked_from). ${skipped} tile(s) selecionado(s) estão em estados não bloqueáveis (in_progress/in_review/problem/blocked) e serão ignorados.`
            : "Os tiles deixam de ser distribuídos até serem desbloqueados. O status original é guardado em blocked_from para poder restaurar."),
        ids: eligible, confirmLabel: `Bloquear ${eligible.length}`, danger: false,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/block", {
        ids: eligible, reason: r.reason,
    });
    showToast(`${resp.affected} bloqueado(s).`, "success");
    // Optimistic recolor: status -> 'blocked', remember prior in blocked_from
    // so a follow-up unblock click can restore without a round-trip.
    for (const id of eligible) {
        const p = mapTilePropsById.get(id);
        if (p) setMapTileProps(p, { blocked_from: p.status, status: "blocked", paused: false });
    }
    refreshMapFeatureProps();
    clearMapSelection();
}

// Unblock selected tiles. Backend rejects anything not currently 'blocked' —
// we filter to surface the skipped count rather than letting the batch 409.
async function mapBulkUnblock() {
    const ids = [...mapSelectedIds];
    if (!ids.length) return;
    const eligible = ids.filter(id => mapTilePropsById.get(id)?.status === "blocked");
    if (!eligible.length) {
        showToast("Nenhum tile selecionado está bloqueado.", "error");
        return;
    }
    const skipped = ids.length - eligible.length;
    const r = await confirmDestructive({
        title: `Desbloquear ${eligible.length} tile(s)`,
        description: (skipped > 0
            ? `Cada tile volta ao status anterior ao bloqueio (blocked_from). ${skipped} tile(s) selecionado(s) não estão bloqueados e serão ignorados.`
            : "Cada tile volta ao status anterior ao bloqueio (blocked_from)."),
        ids: eligible, confirmLabel: `Desbloquear ${eligible.length}`, danger: false,
    });
    if (!r.confirmed) return;
    const resp = await apiPostJson("/api/admin/tiles/bulk/unblock", {
        ids: eligible, reason: r.reason,
    });
    showToast(`${resp.affected} desbloqueado(s).`, "success");
    // Optimistic recolor: status -> blocked_from, clear blocked_from. If the
    // cached props are missing blocked_from (shouldn't happen for blocked tiles
    // returned by /tiles/map, but be defensive), fall back to 'pending' so the
    // tile at least re-enters the queue rather than visually staying blocked.
    for (const id of eligible) {
        const p = mapTilePropsById.get(id);
        if (p) setMapTileProps(p, { status: p.blocked_from || "pending", paused: false, blocked_from: null });
    }
    refreshMapFeatureProps();
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
        const newRole = u.role === "admin" ? "operator" : "admin";
        const roleLabel = u.role === "admin" ? "Tornar operador" : "Tornar admin";
        tdAct.append(btn(roleLabel, () => changeRole(u.id, u.username, newRole)));
        // Admins are always reviewers (role check shortcuts can_review on the
        // backend), so display "sim" regardless of the column value.
        const isReviewer = u.role === "admin" || !!u.can_review;
        tr.append(td(u.id), td(u.username), td(u.role),
                  td(u.active ? "sim" : "não"), td(isReviewer ? "sim" : "não"),
                  td(fmtDate(u.created_at)), tdAct);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);
}

async function toggleCanReview(userId, canReview) {
    try {
        await apiPatchJson(`/api/admin/users/${userId}/can-review`, { can_review: canReview });
        showToast(canReview ? "Revisor habilitado." : "Revisor revogado.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}

async function changeRole(userId, username, newRole) {
    const promoting = newRole === "admin";
    const r = await confirmDestructive({
        title: promoting ? `Promover ${username} a admin?` : `Rebaixar ${username} a operador?`,
        description: promoting
            ? "O usuário ganhará acesso total ao painel administrativo (incluindo gerenciar usuários, resetar tiles e mudar roles)."
            : "O usuário perderá o acesso ao painel administrativo. As atribuições de tiles em andamento são preservadas.",
        ids: [userId],
        confirmLabel: promoting ? "Promover" : "Rebaixar",
        danger: !promoting,
    });
    if (!r.confirmed) return;
    try {
        await apiPatchJson(`/api/admin/users/${userId}/role`, { role: newRole });
        showToast(promoting ? "Usuário promovido a admin." : "Usuário rebaixado a operador.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}

async function toggleActive(userId, active) {
    try {
        await apiPatchJson(`/api/admin/users/${userId}/active`, { active });
        showToast(active ? "Usuário ativado." : "Usuário desativado.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}


// ---------- Maintenance ----------
// Operational read-only view of MBTiles state + on-disk overlay cache, with a
// single destructive action (wipe overlay cache). Editing config.yaml itself
// stays out of band — class palette / mbtiles paths are restart-only and the
// invariants aren't hot-reload safe.

const _MBTILES_LABELS = {
    primary: "Imagem principal (satélite)",
    dsg: "Overlay DSG",
    mapbiomas: "Overlay MapBiomas",
};

function _mbtilesCard(key, info) {
    const title = _MBTILES_LABELS[key] || key;
    if (!info.open) {
        return `<div class="maint-card">
            <h4>${escape(title)}</h4>
            <p class="muted">Não configurado ou arquivo ausente.</p>
        </div>`;
    }
    return `<div class="maint-card">
        <h4>${escape(title)}</h4>
        <dl class="maint-kv">
            <dt>Status</dt><dd>aberto</dd>
            <dt>Formato</dt><dd>${escape(info.format)}</dd>
            <dt>Zooms</dt><dd>${info.min_zoom ?? "?"}–${info.max_zoom ?? "?"}</dd>
            <dt>Arquivo</dt><dd><code>${escape(info.path || "")}</code></dd>
        </dl>
    </div>`;
}

async function renderMaintenance(root) {
    root.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando…</div>`;
    const data = await apiGet("/api/admin/maintenance/overview");
    const cache = data.overlay_cache;
    root.innerHTML = `
        <div class="maint-section">
            <h3>Imagens (MBTiles)</h3>
            <p class="muted">Configurado em <code>backend/config.yaml</code>; alterações exigem reiniciar o servidor.</p>
            <div class="maint-grid">
                ${_mbtilesCard("primary", data.mbtiles.primary)}
                ${_mbtilesCard("dsg", data.mbtiles.dsg)}
                ${_mbtilesCard("mapbiomas", data.mbtiles.mapbiomas)}
            </div>
        </div>

        <div class="maint-section">
            <h3>Cache do overlay administrativo</h3>
            <p class="muted">Tiles do mapa do admin são rasterizados sob demanda e armazenados aqui. A cache é invalidada automaticamente a cada mutação de máscara — limpe manualmente apenas após mudança de paleta ou importação em massa.</p>
            <div class="maint-grid">
                <div class="maint-card">
                    <h4>Estado</h4>
                    <dl class="maint-kv">
                        <dt>Tiles renderizados</dt><dd>${cache.rendered_tiles.toLocaleString("pt-BR")}</dd>
                        <dt>Tiles vazios cacheados</dt><dd>${cache.empty_tiles.toLocaleString("pt-BR")}</dd>
                        <dt>Tamanho em disco</dt><dd>${fmtBytes(cache.file_size_bytes)}</dd>
                        <dt>Zooms</dt><dd>${cache.min_zoom}–${cache.max_zoom}</dd>
                        <dt>Arquivo</dt><dd><code>${escape(cache.path)}</code></dd>
                    </dl>
                </div>
                <div class="maint-card">
                    <h4>Ações</h4>
                    <button id="maint-clear-cache" class="danger">Limpar cache</button>
                    <button id="maint-refresh">Atualizar</button>
                    <p class="muted maint-hint">A cache se reconstrói à medida que você navega no mapa do admin.</p>
                </div>
            </div>
        </div>
    `;
    document.getElementById("maint-refresh").addEventListener("click", () => selectTab("maintenance"));
    document.getElementById("maint-clear-cache").addEventListener("click", async () => {
        const r = await confirmDestructive({
            title: "Limpar cache do overlay?",
            description: `Vai apagar ${cache.rendered_tiles + cache.empty_tiles} entrada(s) (${fmtBytes(cache.file_size_bytes)}). A próxima navegação no mapa do admin recria conforme a área visualizada.`,
            confirmLabel: "Limpar",
            danger: true,
        });
        if (!r.confirmed) return;
        try {
            const res = await apiPostJson("/api/admin/maintenance/overlay-cache/clear", {});
            showToast(`Cache limpa: ${res.deleted} entrada(s) removida(s).`, "success");
            selectTab("maintenance");
        } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
    });
}
