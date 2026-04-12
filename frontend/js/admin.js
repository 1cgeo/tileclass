// Admin panel: dashboard, tiles (list+grid+bulk+filters+viewer), problems, users.
import { apiGet, apiGetBlob, apiPostJson, apiJson } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap } from "./maplib.js";
import { hexToRgb, blobToImage, escapeHtml as escape } from "./utils.js";

let tileserverUrl = "";
let classes = [];
let classesById = {};
let selectedIds = new Set();
let currentTab = "dashboard";
let listView = "table"; // "table" | "grid"

export async function initAdmin(user) {
    document.getElementById("admin-user-label").textContent = `${user.username} (admin)`;
    document.getElementById("btn-admin-logout").addEventListener("click", () => {
        localStorage.removeItem("tileclass_tokens"); location.reload();
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
    await selectTab("dashboard");
}

async function selectTab(tab) {
    currentTab = tab;
    selectedIds.clear();
    document.querySelectorAll(".admin-nav button").forEach(b => {
        b.classList.toggle("active", b.dataset.tab === tab);
    });
    const content = document.getElementById("admin-content");
    content.innerHTML = "<p>Carregando...</p>";
    try {
        if (tab === "dashboard") await renderDashboard(content);
        else if (tab === "tiles") await renderTiles(content);
        else if (tab === "problems") await renderProblems(content);
        else if (tab === "users") await renderUsers(content);
    } catch (e) {
        content.innerHTML = `<p class="error">Erro: ${e.message}</p>`;
    }
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
        const bar = document.createElement("span"); bar.className = "bar"; bar.style.width = `${(v/total)*400}px`;
        const count = document.createElement("span"); count.className = "count"; count.textContent = v;
        row.append(name, bar, count);
        root.appendChild(row);
    }

    const opH = document.createElement("h3");
    opH.textContent = "Por operador";
    opH.style.marginTop = "16px";
    root.appendChild(opH);
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>Usuário</th><th>Classificados</th><th>Revisados</th><th>Problemas</th><th>Tempo médio (s/tile)</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const op of d.per_operator) {
        const tr = document.createElement("tr");
        [op.username, op.classified || 0, op.reviewed || 0, op.problems || 0, op.avg_seconds_per_tile || 0]
            .forEach(v => { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); });
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);

    const dayH = document.createElement("h3");
    dayH.textContent = "Tiles revisados por dia";
    dayH.style.marginTop = "16px";
    root.appendChild(dayH);
    const maxDaily = Math.max(1, ...d.daily_completed.map(r => r.count));
    for (const r of d.daily_completed) {
        const row = document.createElement("div");
        row.className = "bar-row";
        const name = document.createElement("span"); name.className = "name"; name.textContent = r.date;
        const bar = document.createElement("span"); bar.className = "bar"; bar.style.width = `${(r.count/maxDaily)*300}px`;
        const count = document.createElement("span"); count.className = "count"; count.textContent = r.count;
        row.append(name, bar, count);
        root.appendChild(row);
    }
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
    `;
    document.getElementById("view-table").addEventListener("click", () => { listView = "table"; loadAndRender(); });
    document.getElementById("view-grid").addEventListener("click", () => { listView = "grid"; loadAndRender(); });
    document.getElementById("btn-filter").addEventListener("click", loadAndRender);
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
    const qs = params.toString() ? `?${params}` : "";
    const tiles = await apiGet(`/api/admin/tiles${qs}`);
    document.querySelectorAll(".view-mode-toggle button").forEach(b => {
        b.classList.toggle("active", b.id === `view-${listView}`);
    });
    const target = document.getElementById("tiles-list");
    target.innerHTML = "";
    if (listView === "table") renderTable(target, tiles);
    else renderGrid(target, tiles);
    updateBulkBar();
}

function updateBulkBar() {
    const bar = document.getElementById("bulk-bar");
    document.getElementById("bulk-count").textContent = selectedIds.size;
    bar.classList.toggle("hidden", selectedIds.size === 0);
}

function renderTable(root, tiles) {
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = `<tr><th><input type="checkbox" id="check-all"></th>
        <th>ID</th><th>Nome</th><th>Status</th><th>Classificado</th><th>Revisado</th><th>Ações</th></tr>`;
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const t of tiles) {
        const tr = document.createElement("tr");
        tr.dataset.id = t.id;
        if (selectedIds.has(t.id)) tr.classList.add("selected");
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = selectedIds.has(t.id);
        cb.addEventListener("change", () => toggleSelect(t.id, cb.checked, tr));
        const tdCb = document.createElement("td"); tdCb.appendChild(cb);
        const tdId = td(t.id), tdName = td(t.name), tdStatus = td(t.status),
              tdC = td(t.classified_at || ""), tdR = td(t.reviewed_at || "");
        const tdAct = document.createElement("td");
        tdAct.append(
            btn("Ver", () => openViewer(t.id)),
            btn("Resetar", () => resetOne(t.id)),
        );
        if (t.status === "reviewed") tdAct.append(btn("Re-revisar", () => reReviewOne(t.id)));
        tr.append(tdCb, tdId, tdName, tdStatus, tdC, tdR, tdAct);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    root.appendChild(table);
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
    const tokens = JSON.parse(localStorage.getItem("tileclass_tokens") || "{}");
    for (const t of tiles) {
        const card = document.createElement("div");
        card.className = "thumb-card" + (selectedIds.has(t.id) ? " selected" : "");
        card.dataset.id = t.id;
        const img = document.createElement("img");
        // Thumbnails require auth; use fetch + blob URL.
        apiGetBlob(`/api/admin/tiles/${t.id}/thumbnail?size=128`)
            .then(b => { img.src = URL.createObjectURL(b); })
            .catch(() => { img.alt = "?"; });
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `#${t.id} · ${t.status}`;
        card.append(img, meta);
        card.addEventListener("click", (ev) => {
            if (ev.shiftKey) {
                toggleSelect(t.id, !selectedIds.has(t.id), card);
            } else {
                openViewer(t.id);
            }
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
function btn(label, fn) {
    const b = document.createElement("button"); b.textContent = label;
    b.style.marginRight = "4px";
    b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
    return b;
}

async function resetOne(id) {
    if (!confirm(`Resetar tile #${id}?`)) return;
    await apiPostJson(`/api/admin/tiles/${id}/reset`, {});
    showToast("Resetado.", "success");
    loadAndRender();
}
async function reReviewOne(id) {
    await apiPostJson(`/api/admin/tiles/${id}/re-review`, {});
    showToast("Enviado para nova revisão.", "success");
    loadAndRender();
}
async function bulkReset() {
    if (selectedIds.size === 0) return;
    if (!confirm(`Resetar ${selectedIds.size} tiles?`)) return;
    const r = await apiPostJson("/api/admin/tiles/bulk/reset", { ids: [...selectedIds] });
    showToast(`${r.affected} resetados.`, "success");
    selectedIds.clear();
    loadAndRender();
}
async function bulkReReview() {
    if (selectedIds.size === 0) return;
    if (!confirm(`Enviar ${selectedIds.size} tiles para re-revisão?`)) return;
    const r = await apiPostJson("/api/admin/tiles/bulk/re-review", { ids: [...selectedIds] });
    showToast(`${r.affected} enviados.`, "success");
    selectedIds.clear();
    loadAndRender();
}

async function openViewer(tileId) {
    const modal = document.getElementById("modal-view-tile");
    const body = document.getElementById("view-tile-body");
    document.getElementById("view-tile-title").textContent = `Tile #${tileId}`;
    body.innerHTML = `<div id="viewer-loading">Carregando...</div>`;
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
        statusLine.append(document.createTextNode(t.status));
        if (t.classified_by_username) {
            statusLine.append(document.createTextNode(" · classificado por "));
            const b = document.createElement("b"); b.textContent = t.classified_by_username;
            statusLine.append(b);
        }
        meta.appendChild(statusLine);
        body.appendChild(meta);

        // Action history
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
                const when = document.createElement("div"); when.className = "when"; when.textContent = ev.created_at;
                li.append(act, by);
                if (ev.detail) {
                    li.append(document.createTextNode(` — ${ev.detail}`));
                }
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
        // Render satellite
        setTimeout(() => {
            createLockedMap(`viewer-map-${tileId}`, tileserverUrl,
                [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north]);
        }, 0);
        // Render mask colorized
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
        body.innerHTML = `<p class="error">Erro: ${e.message}</p>`;
    }
}


async function renderProblems(root) {
    const problems = await apiGet("/api/admin/tiles/problems");
    root.innerHTML = "<h3>Tiles com problema</h3>";
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
        tr.append(td(p.id), td(p.name), td(p.problem_note || ""), td(p.reported_at || ""), tdAct);
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
            <input type="text" id="nu-username" placeholder="username" required>
            <input type="password" id="nu-password" placeholder="senha (>= 6)" required minlength="6">
            <select id="nu-role"><option value="operator">operator</option><option value="admin">admin</option></select>
            <button type="submit" class="primary">Criar</button>
        </form>
        <h3>Usuários</h3>
    `;
    document.getElementById("form-user").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        try {
            await apiPostJson("/api/admin/users", {
                username: document.getElementById("nu-username").value,
                password: document.getElementById("nu-password").value,
                role: document.getElementById("nu-role").value,
            });
            showToast("Usuário criado.", "success");
            selectTab("users");
        } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
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
        tr.append(td(u.id), td(u.username), td(u.role), td(u.active ? "sim" : "não"), td(u.created_at), tdAct);
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
