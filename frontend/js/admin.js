// Admin panel: dashboard, tiles (list+grid+bulk+filters+viewer), problems, users.
import { apiGet, apiGetBlob, apiGetWithHeaders, apiPostJson, apiJson, logout as apiLogout } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap } from "./maplib.js";
import { hexToRgb, blobToImage, escapeHtml as escape, fmtDate } from "./utils.js";

let tileserverUrl = "";
let classes = [];
let classesById = {};
let selectedIds = new Set();
let currentTab = "dashboard";
let listView = "table"; // "table" | "grid"

// Sort + pagination state for the tiles tab
let sortKey = "id";
let sortDir = "asc";
let page = 0;
const PAGE_SIZE = 100;
let totalTiles = 0;

// Sort state for dashboard per-operator table
let opSortKey = "username";
let opSortDir = "asc";

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
    classes = cls;
    classesById = Object.fromEntries(classes.map(c => [c.id, c]));
    document.getElementById("view-tile-close").addEventListener("click", () => {
        document.getElementById("modal-view-tile").classList.add("hidden");
    });
    wireConfirmModal();
    await selectTab("dashboard");
}

async function selectTab(tab) {
    currentTab = tab;
    selectedIds.clear();
    page = 0;
    document.querySelectorAll(".admin-nav button").forEach(b => {
        b.classList.toggle("active", b.dataset.tab === tab);
    });
    const content = document.getElementById("admin-content");
    content.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando...</div>`;
    try {
        if (tab === "dashboard") await renderDashboard(content);
        else if (tab === "tiles") await renderTiles(content);
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
    grid.append(
        statCard("Total de tiles", d.total_tiles),
        statCard("% concluído", `${d.completion_percent}%`),
        statCard("Revisados", d.totals_by_status.reviewed || 0),
        statCard("Pendentes", d.totals_by_status.pending || 0),
        statCard("Em andamento", (d.totals_by_status.in_progress || 0) + (d.totals_by_status.in_review || 0)),
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
            <label>Status <select id="filter-status">
                <option value="">(todos)</option>
                <option>pending</option><option>in_progress</option><option>classified</option>
                <option>in_review</option><option>reviewed</option><option>problem</option>
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
            <button id="bulk-reset">Resetar</button>
            <button id="bulk-rereview">Re-revisar</button>
            <button id="bulk-clear">Limpar seleção</button>
        </div>
        <div id="tiles-list"></div>
        <div id="pager" class="pager"></div>
    `;
    document.getElementById("view-table").addEventListener("click", () => { listView = "table"; loadAndRender(); });
    document.getElementById("view-grid").addEventListener("click", () => { listView = "grid"; loadAndRender(); });
    document.getElementById("btn-filter").addEventListener("click", () => { page = 0; loadAndRender(); });
    document.getElementById("bulk-reset").addEventListener("click", bulkReset);
    document.getElementById("bulk-rereview").addEventListener("click", bulkReReview);
    document.getElementById("bulk-clear").addEventListener("click", () => { selectedIds.clear(); loadAndRender(); });
    await loadAndRender();
}

async function loadAndRender() {
    const status = document.getElementById("filter-status").value;
    const df = document.getElementById("filter-from").value;
    const dt = document.getElementById("filter-to").value;
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (df) params.set("date_from", df);
    if (dt) params.set("date_to", dt);
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
    const el = document.getElementById("pager");
    if (!el) return;
    const totalPages = Math.max(1, Math.ceil(totalTiles / PAGE_SIZE));
    el.innerHTML = "";
    const info = document.createElement("span");
    info.className = "pager-info";
    const start = page * PAGE_SIZE + 1;
    const end = Math.min(totalTiles, (page + 1) * PAGE_SIZE);
    info.textContent = totalTiles === 0 ? "Nenhum tile" : `${start}–${end} de ${totalTiles}`;
    const prev = document.createElement("button");
    prev.textContent = "← Anterior"; prev.disabled = page === 0;
    prev.addEventListener("click", () => { page--; loadAndRender(); });
    const next = document.createElement("button");
    next.textContent = "Próxima →"; next.disabled = page >= totalPages - 1;
    next.addEventListener("click", () => { page++; loadAndRender(); });
    el.append(prev, info, next);
}

function updateBulkBar() {
    const bar = document.getElementById("bulk-bar");
    document.getElementById("bulk-count").textContent = selectedIds.size;
    bar.classList.toggle("hidden", selectedIds.size === 0);
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
    for (const t of tiles) {
        const tr = document.createElement("tr");
        tr.dataset.id = t.id;
        if (selectedIds.has(t.id)) tr.classList.add("selected");
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = selectedIds.has(t.id);
        cb.addEventListener("change", () => toggleSelect(t.id, cb.checked, tr));
        const tdCb = document.createElement("td"); tdCb.appendChild(cb);
        const tdStatus = document.createElement("td");
        const chip = document.createElement("span");
        chip.className = `chip ${t.status}`; chip.textContent = t.status;
        tdStatus.appendChild(chip);
        const tdAct = document.createElement("td");
        tdAct.append(
            btn("Ver", () => openViewer(t.id)),
            btn("Resetar", () => resetOne(t.id)),
        );
        if (t.status === "reviewed") tdAct.append(btn("Re-revisar", () => reReviewOne(t.id)));
        if (t.status === "in_progress" || t.status === "in_review") {
            tdAct.append(btn("Liberar operador", () => unassignOne(t.id)));
        }
        tr.append(tdCb, td(t.id), td(t.name), tdStatus,
                  td(classifierCell(t)), td(reviewerCell(t)),
                  td(finishedCell(t.classified_at, t.status === "in_progress")),
                  td(finishedCell(t.reviewed_at, t.status === "in_review")),
                  tdAct);
        tbody.appendChild(tr);
    }
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

function renderGrid(root, tiles) {
    const grid = document.createElement("div");
    grid.className = "thumb-grid";
    for (const t of tiles) {
        const card = document.createElement("div");
        card.className = "thumb-card" + (selectedIds.has(t.id) ? " selected" : "");
        card.dataset.id = t.id;
        const img = document.createElement("img");
        img.alt = `Tile ${t.id}`;
        img.className = "thumb-skeleton";
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
        if (whoText) {
            const who = document.createElement("div");
            who.className = "meta-dim";
            who.textContent = whoText;
            card.append(img, meta, who);
        } else {
            card.append(img, meta);
        }
        card.addEventListener("click", (ev) => {
            if (ev.shiftKey) toggleSelect(t.id, !selectedIds.has(t.id), card);
            else openViewer(t.id);
        });
        grid.appendChild(card);
    }
    root.appendChild(grid);
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

function confirmDestructive({ title, description, ids, confirmLabel = "Confirmar", danger = true }) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-confirm");
        document.getElementById("confirm-title").textContent = title;
        document.getElementById("confirm-description").textContent = description;
        const list = document.getElementById("confirm-ids");
        list.innerHTML = "";
        const preview = ids.slice(0, 20);
        list.textContent = `IDs: ${preview.join(", ")}${ids.length > 20 ? ` … (+${ids.length - 20})` : ""}`;
        const reason = document.getElementById("confirm-reason");
        reason.value = "";
        const ok = document.getElementById("confirm-ok");
        ok.textContent = confirmLabel;
        ok.classList.toggle("danger", !!danger);
        const cancel = document.getElementById("confirm-cancel");

        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            resolve(result);
        };
        const onOk = () => cleanup({ confirmed: true, reason: reason.value.trim() });
        const onCancel = () => cleanup({ confirmed: false });
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
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
    loadAndRender();
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
    loadAndRender();
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
    loadAndRender();
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
    loadAndRender();
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
    loadAndRender();
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
        chip.className = `chip ${t.status}`; chip.textContent = t.status;
        statusLine.appendChild(chip);
        if (t.classified_by_username) {
            statusLine.append(document.createTextNode(" · classificado por "));
            const b = document.createElement("b"); b.textContent = t.classified_by_username;
            statusLine.append(b);
        }
        meta.appendChild(statusLine);
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
                [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north]);
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
        tdAct.append(btn("Ver", () => openViewer(p.id)), btn("Resetar", () => resetOne(p.id)));
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
    thead.innerHTML = "<tr><th>ID</th><th>Usuário</th><th>Role</th><th>Ativo</th><th>Criado em</th><th>Ações</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const u of users) {
        const tr = document.createElement("tr");
        const tdAct = document.createElement("td");
        const actLabel = u.active ? "Desativar" : "Ativar";
        tdAct.append(btn(actLabel, () => toggleActive(u.id, !u.active)));
        tr.append(td(u.id), td(u.username), td(u.role), td(u.active ? "sim" : "não"), td(fmtDate(u.created_at)), tdAct);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);
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
