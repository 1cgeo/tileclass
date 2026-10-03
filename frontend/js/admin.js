// Admin panel shell + tabs: tiles (table/grid, filters, bulk actions, viewer),
// map, problems, users, maintenance. Dashboard and Projetos live in ./admin/;
// the rest stays here because tiles/map/actions/viewer share so much state
// that splitting them costs more readability than it gains.
import { apiGet, apiGetBlob, apiGetWithHeaders, apiPostJson, apiPatchJson, apiJson, logout as apiLogout } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap, disposeMap, tileTransformRequest } from "./maplib.js";
import {
    hexToRgb, blobToImage, fmtDate, fmtBytes, withProjectParam, renderProjectPicker,
    statusLabel, kindLabel, truncateName,
} from "./utils.js";
import {
    BLOCKABLE_STATES, tileRowActions, pageWindow, actionLabel, actionTone, relativeTime,
    initials, fmtInt, STATUS_ORDER,
} from "./admin-core.js";
import { renderDashboard } from "./admin/dashboard.js";
import { wireConfirmModal, confirmDestructive, promptAssign, promptForm } from "./admin/modals.js";
import { renderProjects } from "./admin/projects.js";
import {
    h, icon, button, iconButton, avatar, userCell, statusChip, card, kpi, emptyState,
    menuButton, sortHeader, cssVar, closeMenu,
} from "./admin/ui.js";

let tileserverUrl = "";
let tileserverMaxZoom = 22;
let classes = [];
let classesById = {};
let currentProjectId = null;
let projectsById = {};   // id → { id, name, kind, ... } — populated by initAdmin
let selectedIds = new Set();
let currentTab = "dashboard";
let listView = "table"; // "table" | "grid"
// Status preset handed from the dashboard ("ver estes tiles") to the Tiles tab.
let _pendingStatusFilter = null;

// User list used by the tiles-tab filter dropdown. Lazily populated by
// renderTiles via /api/admin/users; invalidated when a user is created or
// toggled so the dropdown doesn't go stale across tab switches.
let _usersCache = null;

// Sync palette + tileserver to the current project so the map overlay (legend
// colors) and thumbnails reflect the project being filtered. `null` resets
// everything — cross-project view (e.g. Tiles filtered to "all") has no
// single palette/tileserver to anchor to.
async function _loadProjectPalette(projectId) {
    if (projectId == null) {
        tileserverUrl = "";
        tileserverMaxZoom = 22;
        classes = [];
        classesById = {};
        return;
    }
    const proj = await apiGet(`/api/projects/${projectId}`);
    const primary = (proj.layers || {}).primary;
    tileserverUrl = (primary && primary.url) || "";
    tileserverMaxZoom = (primary && primary.max_zoom) ?? 22;
    classes = proj.classes || [];
    classesById = Object.fromEntries(classes.map(c => [c.id, c]));
    projectsById[proj.id] = { ...projectsById[proj.id], ...proj };
}

// Public refresh hook called from projects.js after CRUD (create/delete/clone/
// rename). Returns the up-to-date project list so callers don't refetch.
export async function syncAdminProjects() {
    const projects = (await apiGet("/api/projects")) || [];
    projectsById = Object.fromEntries(projects.map(p => [p.id, p]));
    if (currentProjectId != null && !projectsById[currentProjectId]) {
        // Selected project was deleted → fall back to the first one (or null).
        currentProjectId = projects.length ? projects[0].id : null;
        if (currentProjectId != null) {
            try { await _loadProjectPalette(currentProjectId); } catch {}
        } else {
            tileserverUrl = ""; tileserverMaxZoom = 22;
            classes = []; classesById = {};
        }
    }
    return projects;
}

// Single `onChange` shared by every tab's project scope picker (Dashboard,
// Tiles, Mapa, Problemas). Keeps `currentProjectId` + palette in sync.
async function _onProjectFilterChange(newId) {
    currentProjectId = newId;
    await _loadProjectPalette(newId);
    selectedIds.clear();
    page = 0;
    await selectTab(currentTab);
}

// Mount the project scope picker into the top bar. Each tab keeps its own
// ids (`<tab>-project-filter[-wrap]`) — tests and deep links rely on them.
function _wireTabProjectFilter({ wrapId, selectId, allowAll = true, alwaysShow = true }) {
    const slot = document.getElementById("admin-scope-slot");
    slot.textContent = "";
    const sel = h("select", { id: selectId, "aria-label": "Projeto" });
    slot.appendChild(h("label", { id: wrapId, class: "scope-picker", title: "Projeto exibido" },
        icon("folder"), sel));
    renderProjectPicker({
        wrapId, selectId,
        projects: Object.values(projectsById),
        activeId: currentProjectId,
        allowAll, alwaysShow,
        allLabel: "Todos os projetos",
        onChange: _onProjectFilterChange,
    });
}

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
// AbortController scoped to the current map view. Owns the `mousemove`/
// `mouseup` listeners attached to `window` for the rect-select gesture (the
// container can't catch them because the drag may leave its bounds). Aborted
// on tab change / re-render so listeners don't accumulate across sessions.
let mapEventsAbort = null;
// Active MapLibre instance for the tile viewer modal. Disposed on close
// and before each re-open — otherwise WebGL contexts pile up until the
// browser starts evicting them ("Too many active WebGL contexts").
let viewerMap = null;

function disposeViewerMap() { viewerMap = disposeMap(viewerMap); }

const isBlockable = t => BLOCKABLE_STATES.has(t.status);

let _adminInitialized = false;

export async function initAdmin(user) {
    document.getElementById("admin-user-label").textContent = user.username;
    document.getElementById("admin-user-avatar").textContent = initials(user.username);
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
    // All admin endpoints (dashboard, tiles, map, problems, distributions,
    // mask overlay) accept ?project_id=. The picker switches between projects;
    // re-selecting the current tab re-issues every query with the new scope.
    const projects = await apiGet("/api/projects");
    projectsById = Object.fromEntries((projects || []).map(p => [p.id, p]));
    if (projects && projects.length) {
        currentProjectId = projects[0].id;
        await _loadProjectPalette(currentProjectId);
    } else {
        currentProjectId = null;
        tileserverUrl = "";
        tileserverMaxZoom = 22;
        classes = [];
        classesById = {};
    }
    const vtModal = document.getElementById("modal-view-tile");
    const closeVt = () => {
        vtModal.classList.add("hidden");
        disposeViewerMap();
        // Clear body so any in-flight renderTileInfo's setTimeout sees the
        // mapDiv is gone and bails before creating an orphan WebGL context.
        document.getElementById("view-tile-body").textContent = "";
    };
    document.getElementById("view-tile-close").addEventListener("click", closeVt);
    // Click on the backdrop (outside the modal-box) closes the viewer.
    vtModal.addEventListener("click", (ev) => { if (ev.target === vtModal) closeVt(); });
    document.addEventListener("keydown", (ev) => {
        if (document.getElementById("view-admin").classList.contains("hidden")) return;
        if (ev.key === "Escape" && !vtModal.classList.contains("hidden")) closeVt();
    });
    // Map layers paint status colors from CSS tokens — repaint on theme switch.
    window.addEventListener("tc-themechange", () => applyMapTheme());
    wireConfirmModal();
    _adminInitialized = true;
    await selectTab("dashboard");
}

// Re-render the active tab so dashboards/lists reflect any work the admin
// just did while toggled into the editor view.
export async function enterAdmin() {
    await selectTab(currentTab || "dashboard");
}

// Page title + subtitle per tab (top bar), also used for the browser tab.
const _TAB_META = {
    dashboard:   { title: "Dashboard",  sub: "Progresso, ritmo e produtividade da equipe" },
    projects:    { title: "Projetos",   sub: "Configuração, classes, membros e exportação" },
    tiles:       { title: "Tiles",      sub: "Busque, inspecione e aja sobre tiles individuais ou em lote" },
    map:         { title: "Mapa",       sub: "Distribuição espacial dos tiles por status" },
    problems:    { title: "Problemas",  sub: "Tiles reportados pela equipe que precisam de decisão" },
    users:       { title: "Usuários",   sub: "Contas, papéis e acesso ao sistema" },
    maintenance: { title: "Manutenção", sub: "Estado do cache do overlay do mapa" },
};

// Tabs whose content depends on the project scope picker.
const _SCOPED_TABS = new Set(["dashboard", "tiles", "map", "problems"]);

async function selectTab(tab) {
    closeMenu();
    currentTab = tab;
    selectedIds.clear();
    page = 0;
    const meta = _TAB_META[tab] || { title: tab, sub: "" };
    document.title = `${meta.title} · Admin — TileClass`;
    document.getElementById("admin-page-title").textContent = meta.title;
    document.getElementById("admin-page-subtitle").textContent = meta.sub;
    document.getElementById("admin-scope-slot").textContent = "";
    document.getElementById("admin-page-actions").textContent = "";
    if (mapEventsAbort) { mapEventsAbort.abort(); mapEventsAbort = null; }
    mapView = disposeMap(mapView);
    // Drop the GeoJSON references when leaving the map tab — these hold one
    // feature per tile and pin tile-row-sized objects in memory while admin
    // sessions stay open.
    mapPolyGeojson = null;
    mapPointGeojson = null;
    disposeViewerMap();
    document.querySelectorAll(".admin-nav button").forEach(b => {
        const on = b.dataset.tab === tab;
        b.classList.toggle("active", on);
        if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    const content = document.getElementById("admin-content");
    content.classList.toggle("is-map", tab === "map");
    content.dataset.tab = tab;
    content.scrollTop = 0;
    // Each render gets its own container. Switching tabs replaces it, so a
    // slow render from the previous tab (e.g. the dashboard still fetching)
    // lands in a detached node instead of overwriting the new tab.
    const view = h("div", { class: "tab-view", dataset: { tab } });
    view.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando...</div>`;
    content.replaceChildren(view);
    if (_SCOPED_TABS.has(tab)) {
        _wireTabProjectFilter({ wrapId: `${tab}-project-filter-wrap`, selectId: `${tab}-project-filter` });
    }
    refreshProblemsBadge();
    try {
        if (tab === "dashboard") {
            await renderDashboard(view, {
                projectId: currentProjectId, projectsById,
                onSelectProject: _onProjectFilterChange,
                onOpenStatus: (status) => {
                    if (status === "problem") { selectTab("problems"); return; }
                    _pendingStatusFilter = status;
                    selectTab("tiles");
                },
            });
        }
        else if (tab === "projects") await renderProjects(view);
        else if (tab === "tiles") await renderTiles(view);
        else if (tab === "map") await renderMap(view);
        else if (tab === "problems") await renderProblems(view);
        else if (tab === "users") await renderUsers(view);
        else if (tab === "maintenance") await renderMaintenance(view);
    } catch (e) {
        if (view.isConnected) renderError(view, e);
    }
}

// Sidebar badge with the number of open problems (all projects).
async function refreshProblemsBadge() {
    const el = document.getElementById("nav-problems-count");
    if (!el) return;
    try {
        const list = await apiGet("/api/admin/tiles/problems");
        const n = (list || []).length;
        el.textContent = n > 99 ? "99+" : String(n);
        el.classList.toggle("hidden", n === 0);
        el.title = `${n} problema(s) aberto(s)`;
    } catch { el.classList.add("hidden"); }
}

function renderError(target, e) {
    target.textContent = "";
    target.appendChild(card({},
        emptyState({ icon: "triangle-alert", title: "Não foi possível carregar", text: `Erro: ${e.message}` })));
}

// Filter-status options. `value` stays as the API contract (English snake_case);
// only the label is localized. `paused_review` is virtual — the backend uses
// it to filter paused-in-review tiles via the `paused=true` query param.
const _STATUS_FILTER_OPTIONS = [
    { value: "",              label: "Todos os status" },
    { value: "pending",       label: statusLabel("pending") },
    { value: "in_progress",   label: statusLabel("in_progress") },
    { value: "paused",        label: statusLabel("paused") },
    { value: "paused_review", label: "Pausado (revisão)" },
    { value: "classified",    label: statusLabel("classified") },
    { value: "in_review",     label: statusLabel("in_review") },
    { value: "reviewed",      label: statusLabel("reviewed") },
    { value: "problem",       label: statusLabel("problem") },
    { value: "blocked",       label: statusLabel("blocked") },
];

async function renderTiles(root) {
    // Load the user list once per session so the operator dropdown is ready
    // before the first filter — the table itself doesn't need it.
    if (_usersCache == null) {
        try { _usersCache = await apiGet("/api/admin/users"); }
        catch { _usersCache = []; }
    }
    if (!root.isConnected) return;  // tab changed while loading
    root.textContent = "";
    const statusSel = h("select", { id: "filter-status", "aria-label": "Status" },
        _STATUS_FILTER_OPTIONS.map(o => h("option", { value: o.value, text: o.label })));
    const userSel = h("select", { id: "filter-user", "aria-label": "Operador",
                                  title: "Filtra tiles em que esse usuário classificou ou revisou" },
        h("option", { value: "", text: "Qualquer operador" }),
        _usersCache.filter(u => u.active).map(u => h("option", { value: String(u.id), text: u.username })));
    const viewToggle = h("div", { class: "segmented view-mode-toggle", role: "group", "aria-label": "Visualização" },
        h("button", { type: "button", id: "view-table", class: listView === "table" ? "active" : null, title: "Tabela" },
            icon("table-2"), "Tabela"),
        h("button", { type: "button", id: "view-grid", class: listView === "grid" ? "active" : null, title: "Grade" },
            icon("layout-grid"), "Grade"));
    const toolbar = h("div", { class: "toolbar tiles-toolbar" },
        h("label", { class: "input-icon toolbar-search" }, icon("search"),
            h("input", { type: "search", id: "filter-q", placeholder: "Buscar por ID ou nome…", autocomplete: "off",
                         "aria-label": "Buscar tiles" })),
        h("label", { class: "toolbar-field" }, statusSel),
        h("label", { class: "toolbar-field" }, userSel),
        h("div", { class: "toolbar-dates", title: "Período (classificação ou revisão)" },
            icon("calendar", "icon-sm"),
            h("input", { type: "date", id: "filter-from", "aria-label": "De" }),
            h("span", { class: "dim", text: "–" }),
            h("input", { type: "date", id: "filter-to", "aria-label": "Até" })),
        button("Filtrar", { id: "btn-filter", icon: "filter" }));
    const bulk = h("div", { id: "bulk-bar", class: "bulk-bar hidden", role: "toolbar", "aria-label": "Ações em lote" },
        h("span", { class: "bulk-count" }, h("strong", { id: "bulk-count", class: "tabular", text: "0" }), " selecionados"),
        h("span", { class: "bulk-sep" }),
        button("Atribuir…", { id: "bulk-assign", icon: "user-plus", variant: "ghost btn-sm" }),
        button("Liberar", { id: "bulk-unassign", icon: "log-out", variant: "ghost btn-sm", title: "Liberar operador" }),
        button("Re-revisar", { id: "bulk-rereview", icon: "refresh-cw", variant: "ghost btn-sm" }),
        button("Bloquear", { id: "bulk-block", icon: "lock", variant: "ghost btn-sm" }),
        button("Desbloquear", { id: "bulk-unblock", icon: "lock-open", variant: "ghost btn-sm" }),
        button("Reportar…", { id: "bulk-report-problem", icon: "flag", variant: "ghost btn-sm", title: "Reportar problema" }),
        button("Resetar", { id: "bulk-reset", icon: "rotate-ccw", variant: "ghost btn-sm bulk-danger" }),
        h("span", { class: "bulk-sep" }),
        iconButton("x", "Limpar seleção", { id: "bulk-clear" }));
    root.append(
        toolbar,
        h("div", { class: "results-bar" },
            h("span", { id: "tiles-result-count", class: "results-count" }),
            h("div", { class: "results-bar-right" }, h("div", { id: "pager-top", class: "pager" }), viewToggle)),
        h("div", { id: "tiles-list" }),
        h("div", { id: "pager-bottom", class: "pager" }),
        bulk);
    if (_pendingStatusFilter) {
        statusSel.value = _pendingStatusFilter;
        _pendingStatusFilter = null;
    }
    document.getElementById("view-table").addEventListener("click", () => { listView = "table"; loadAndRender(); });
    document.getElementById("view-grid").addEventListener("click", () => { listView = "grid"; loadAndRender(); });
    document.getElementById("btn-filter").addEventListener("click", () => { page = 0; loadAndRender(); });
    // Selects/dates apply immediately; "Filtrar" stays for explicit refresh.
    for (const id of ["filter-status", "filter-user", "filter-from", "filter-to"]) {
        document.getElementById(id).addEventListener("change", () => { page = 0; loadAndRender(); });
    }
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
    // Scope to the current admin project + optional operator filter.
    if (currentProjectId != null) params.set("project_id", currentProjectId);
    const userFilter = document.getElementById("filter-user")?.value;
    if (userFilter) params.set("user_id", userFilter);
    params.set("sort_by", sortKey);
    params.set("sort_dir", sortDir);
    params.set("limit", PAGE_SIZE);
    params.set("offset", page * PAGE_SIZE);
    const target = document.getElementById("tiles-list");
    target.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando tiles...</div>`;
    const { json: tiles, headers: hd } = await apiGetWithHeaders(`/api/admin/tiles?${params}`);
    totalTiles = Number(hd.get("X-Total-Count") || tiles.length);
    document.querySelectorAll(".view-mode-toggle button").forEach(b => {
        b.classList.toggle("active", b.id === `view-${listView}`);
    });
    const count = document.getElementById("tiles-result-count");
    if (count) {
        count.textContent = "";
        count.append(h("strong", { class: "tabular", text: fmtInt(totalTiles) }),
            totalTiles === 1 ? " tile encontrado" : " tiles encontrados");
    }
    target.textContent = "";
    if (!tiles.length) {
        target.appendChild(card({}, emptyState({
            icon: "search", title: "Nenhum tile encontrado",
            text: "Ajuste a busca ou os filtros para ver resultados.",
        })));
    } else if (listView === "table") renderTable(target, tiles);
    else renderGrid(target, tiles);
    updateBulkBar();
    renderPager();
}

function setSort(key) {
    if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
    else { sortKey = key; sortDir = "asc"; }
    // Sort changes the order of every row, including ones not on the current
    // page — go back to page 0 so the user sees the new top of the list.
    page = 0;
    loadAndRender();
}

function renderPager() {
    for (const id of ["pager-top", "pager-bottom"]) {
        const el = document.getElementById(id);
        if (el) buildPagerInto(el, id === "pager-bottom");
    }
}

function buildPagerInto(el, withInfo) {
    el.textContent = "";
    const totalPages = Math.max(1, Math.ceil(totalTiles / PAGE_SIZE));
    if (totalPages <= 1 && !withInfo) return;
    const goTo = (p) => {
        const clamped = Math.max(0, Math.min(totalPages - 1, p));
        if (clamped === page) return;
        page = clamped;
        loadAndRender();
        document.getElementById("admin-content").scrollTop = 0;
    };
    const navBtn = (ic, disabled, onClick, title) => {
        const b = iconButton(ic, title, { onClick, variant: "pager-btn" });
        b.disabled = disabled;
        return b;
    };
    if (totalPages > 1) {
        el.append(navBtn("chevron-left", page === 0, () => goTo(page - 1), "Página anterior"));
        for (const item of pageWindow(page, totalPages)) {
            if (item === "…") {
                el.appendChild(h("span", { class: "pager-ellipsis", text: "…" }));
            } else {
                const b = h("button", { type: "button", class: `pager-btn pager-num${item === page ? " active" : ""}`,
                                        text: String(item + 1), "aria-current": item === page ? "page" : null });
                b.addEventListener("click", () => goTo(item));
                el.appendChild(b);
            }
        }
        el.append(navBtn("chevron-right", page >= totalPages - 1, () => goTo(page + 1), "Próxima página"));
    }
    if (withInfo) {
        const start = page * PAGE_SIZE + 1;
        const end = Math.min(totalTiles, (page + 1) * PAGE_SIZE);
        el.appendChild(h("span", { class: "pager-info",
            text: totalTiles === 0 ? "Nenhum tile" : `${fmtInt(start)}–${fmtInt(end)} de ${fmtInt(totalTiles)}` }));
    }
}

function updateBulkBar() {
    const bar = document.getElementById("bulk-bar");
    if (!bar) return;
    document.getElementById("bulk-count").textContent = selectedIds.size;
    bar.classList.toggle("hidden", selectedIds.size === 0);
}

function tileStatusChip(t) {
    if (t.paused_at) return statusChip(t.status, { paused: true, title: `Pausado em ${fmtDate(t.paused_at)}` });
    return statusChip(t.status, {
        title: t.status === "blocked" && t.blocked_from ? `Antes: ${statusLabel(t.blocked_from)}` : null,
    });
}

// Secondary actions → overflow menu entries (labels/icons per action key).
const _ROW_ACTION_META = {
    assign:   (t) => ({ label: t.status === "classified" ? "Atribuir revisor…" : "Atribuir operador…", icon: "user-plus", run: () => assignOne(t) }),
    rereview: (t) => ({ label: "Enviar para nova revisão", icon: "refresh-cw", run: () => reReviewOne(t.id) }),
    pause:    (t) => ({ label: "Pausar", icon: "circle-pause", run: () => adminPauseOne(t.id) }),
    unassign: (t) => ({ label: "Liberar operador", icon: "log-out", run: () => unassignOne(t.id) }),
    block:    (t) => ({ label: "Bloquear", icon: "lock", run: () => blockAction([t.id]) }),
    unblock:  (t) => ({ label: "Desbloquear", icon: "lock-open", run: () => blockAction([t.id], { unblock: true }) }),
};

function rowMenuItems(t) {
    return tileRowActions(t).map(k => {
        const m = _ROW_ACTION_META[k](t);
        return { label: m.label, icon: m.icon, onClick: m.run };
    });
}

function dateCell(timestamp, pendingLabel) {
    if (timestamp) {
        return h("span", { class: "date-cell", title: fmtDate(timestamp) },
            h("span", { text: fmtDate(timestamp).split(",")[0] || fmtDate(timestamp) }),
            h("span", { class: "dim", text: relativeTime(timestamp) }));
    }
    if (pendingLabel) return h("span", { class: "chip-text", text: pendingLabel });
    return h("span", { class: "dim", text: "—" });
}

function buildTableRow(t) {
    const tr = h("tr", { dataset: { id: t.id }, class: selectedIds.has(t.id) ? "selected" : null });
    const cb = h("input", { type: "checkbox", "aria-label": `Selecionar tile ${t.id}` });
    cb.checked = selectedIds.has(t.id);
    cb.addEventListener("change", () => toggleSelect(t.id, cb.checked, tr));
    const actions = h("div", { class: "row-actions" },
        button("Ver", { variant: "btn-sm", onClick: () => openViewer(t.id) }));
    const items = rowMenuItems(t);
    if (items.length) actions.appendChild(menuButton(() => rowMenuItems(t), `Ações do tile ${t.id}`));
    else actions.appendChild(h("span", { class: "row-actions-spacer" }));
    const multiProject = currentProjectId == null;
    tr.append(
        h("td", { class: "col-check" }, cb),
        h("td", { class: "num mono dim", text: `#${t.id}` }),
        h("td", {}, h("span", { class: "cell-strong", text: t.name }),
            multiProject && projectsById[t.project_id]
                ? h("span", { class: "cell-sub", text: projectsById[t.project_id].name }) : null),
        h("td", {}, tileStatusChip(t)),
        h("td", {}, userCell(classifierCell(t), { hint: t.status === "in_progress" && !t.classified_by_username ? "atribuído" : null })),
        h("td", {}, userCell(reviewerCell(t), { hint: t.status === "in_review" && !t.reviewed_by_username ? "atribuído" : null })),
        h("td", {}, dateCell(t.classified_at, t.status === "in_progress" ? "em andamento" : "")),
        h("td", {}, dateCell(t.reviewed_at, t.status === "in_review" ? "em revisão" : "")),
        h("td", { class: "col-actions" }, actions));
    tr.addEventListener("dblclick", () => openViewer(t.id));
    return tr;
}

function renderTable(root, tiles) {
    const cols = [
        ["id", "ID", true], ["name", "Nome"], ["status", "Status"],
        ["classified_by_username", "Classificado por"], ["reviewed_by_username", "Revisado por"],
        ["classified_at", "Classificado em"], ["reviewed_at", "Revisado em"],
    ];
    const checkAll = h("input", { type: "checkbox", id: "check-all", "aria-label": "Selecionar todos da página" });
    const table = h("table", { class: "table admin-table tiles-table" },
        h("thead", {}, h("tr", {},
            h("th", { class: "col-check" }, checkAll),
            cols.map(([k, l, num]) => sortHeader(l, { key: k, activeKey: sortKey, dir: sortDir, onSort: setSort, num })),
            h("th", { class: "col-actions", "aria-label": "Ações" }))));
    const tbody = h("tbody");
    for (const t of tiles) tbody.appendChild(buildTableRow(t));
    table.appendChild(tbody);
    root.appendChild(h("div", { class: "card card-flush" }, h("div", { class: "table-wrap admin-table-wrap" }, table)));
    checkAll.addEventListener("change", (ev) => {
        tbody.querySelectorAll("tr").forEach(tr => {
            const id = Number(tr.dataset.id);
            if (ev.target.checked) selectedIds.add(id); else selectedIds.delete(id);
            tr.classList.toggle("selected", ev.target.checked);
            tr.querySelector("input[type=checkbox]").checked = ev.target.checked;
        });
        updateBulkBar();
    });
}

// Raster tiles with paint: satellite underneath, colorized mask on top.
// Everything else: the server picks the best single thumbnail.
// Classification tiles: satellite only — the class goes in an HTML tag (the
// server-rendered badge can't draw accented names).
function _thumbLayers(t) {
    const kind = projectsById[t.project_id]?.kind || "raster";
    const painted = kind === "raster" && !["pending", "problem"].includes(t.status)
        && !(t.status === "blocked" && t.blocked_from === "pending");
    const classified = kind === "classification" && ["classified", "in_review", "reviewed"].includes(t.status);
    return { kind, painted, classified };
}

function classTag(classId, projectId) {
    const cls = (projectsById[projectId]?.classes || []).find(c => c.id === classId);
    const sw = h("span", { class: "viewer-swatch" });
    sw.style.background = cls?.color || "var(--border-strong)";
    return h("span", { class: "thumb-class" }, sw, h("span", { text: cls ? cls.name : `Classe #${classId}` }));
}

function buildGridCard(t) {
    const cardEl = h("div", {
        class: `thumb-card${selectedIds.has(t.id) ? " selected" : ""}`,
        dataset: { id: t.id }, tabIndex: 0, role: "button", "aria-label": `Abrir tile ${t.id}`,
    });
    // Selection checkbox overlaid top-left. Stops propagation so toggling it
    // doesn't also open the viewer. Shift+click on the card body still works.
    const cb = h("input", { type: "checkbox", class: "thumb-check", "aria-label": `Selecionar tile ${t.id}` });
    cb.checked = selectedIds.has(t.id);
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", () => toggleSelect(t.id, cb.checked, cardEl));
    const media = h("div", { class: "thumb-media thumb-skeleton" });
    const { kind, painted, classified } = _thumbLayers(t);
    const addImg = (url, cls) => apiGetBlob(url)
        .then(b => {
            const img = h("img", { alt: "", class: cls, draggable: "false" });
            img.src = URL.createObjectURL(b);
            img.onload = () => URL.revokeObjectURL(img.src);
            media.appendChild(img);
        });
    const loads = [];
    if (kind === "classification") {
        loads.push(addImg(`/api/admin/tiles/${t.id}/satellite-thumbnail?size=192`, "thumb-sat")
            .catch(() => addImg(`/api/admin/tiles/${t.id}/thumbnail?size=192`, "thumb-main")).catch(() => {}));
    } else {
        if (painted) loads.push(addImg(`/api/admin/tiles/${t.id}/satellite-thumbnail?size=192`, "thumb-sat").catch(() => {}));
        loads.push(addImg(`/api/admin/tiles/${t.id}/thumbnail?size=192`, painted ? "thumb-mask" : "thumb-main").catch(() => {}));
    }
    Promise.all(loads).finally(() => media.classList.remove("thumb-skeleton"));
    media.appendChild(cb);
    media.appendChild(h("span", { class: "thumb-status" }, tileStatusChip(t)));
    const whoName = t.reviewed_by_username || t.classified_by_username || t.assigned_to_username;
    const whoText =
        t.reviewed_by_username ? "revisado por" :
        t.classified_by_username ? "classificado por" :
        t.assigned_to_username ? "atribuído a" : "";
    const body = h("div", { class: "thumb-body" },
        h("div", { class: "thumb-title" },
            h("span", { class: "thumb-name", text: truncateName(t.name, 24), title: t.name }),
            h("span", { class: "thumb-id mono", text: `#${t.id}` })),
        whoName
            ? h("div", { class: "thumb-who" }, avatar(whoName, { size: "xs" }),
                h("span", { class: "dim", text: whoText }), h("span", { text: whoName }))
            : h("div", { class: "thumb-who dim", text: "Sem responsável" }));
    const items = rowMenuItems(t);
    if (items.length) {
        const mb = menuButton(() => rowMenuItems(t), `Ações do tile ${t.id}`);
        mb.classList.add("thumb-menu");
        media.appendChild(mb);
    }
    if (classified) {
        apiGet(`/api/tiles/${t.id}/classification`)
            .then(r => { if (r?.class_id != null) body.insertBefore(classTag(r.class_id, t.project_id), body.lastChild); })
            .catch(() => {});
    }
    cardEl.append(media, body);
    cardEl.addEventListener("click", (ev) => {
        if (ev.shiftKey) toggleSelect(t.id, !selectedIds.has(t.id), cardEl, true);
        else openViewer(t.id);
    });
    cardEl.addEventListener("keydown", (ev) => {
        if (ev.target !== cardEl) return;
        if (ev.key === "Enter") openViewer(t.id);
        if (ev.key === " ") { ev.preventDefault(); toggleSelect(t.id, !selectedIds.has(t.id), cardEl, true); }
    });
    return cardEl;
}

function renderGrid(root, tiles) {
    const grid = h("div", { class: "thumb-grid" });
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

function toggleSelect(id, on, el, syncCheckbox = false) {
    if (on) selectedIds.add(id); else selectedIds.delete(id);
    el?.classList.toggle("selected", on);
    if (syncCheckbox) {
        const cb = el?.querySelector("input[type=checkbox]");
        if (cb) cb.checked = on;
    }
    updateBulkBar();
}

function classifierCell(t) {
    return t.classified_by_username
        || (t.status === "in_progress" ? (t.assigned_to_username || "") : "");
}

function reviewerCell(t) {
    return t.reviewed_by_username
        || (t.status === "in_review" ? (t.assigned_to_username || "") : "");
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
// Membership is per-project: eligibility for assigning a tile (single or
// bulk) is computed from the project's member list — not from a global user
// flag. For `classified` tiles we keep only reviewers and exclude the
// classifiers of those tiles. Returns the `{id, username, project_role}[]`
// shape `promptAssign` consumes.
//
// `tiles` may be 1+ rows; mixed-project batches must be rejected by callers
// because they'd need different member lists.
async function _eligibleAssignees(members, tiles) {
    const hasReview = tiles.some(t => t.status === "classified");
    const classifierIds = new Set(
        tiles.filter(t => t.status === "classified" && t.classified_by)
             .map(t => t.classified_by),
    );
    return members
        .filter(m => m.active)
        .filter(m => !hasReview || (
            m.project_role === "reviewer" && !classifierIds.has(m.id)
        ))
        .map(m => ({ id: m.id, username: m.username, project_role: m.project_role }));
}

// Strings for the assign modal: title varies by single vs bulk, description
// changes when any tile in the batch needs a reviewer.
function _assignPromptStrings({ hasReview, count, singleTileId }) {
    const title = count === 1
        ? (hasReview
            ? `Atribuir revisão do tile #${singleTileId}`
            : `Atribuir tile #${singleTileId}`)
        : `Atribuir ${count} tile(s) a um usuário`;
    const description = hasReview
        ? (count === 1
            ? "Apenas revisores do projeto aparecem na lista. O classificador não pode revisar a própria classificação."
            : "Apenas revisores do projeto aparecem. Tiles vão para a fila pessoal como pausados; ao terminar o atual, o usuário recebe o próximo.")
        : (count === 1
            ? "Apenas membros do projeto aparecem na lista."
            : "Apenas membros do projeto aparecem. Tiles vão para a fila pessoal como pausados.");
    return { title, description };
}

async function assignOne(tile) {
    const members = await apiGet(`/api/admin/projects/${tile.project_id}/members`);
    const eligible = await _eligibleAssignees(members, [tile]);
    const hasReview = tile.status === "classified";
    if (!eligible.length) {
        showToast(hasReview
            ? "Sem revisores no projeto (ou todos classificaram este tile)."
            : "Sem membros no projeto.", "error");
        return;
    }
    const r = await promptAssign({
        ..._assignPromptStrings({ hasReview, count: 1, singleTileId: tile.id }),
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
    if (await blockAction([...selectedIds])) { selectedIds.clear(); updateBulkBar(); }
}

async function bulkUnblock() {
    if (!selectedIds.size) return;
    if (await blockAction([...selectedIds], { unblock: true })) { selectedIds.clear(); updateBulkBar(); }
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
    const tileRows = await Promise.all(ids.map(id => apiGet(`/api/tiles/${id}`)));
    // Backend rejects any tile that isn't pending or classified — filter up
    // front so we can build the eligible list and give a clean error.
    const bad = tileRows.filter(t => t.status !== "pending" && t.status !== "classified");
    if (bad.length) {
        showToast(
            `Seleção inválida: ${bad.length} tile(s) não estão em pending/classified.`,
            "error",
        );
        return;
    }
    // The batch must belong to a single project for the membership lookup —
    // bulk-assigning across projects would mean mixing eligible lists.
    const projectIds = new Set(tileRows.map(t => t.project_id));
    if (projectIds.size > 1) {
        showToast("Seleção mistura projetos. Filtre por projeto antes de atribuir.", "error");
        return;
    }
    const pid = [...projectIds][0];
    const members = await apiGet(`/api/admin/projects/${pid}/members`);
    const eligible = await _eligibleAssignees(members, tileRows);
    const hasReview = tileRows.some(t => t.status === "classified");
    if (!eligible.length) {
        showToast(hasReview
            ? "Sem revisores no projeto (ou todos classificaram algum tile do lote)."
            : "Sem membros ativos no projeto.", "error");
        return;
    }
    const r = await promptAssign({
        ..._assignPromptStrings({ hasReview, count: ids.length }),
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

// ---------- Tile viewer (modal + map side panel) ----------

// Render the tile info (imagery + mask, status, metadata, actions, history)
// into `targetBody`. Used by both the modal viewer (two columns) and the map
// side panel (stacked). Owns `viewerMap` lifecycle — disposes the previous
// map and installs a new one in the rendered `viewer-map-${tileId}` div.
// `reload` re-renders the same view after an action (Re-revisar, Bloquear…).
async function renderTileInfo(targetBody, tileId, reload, { layout = "wide", onLoaded } = {}) {
    disposeViewerMap();
    targetBody.innerHTML = `
        <div class="tile-info tile-info-${layout} is-loading">
            <div class="tile-info-media"><div class="viewer-skeleton"></div></div>
            <div class="tile-info-side"><div class="loading-text"><span class="loading"></span> Carregando tile...</div></div>
        </div>`;
    try {
        // Tile first so we can decide whether to fetch the raster mask:
        // classification doesn't have a per-pixel PNG and the /image endpoint
        // isn't meant for it.
        const t = await apiGet(`/api/tiles/${tileId}`);
        const proj = projectsById[t.project_id] || {};
        const kind = proj.kind || "raster";
        // The mask exists only for raster projects, and only after the operator
        // has painted something. `pending` and `problem` are wiped/empty.
        const hasMask = kind === "raster" && t.status !== "pending" && t.status !== "problem";
        const isClassification = kind === "classification";
        const [history, blob, clsBody, projDetail] = await Promise.all([
            apiGet(`/api/tiles/${tileId}/history`),
            hasMask ? apiGetBlob(`/api/tiles/${tileId}/image`) : Promise.resolve(null),
            isClassification
                ? apiGet(`/api/tiles/${tileId}/classification`).catch(() => null)
                : Promise.resolve(null),
            !proj.classes ? apiGet(`/api/projects/${t.project_id}`).catch(() => null) : Promise.resolve(null),
        ]);
        if (projDetail) projectsById[t.project_id] = { ...projectsById[t.project_id], ...projDetail };
        const projClasses = projectsById[t.project_id]?.classes || [];
        const palette = Object.fromEntries(projClasses.map(c => [c.id, c]));
        const tilePx = projectsById[t.project_id]?.tile_px || 256;
        const primaryUrl = projectsById[t.project_id]?.layers?.primary?.url
            || (t.project_id === currentProjectId ? tileserverUrl : "");
        const primaryMax = projectsById[t.project_id]?.layers?.primary?.max_zoom ?? tileserverMaxZoom;
        const img = blob ? await blobToImage(blob) : null;
        onLoaded?.(t);

        // ---- Media column ----
        const stack = h("div", { class: "viewer-stack" });
        const mapDiv = h("div", { id: `viewer-map-${tileId}`, class: "viewer-map" });
        stack.appendChild(mapDiv);
        let maskCnv = null;
        if (hasMask) {
            maskCnv = h("canvas", { width: 512, height: 512 });
            stack.appendChild(maskCnv);
        }
        if (!primaryUrl) {
            stack.appendChild(h("div", { class: "viewer-noimage" }, icon("image"),
                h("span", { text: "Sem imagem de satélite configurada" })));
        }
        const frame = h("div", { class: "viewer-frame" }, stack);
        if (hasMask) {
            const maskLabel = h("span", { text: "Máscara" });
            const maskBtn = h("button", {
                type: "button", class: "viewer-tool-btn active", "aria-pressed": "true",
                title: "Mostrar/esconder a máscara sobre a imagem",
            }, icon("eye"), maskLabel);
            maskBtn.addEventListener("click", () => {
                // Toggle the canvas only — the satellite map keeps rendering
                // so admins can inspect the imagery beneath.
                const hidden = stack.classList.toggle("mask-hidden");
                maskBtn.classList.toggle("active", !hidden);
                maskBtn.setAttribute("aria-pressed", String(!hidden));
                maskBtn.querySelector("use")?.setAttribute("href",
                    `/static/vendor/lucide/icons.svg#i-${hidden ? "eye-off" : "eye"}`);
            });
            frame.appendChild(h("div", { class: "viewer-stack-tools" }, maskBtn));
        }
        const media = h("div", { class: "tile-info-media" }, frame);
        const legendHost = h("div", { class: "viewer-legend-host" });
        if (hasMask) media.appendChild(legendHost);
        // Timestamps / notes come from the action log (the tile payload
        // carries only the actors).
        const lastLog = (action) => [...history].reverse().find(e => e.action === action);
        const classifiedAt = lastLog("classify")?.created_at;
        const reviewedAt = lastLog("review")?.created_at;
        const problemNote = t.status === "problem" ? lastLog("report_problem")?.detail : null;

        // ---- Side column ----
        const side = h("div", { class: "tile-info-side" });
        const statusRow = h("div", { class: "viewer-status-row" }, tileStatusChip(t));
        if (t.status === "blocked" && t.blocked_from) {
            statusRow.appendChild(h("span", { class: "muted", text: `antes: ${statusLabel(t.blocked_from)}` }));
        }
        statusRow.appendChild(h("span", { class: "chip", text: kindLabel(kind) }));
        side.appendChild(statusRow);
        if (isClassification) {
            side.appendChild(classificationLine(clsBody?.class_id ?? null, projClasses));
        }
        if (problemNote) {
            side.appendChild(h("div", { class: "viewer-note" }, icon("message-square-text"),
                h("div", {}, h("strong", { text: "Problema reportado" }), h("p", { text: problemNote }))));
        }
        const meta = h("dl", { class: "viewer-meta" });
        const addMeta = (label, value) => {
            if (value == null || value === "") return;
            meta.append(h("dt", { text: label }), h("dd", {}, value));
        };
        addMeta("Projeto", proj.name || `#${t.project_id}`);
        addMeta("Nome", h("span", { class: "mono", text: t.name }));
        if (t.classified_by_username) {
            addMeta("Classificado", h("span", { class: "meta-user" }, userCell(t.classified_by_username),
                classifiedAt ? h("span", { class: "dim", text: fmtDate(classifiedAt) }) : null));
        }
        if (t.reviewed_by_username) {
            addMeta("Revisado", h("span", { class: "meta-user" }, userCell(t.reviewed_by_username),
                reviewedAt ? h("span", { class: "dim", text: fmtDate(reviewedAt) }) : null));
        }
        if (t.assigned_to_username && (t.status === "in_progress" || t.status === "in_review")) {
            addMeta("Com", userCell(t.assigned_to_username, { hint: t.paused_at ? "pausado" : "trabalhando" }));
        }
        addMeta("Centro", h("span", { class: "mono dim",
            text: `${((t.bbox_south + t.bbox_north) / 2).toFixed(5)}, ${((t.bbox_west + t.bbox_east) / 2).toFixed(5)}` }));
        side.appendChild(meta);

        const actions = h("div", { class: "viewer-actions" });
        if (t.status === "pending" || t.status === "classified") {
            actions.appendChild(button(t.status === "classified" ? "Atribuir revisor" : "Atribuir operador",
                { icon: "user-plus", variant: "primary btn-sm", onClick: () => assignOne(t) }));
        }
        if (t.status === "reviewed") {
            actions.appendChild(button("Re-revisar", { icon: "refresh-cw", variant: "btn-sm", onClick: async () => {
                await reReviewOne(t.id);
                reload(t.id);
            } }));
        }
        if (isBlockable(t)) {
            actions.appendChild(button("Bloquear", { icon: "lock", variant: "btn-sm", onClick: async () => {
                if (await blockAction([t.id], { reload: false })) reload(t.id);
            } }));
        } else if (t.status === "blocked") {
            actions.appendChild(button("Desbloquear", { icon: "lock-open", variant: "btn-sm", onClick: async () => {
                if (await blockAction([t.id], { unblock: true, reload: false })) reload(t.id);
            } }));
        }
        if (actions.children.length) side.appendChild(actions);

        if (history.length) {
            const list = h("ol", { class: "timeline" });
            for (const ev of [...history].reverse()) {
                list.appendChild(h("li", { class: `timeline-item tl-${actionTone(ev.action)}` },
                    h("span", { class: "timeline-dot" }),
                    h("div", { class: "timeline-body" },
                        h("div", { class: "timeline-line" },
                            h("strong", { text: actionLabel(ev.action) }),
                            h("span", { class: "muted", text: ` · ${ev.username || "?"}` })),
                        ev.detail && !/^[{[]/.test(ev.detail) && !["queue", "auto", "admin"].includes(ev.detail)
                            ? h("div", { class: "timeline-detail", text: ev.detail }) : null,
                        h("time", { class: "timeline-when", title: fmtDate(ev.created_at),
                                    text: `${fmtDate(ev.created_at)} · ${relativeTime(ev.created_at)}` }))));
            }
            side.appendChild(h("div", { class: "viewer-section" },
                h("h4", { class: "viewer-section-title" }, icon("clock", "icon-sm"),
                    `Histórico`, h("span", { class: "badge badge-soft", text: String(history.length) })),
                list));
        }

        targetBody.textContent = "";
        targetBody.appendChild(h("div", { class: `tile-info tile-info-${layout}` }, media, side));

        setTimeout(() => {
            // Bail if the host cleared this body (closed modal / panel, or
            // requested a different tile) before this tick fires — otherwise
            // we'd leak a WebGL context into an orphaned div.
            if (!document.getElementById(`viewer-map-${tileId}`) || !primaryUrl) return;
            disposeViewerMap();
            viewerMap = createLockedMap(`viewer-map-${tileId}`, primaryUrl,
                [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north], primaryMax);
        }, 0);
        if (hasMask && img && maskCnv) {
            const n = tilePx;
            const tmp = document.createElement("canvas");
            tmp.width = n; tmp.height = n;
            const tctx = tmp.getContext("2d");
            tctx.drawImage(img, 0, 0, n, n);
            const data = tctx.getImageData(0, 0, n, n);
            const out = tctx.createImageData(n, n);
            const lut = new Map(projClasses.map(c => [c.id, hexToRgb(c.color)]));
            const counts = {};
            for (let i = 0; i < n * n; i++) {
                const v = data.data[i * 4];
                if (v !== 255) counts[v] = (counts[v] || 0) + 1;
                const rgb = lut.get(v);
                if (rgb) {
                    out.data[i * 4] = rgb[0]; out.data[i * 4 + 1] = rgb[1];
                    out.data[i * 4 + 2] = rgb[2]; out.data[i * 4 + 3] = 120;
                }
            }
            tctx.putImageData(out, 0, 0);
            const mctx = maskCnv.getContext("2d");
            mctx.imageSmoothingEnabled = false;
            mctx.drawImage(tmp, 0, 0, 512, 512);
            const filled = Object.values(counts).reduce((a, b) => a + b, 0);
            legendHost.appendChild(classCountsLegend(counts, palette, n * n - filled));
        }
    } catch (e) {
        renderError(targetBody, e);
    }
}

// Class share bar + legend under the image, from the decoded mask.
// `counts` = {classId: pixels}; `unfilled` pixels (255) get their own slot.
function classCountsLegend(counts, palette, unfilled = 0) {
    const entries = Object.entries(counts).map(([k, v]) => [Number(k), Number(v)]).filter(([, v]) => v > 0);
    if (unfilled > 0) entries.push([255, unfilled]);
    const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
    entries.sort((a, b) => b[1] - a[1]);
    const bar = h("div", { class: "viewer-mix", role: "img",
        "aria-label": entries.map(([k, v]) => `${palette[k]?.name || `#${k}`}: ${Math.round(100 * v / total)}%`).join(", ") });
    const legend = h("div", { class: "viewer-legend" });
    for (const [k, v] of entries) {
        const c = k === 255 ? { name: "Não preenchido", color: "var(--bg-4)" } : palette[k];
        const seg = h("span", { class: "viewer-mix-seg", title: `${c?.name || `#${k}`}: ${Math.round(100 * v / total)}%` });
        seg.style.flexGrow = String(v);
        seg.style.background = c?.color || "var(--border-strong)";
        bar.appendChild(seg);
        legend.appendChild(h("span", { class: "viewer-legend-item" },
            h("span", { class: "viewer-swatch", style: { background: c?.color || "var(--border-strong)" } }),
            h("span", { text: c?.name || `#${k}` }),
            h("span", { class: "dim tabular", text: `${Math.round(100 * v / total)}%` })));
    }
    return h("div", { class: "viewer-mix-wrap" }, bar, legend);
}

async function openViewer(tileId) {
    const modal = document.getElementById("modal-view-tile");
    const body = document.getElementById("view-tile-body");
    document.getElementById("view-tile-title").textContent = `Tile #${tileId}`;
    const sub = document.getElementById("view-tile-subtitle");
    sub.textContent = "";
    modal.classList.remove("hidden");
    await renderTileInfo(body, tileId, openViewer, {
        layout: "wide",
        onLoaded: (t) => {
            sub.textContent = `${t.name} · ${projectsById[t.project_id]?.name || `projeto #${t.project_id}`}`;
        },
    });
}

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
    await renderTileInfo(els.body, tileId, (id) => { delete els.panel.dataset.tileId; showTileInPanel(id); },
                         { layout: "stack" });
}

function closeMapPanel() {
    const els = getMapPanelEls();
    if (!els) return;
    disposeViewerMap();
    delete els.panel.dataset.tileId;
    els.panel.classList.add("empty");
    els.title.textContent = "Detalhes do tile";
    els.body.textContent = "";
}

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

// ---------- Map tab ----------

// Map statuses keyed on `display_status` (paused is virtual: in_progress|
// in_review with paused_at). Colors come from the --st-* tokens so the map
// legend matches the status chips in both themes.
const MAP_STATUSES = STATUS_ORDER;
function mapStatusColors() {
    return Object.fromEntries(MAP_STATUSES.map(s => [s, cssVar(`--st-${s}`) || cssVar("--text-dim")]));
}
// Pending tiles are the bulk of a fresh project — keep them light so the
// imagery and the finished work stand out.
const MAP_FILL_OPACITY = ["match", ["get", "display_status"], "pending", 0.18, "reviewed", 0.4, 0.55];
function mapColorExpr() {
    const expr = ["match", ["get", "display_status"]];
    for (const [status, color] of Object.entries(mapStatusColors())) expr.push(status, color);
    expr.push(cssVar("--text-dim"));
    return expr;
}

// Basemap follows the theme (Esri light/dark gray canvas).
const BASEMAP_URLS = {
    light: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    dark: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
};
const currentThemeName = () =>
    document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";

// Re-apply token-driven paint after a theme switch (MapLibre can't read CSS).
function applyMapTheme() {
    if (!mapView || !mapView.getLayer("tiles-fill")) return;
    const expr = mapColorExpr();
    mapView.setPaintProperty("tiles-fill", "fill-color", expr);
    mapView.setPaintProperty("tiles-outline", "line-color", expr);
    mapView.setPaintProperty("tiles-dot", "circle-color", expr);
    mapView.setPaintProperty("tiles-dot", "circle-stroke-color", cssVar("--bg-1"));
    mapView.setPaintProperty("tiles-selected-outline", "line-color", cssVar("--text"));
    mapView.setPaintProperty("tiles-selected-glow", "circle-stroke-color", cssVar("--text"));
    mapView.getSource("basemap")?.setTiles([BASEMAP_URLS[currentThemeName()]]);
    const legend = document.getElementById("map-legend");
    const colors = mapStatusColors();
    legend?.querySelectorAll(".map-legend-item").forEach(item => {
        const sw = item.querySelector(".map-legend-swatch");
        if (sw) sw.style.background = colors[item.dataset.status];
    });
}

async function renderMap(root) {
    const tiles = await apiGet(withProjectParam("/api/admin/tiles/map", currentProjectId));
    if (!root.isConnected) return;  // tab changed while loading
    mapSelectedIds = new Set();
    mapTilePropsById = new Map();
    mapRectSelectActive = false;
    root.innerHTML = `
        <div class="map-tab">
            <div class="map-tab-container" id="admin-map">
                <div id="map-rect-overlay" class="map-rect-overlay hidden"></div>
            </div>
            <div class="map-float map-float-tools" id="map-toolbar" role="toolbar" aria-label="Ferramentas do mapa">
                <button id="map-tool-rect" class="map-tool-btn" type="button"
                        title="Seleção retangular: arraste sobre o mapa. Shift soma à seleção, Alt remove. Esc cancela."></button>
                <span class="map-float-sep"></span>
                <button id="map-tool-sat" class="map-tool-btn" type="button"
                        title="Sobrepõe a imagem de satélite primária do projeto."></button>
                <button id="map-tool-classifs" class="map-tool-btn" type="button"
                        title="Sobrepõe as classificações dos tiles classificados, em revisão e revisados."></button>
            </div>
            <div class="map-float map-float-legend">
                <div class="map-float-title">Status <span class="dim" id="map-total"></span></div>
                <div class="map-tab-legend" id="map-legend"></div>
                <div class="map-class-legend-floating hidden" id="map-class-legend"></div>
            </div>
            <div class="map-selection-bar hidden" id="map-sel-info" role="toolbar" aria-label="Ações na seleção">
                <span class="map-sel-count"><b id="map-sel-count">0</b> selecionado(s)</span>
                <span class="bulk-sep"></span>
                <button id="map-bulk-assign" class="ghost btn-sm" type="button" disabled>Atribuir operador</button>
                <button id="map-bulk-unassign" class="ghost btn-sm" type="button" disabled>Liberar operador</button>
                <button id="map-bulk-rereview" class="ghost btn-sm" type="button" disabled>Re-revisar selecionados</button>
                <button id="map-bulk-block" class="ghost btn-sm" type="button" disabled>Bloquear selecionados</button>
                <button id="map-bulk-unblock" class="ghost btn-sm" type="button" disabled>Desbloquear selecionados</button>
                <span class="bulk-sep"></span>
                <button id="map-sel-clear" class="ghost btn-sm" type="button" disabled>Limpar seleção</button>
            </div>
            <aside class="map-panel map-float empty" id="map-panel" aria-label="Detalhes do tile">
                <header class="map-panel-header">
                    <h3 id="map-panel-title"></h3>
                    <button id="map-panel-close" class="ghost icon-btn btn-sm map-panel-close" type="button"
                            title="Fechar painel" aria-label="Fechar"></button>
                </header>
                <div class="map-panel-body" id="map-panel-body"></div>
            </aside>
        </div>
    `;
    // Static labels/icons built with DOM APIs (no data in them, but keeps
    // every icon going through the shared helper).
    const label = (id, ic, text) => {
        const b = document.getElementById(id);
        b.append(icon(ic), h("span", { text }));
    };
    label("map-tool-rect", "square-dashed-mouse-pointer", "Selecionar área");
    label("map-tool-sat", "satellite", "Satélite");
    label("map-tool-classifs", "palette", "Classificações");
    document.getElementById("map-panel-close").appendChild(icon("x"));
    document.getElementById("map-panel-close").addEventListener("click", closeMapPanel);
    closeMapPanel();  // initialize empty state from the single source of truth

    // Count per display status for the legend.
    const counts = {};
    for (const t of tiles) {
        const ds = t.paused_at ? "paused" : t.status;
        counts[ds] = (counts[ds] || 0) + 1;
    }
    document.getElementById("map-total").textContent = `· ${fmtInt(tiles.length)} tiles`;
    // All statuses enabled by default. Clicking a legend item toggles — the
    // filter on the three tile layers is recomputed from this set.
    const enabledStatuses = new Set(MAP_STATUSES);
    const legend = document.getElementById("map-legend");
    const legendButtons = new Map();
    const colors = mapStatusColors();
    for (const status of MAP_STATUSES) {
        const item = h("button", { type: "button", class: "map-legend-item active", dataset: { status },
                                   title: "Mostrar/ocultar no mapa", "aria-pressed": "true" },
            h("span", { class: "map-legend-swatch", style: { background: colors[status] } }),
            h("span", { class: "map-legend-label", text: statusLabel(status) }),
            h("span", { class: "map-legend-count tabular", text: fmtInt(counts[status] || 0) }));
        if (!counts[status]) item.classList.add("is-zero");
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
        const on = enabledStatuses.has(status);
        legendButtons.get(status).classList.toggle("active", on);
        legendButtons.get(status).setAttribute("aria-pressed", String(on));
        applyFilter();
    };

    if (!tiles.length) {
        const host = document.getElementById("admin-map");
        host.textContent = "";
        host.appendChild(emptyState({ icon: "map", title: "Nenhum tile neste escopo",
            text: "Importe tiles ou escolha outro projeto no seletor acima." }));
        document.getElementById("map-toolbar").classList.add("hidden");
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

    const colorExpr = mapColorExpr();
    mapView = new maplibregl.Map({
        container: "admin-map",
        style: {
            version: 8,
            sources: {
                basemap: {
                    type: "raster",
                    tiles: [BASEMAP_URLS[currentThemeName()]],
                    tileSize: 256,
                    attribution: "Tiles © Esri",
                    maxzoom: 16,
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
                    paint: { "fill-color": colorExpr, "fill-opacity": MAP_FILL_OPACITY },
                },
                {
                    id: "tiles-outline", type: "line", source: "tiles",
                    paint: { "line-color": colorExpr, "line-width": 1.5, "line-opacity": 0.9 },
                },
                {
                    // Circle marker per tile so they stay visible when the
                    // polygon is sub-pixel at wide zoom levels.
                    id: "tiles-dot", type: "circle", source: "tile-points",
                    maxzoom: 11,
                    paint: {
                        "circle-color": colorExpr,
                        "circle-radius": 4,
                        "circle-stroke-color": cssVar("--bg-1"),
                        "circle-stroke-width": 1.5,
                    },
                },
                {
                    // Selection highlight. Filter is updated by
                    // updateMapSelectionHighlight().
                    id: "tiles-selected-outline", type: "line", source: "tiles",
                    paint: {
                        "line-color": cssVar("--text"),
                        "line-width": 3,
                        "line-opacity": 0.95,
                    },
                    filter: ["in", ["get", "id"], ["literal", []]],
                },
                {
                    id: "tiles-selected-glow", type: "circle", source: "tile-points",
                    maxzoom: 11,
                    paint: {
                        "circle-opacity": 0,
                        "circle-radius": 8,
                        "circle-stroke-color": cssVar("--text"),
                        "circle-stroke-width": 2,
                    },
                    filter: ["in", ["get", "id"], ["literal", []]],
                },
            ],
        },
        bounds: [[w, s], [e, n]],
        fitBoundsOptions: { padding: { top: 70, bottom: 70, left: 260, right: 60 }, animate: false, maxZoom: 15 },
        transformRequest: tileTransformRequest,
        attributionControl: { compact: true },
    });
    mapView.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-left");

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
            // Any click ends the prior selection — admin's intent has moved
            // on. Cleared before showTileInPanel so the side panel opens
            // without leftover outlines.
            if (mapSelectedIds.size) clearMapSelection();
            const f = ev.features && ev.features[0];
            if (!f) return;
            showTileInPanel(f.properties.id);
        });
    }
    // Background click (no tile hit): also clears the selection.
    mapView.on("click", (ev) => {
        if (mapRectSelectActive) return;
        const feats = mapView.queryRenderedFeatures(ev.point, {
            layers: ["tiles-fill", "tiles-outline", "tiles-dot"],
        });
        if (feats.length) return;  // layer-scoped handler above takes over
        if (mapSelectedIds.size) clearMapSelection();
    });

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
    // Projects without a primary mbtiles URL get the `data:,` source fallback,
    // which 404s every tile request — there's nothing to toggle into. Hide
    // the button instead of inviting clicks that produce noisy console errors.
    if (!tileserverUrl) {
        btn.classList.add("hidden");
        return;
    }
    btn.classList.remove("hidden");
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
    if (!currentProjectId) return;
    // Cache-busting param so toggling off then on re-fetches anything the
    // browser cached during the previous session. Server-side MBTiles cache
    // is unaffected.
    const stamp = Date.now();
    mapView.addSource("mask-overlay", {
        type: "raster",
        tiles: [`/api/admin/mask-tiles/${currentProjectId}/{z}/{x}/{y}.png?t=${stamp}`],
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

// Force the overlay to re-fetch from the server after a mutation that the
// per-tile invalidation hooks already cleared server-side. MapLibre keeps its
// own in-memory tile cache and the browser may hold the response up to 60s
// (`Cache-Control` from /mask-tiles), so without a remove+add the admin sees
// stale colors until the source is rebuilt for some other reason (toggle,
// project switch, tab change). Silent no-op when the overlay is off.
function refreshClassOverlayIfActive() {
    if (!mapView || !mapView.getSource("mask-overlay")) return;
    removeClassOverlayLayer();
    addClassOverlayLayer();
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
    // Polygons fade — but don't disappear — when the overlay is on so the
    // classification colors come through while the status cue stays visible
    // (matters for tiles that render transparent while they have no
    // classification yet). visibility=none would also drop the layer from
    // queryRenderedFeatures, breaking rectangle-select.
    if (mapView && mapView.getLayer("tiles-fill")) {
        mapView.setPaintProperty("tiles-fill", "fill-opacity", on ? 0.15 : MAP_FILL_OPACITY);
    }
}

function wireClassOverlayToggle() {
    const btn = document.getElementById("map-tool-classifs");
    if (!btn || !mapView) return;
    // The overlay is per-project (palette + MBTiles cache live under one id),
    // so it can't render across projects — hide the toggle entirely in the
    // cross-project view instead of letting clicks no-op silently.
    if (!currentProjectId) {
        btn.classList.add("hidden");
        return;
    }
    btn.classList.remove("hidden");
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

    // All listeners below share the same AbortSignal so a tab switch (which
    // aborts the controller from selectTab) tears them down in one shot —
    // previously they accumulated on `window`/`document` every time the map
    // re-rendered.
    if (mapEventsAbort) mapEventsAbort.abort();
    mapEventsAbort = new AbortController();
    const signal = mapEventsAbort.signal;

    // Esc clears tool/selection.
    const onKey = (ev) => {
        if (currentTab !== "map") return;
        if (ev.key === "Escape") {
            if (mapRectSelectActive) setRectSelectActive(false);
            else if (mapSelectedIds.size) clearMapSelection();
        }
    };
    document.addEventListener("keydown", onKey, { signal });

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
    }, { signal });

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
    }, { signal });

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
        // One gesture = one selection. Auto-disable the tool so the next map
        // interaction is pan/zoom by default; admins doing back-to-back
        // selections can re-arm with the toolbar button or Shift+drag (handled
        // inside containerPoint's modifier resolution).
        setRectSelectActive(false);
    }, { signal });
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
    refreshClassOverlayIfActive();
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
    const tileRows = await Promise.all(
        eligibleIds.map(id => apiGet(`/api/tiles/${id}`))
    );
    // Re-check status against fresh data — the cached props snapshot may be
    // stale after other admins acted concurrently.
    const fresh = tileRows.filter(t => t.status === "pending" || t.status === "classified");
    if (!fresh.length) {
        showToast("Os tiles selecionados mudaram de status. Recarregue o mapa.", "error");
        return;
    }
    // Map view is always scoped to one project — all rows share project_id.
    const pid = fresh[0].project_id;
    const members = await apiGet(`/api/admin/projects/${pid}/members`);
    const eligibleUsers = await _eligibleAssignees(members, fresh);
    const hasReview = fresh.some(t => t.status === "classified");
    if (!eligibleUsers.length) {
        showToast(hasReview
            ? "Sem revisores no projeto (ou todos classificaram algum tile do lote)."
            : "Sem membros no projeto.", "error");
        return;
    }
    const baseStrings = _assignPromptStrings({ hasReview, count: fresh.length });
    const r = await promptAssign({
        title: baseStrings.title,
        description: baseStrings.description
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
    refreshClassOverlayIfActive();
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
    refreshClassOverlayIfActive();
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
    refreshClassOverlayIfActive();
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
    refreshClassOverlayIfActive();
    clearMapSelection();
}

// ---------- Problemas ----------

async function renderProblems(root) {
    const problems = await apiGet(withProjectParam("/api/admin/tiles/problems", currentProjectId));
    if (!root.isConnected) return;  // tab changed while loading
    root.textContent = "";
    if (!problems.length) {
        root.appendChild(card({}, emptyState({
            icon: "party-popper", title: "Nenhum problema reportado",
            text: currentProjectId == null
                ? "Nenhum tile de nenhum projeto foi marcado com problema."
                : "Nenhum tile deste projeto foi marcado com problema.",
        })));
        return;
    }
    const projectsInvolved = new Set(problems.map(p => p.project_id)).size;
    const table = h("table", { class: "table admin-table problems-table" },
        h("thead", {}, h("tr", {},
            h("th", { class: "num", text: "ID" }), h("th", { text: "Tile" }),
            h("th", { text: "Problema" }), h("th", { text: "Reportado" }),
            h("th", { class: "col-actions", "aria-label": "Ações" }))));
    const tbody = h("tbody");
    for (const p of problems) {
        // Column "Projeto" folds under the tile name — most useful in the
        // cross-project view, harmless when filtered.
        const projectName = p.project_name || projectsById[p.project_id]?.name || `#${p.project_id}`;
        const actions = h("div", { class: "row-actions" },
            button("Ver", { variant: "btn-sm", onClick: () => openViewer(p.id) }),
            button("Resetar", { variant: "btn-sm", icon: "rotate-ccw", onClick: () => resetOne(p.id),
                                title: "Volta para pendente, apagando o trabalho" }),
            iconButton("trash-2", "Excluir tile", { variant: "ghost danger", onClick: () => deleteOne(p.id, p.name) }));
        tbody.appendChild(h("tr", { dataset: { id: p.id } },
            h("td", { class: "num mono dim", text: `#${p.id}` }),
            h("td", {}, h("span", { class: "cell-strong", text: p.name }), h("span", { class: "cell-sub", text: projectName })),
            h("td", { class: "problem-note-cell" },
                h("span", { class: "problem-note" }, icon("message-square-text", "icon-sm"),
                    h("span", { text: p.problem_note || "Sem descrição" }))),
            h("td", {}, h("span", { class: "date-cell", title: fmtDate(p.reported_at) },
                h("span", { text: relativeTime(p.reported_at) || "—" }),
                h("span", { class: "dim", text: fmtDate(p.reported_at) }))),
            h("td", { class: "col-actions" }, actions)));
    }
    table.appendChild(tbody);
    root.append(
        h("div", { class: "callout callout-err" }, icon("triangle-alert"),
            h("div", {},
                h("strong", { text: `${fmtInt(problems.length)} ${problems.length === 1 ? "tile aguarda" : "tiles aguardam"} decisão` }),
                h("p", { text: `Em ${projectsInvolved} projeto(s). Resetar devolve o tile à fila como pendente; excluir remove o tile e seu histórico.` }))),
        h("div", { class: "card card-flush" }, h("div", { class: "table-wrap" }, table)));
}

// ---------- Usuários ----------

// User-level role labels (just two: regular user vs admin). "Revisor" is no
// longer a property here — it lives in project membership.
const _ROLE_LABELS = { operator: "Usuário", admin: "Administrador" };
const _roleLabel = (r) => _ROLE_LABELS[r] || r;

// Sort state survives re-renders so toggling active / changing role keeps the
// admin's chosen ordering. Default: id ascending (creation order).
let _usersSortKey = "id";
let _usersSortDir = "asc";

const _USER_COLUMNS = [
    { key: "username",   label: "Usuário",   accessor: u => u.username,              type: "str" },
    { key: "role",       label: "Papel",     accessor: u => _roleLabel(u.role),      type: "str" },
    { key: "active",     label: "Acesso",    accessor: u => (u.active ? 1 : 0),      type: "num" },
    { key: "created_at", label: "Criado em", accessor: u => u.created_at || "",      type: "str" },
];

function _sortUsers(rows) {
    const col = _USER_COLUMNS.find(c => c.key === _usersSortKey)
        || { accessor: u => u.id, type: "num" };
    const dir = _usersSortDir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
        const va = col.accessor(a);
        const vb = col.accessor(b);
        if (col.type === "num") return ((va || 0) - (vb || 0)) * dir;
        return String(va).localeCompare(String(vb), "pt-BR") * dir;
    });
}

async function renderUsers(root) {
    const users = await apiGet("/api/admin/users");
    if (!root.isConnected) return;  // tab changed while loading
    document.getElementById("admin-page-actions").appendChild(
        button("Novo usuário", { id: "btn-new-user", icon: "user-plus", variant: "primary", onClick: openCreateUserModal }));
    const admins = users.filter(u => u.role === "admin").length;
    const active = users.filter(u => u.active).length;
    root.textContent = "";
    root.append(
        h("div", { class: "kpi-grid kpi-grid-3" },
            kpi({ icon: "users", label: "Usuários", value: fmtInt(users.length), sub: "contas cadastradas", tone: "neutral" }),
            kpi({ icon: "user-check", label: "Ativos", value: fmtInt(active),
                  sub: users.length - active ? `${fmtInt(users.length - active)} desativado(s)` : "todos com acesso", tone: "ok" }),
            kpi({ icon: "shield", label: "Administradores", value: fmtInt(admins), sub: "acesso ao painel", tone: "accent" })),
        h("div", { id: "users-table-host", class: "card card-flush" }));
    _drawUsersTable(users);
}

function _drawUsersTable(users) {
    const host = document.getElementById("users-table-host");
    if (!host) return;
    host.textContent = "";
    const onSort = (key) => {
        if (_usersSortKey === key) _usersSortDir = _usersSortDir === "asc" ? "desc" : "asc";
        else {
            _usersSortKey = key;
            _usersSortDir = _USER_COLUMNS.find(c => c.key === key)?.type === "num" ? "desc" : "asc";
        }
        _drawUsersTable(users);
    };
    const table = h("table", { class: "table admin-table users-table sortable" },
        h("thead", {}, h("tr", {},
            _USER_COLUMNS.map(c => sortHeader(c.label, { key: c.key, activeKey: _usersSortKey, dir: _usersSortDir, onSort })),
            h("th", { class: "col-actions", "aria-label": "Ações" }))));
    const tbody = h("tbody");
    for (const u of _sortUsers(users)) {
        // Admins can never be deactivated directly — they have to be demoted
        // to "Usuário" first. Disable + explain instead of a 409 after click.
        const lockActive = u.role === "admin" && u.active;
        const sw = h("input", { type: "checkbox", class: "switch", role: "switch",
                                "aria-label": u.active ? `Desativar ${u.username}` : `Ativar ${u.username}`,
                                title: lockActive ? "Administradores não podem ser desativados. Use \"Tornar usuário\" primeiro." : (u.active ? "Desativar" : "Ativar") });
        sw.checked = !!u.active;
        sw.disabled = lockActive;
        sw.addEventListener("change", () => toggleActive(u.id, !u.active));
        const newRole = u.role === "admin" ? "operator" : "admin";
        const roleAction = u.role === "admin" ? "Tornar usuário" : "Tornar administrador";
        tbody.appendChild(h("tr", { class: u.active ? null : "is-inactive" },
            h("td", {}, h("span", { class: "user-cell" }, avatar(u.username, { size: "sm" }),
                h("span", { class: "user-cell-stack" },
                    h("span", { class: "user-cell-name", text: u.username }),
                    h("span", { class: "cell-sub mono", text: `#${u.id}` })))),
            h("td", {}, h("span", { class: u.role === "admin" ? "chip accent" : "chip", text: _roleLabel(u.role) })),
            h("td", {}, h("label", { class: "switch-label" }, sw,
                h("span", { class: u.active ? "text-ok" : "dim", text: u.active ? "Ativo" : "Desativado" }))),
            h("td", {}, h("span", { class: "date-cell" },
                h("span", { text: fmtDate(u.created_at).split(",")[0] || "—" }),
                h("span", { class: "dim", text: relativeTime(u.created_at) }))),
            h("td", { class: "col-actions" }, h("div", { class: "row-actions" },
                menuButton(() => [{ label: roleAction, icon: u.role === "admin" ? "user" : "shield",
                                     onClick: () => changeRole(u.id, u.username, newRole) }],
                           `Ações de ${u.username}`)))));
    }
    table.appendChild(tbody);
    host.appendChild(h("div", { class: "table-wrap" }, table));
}
async function openCreateUserModal() {
    const r = await promptForm({
        title: "Criar usuário",
        description: "O usuário pode entrar no sistema assim que for criado. Senhas têm no mínimo 6 caracteres.",
        submitLabel: "Criar",
        fields: [
            { key: "username", label: "Nome de usuário", type: "text",
              required: true, autocomplete: "username",
              placeholder: "Ex: joao.silva", maxLength: 80 },
            { key: "password", label: "Senha", type: "password",
              required: true, minLength: 6, autocomplete: "new-password",
              placeholder: "Mínimo 6 caracteres", maxLength: 200,
              help: "Você poderá redefinir depois pela linha de comando." },
            { key: "role", label: "Papel", type: "select",
              required: true, defaultValue: "operator",
              options: [
                  { value: "operator", label: "Usuário" },
                  { value: "admin", label: "Administrador" },
              ],
              help: "Administradores gerenciam projetos e usuários. \"Revisor\" é definido por projeto, não aqui." },
        ],
    });
    if (!r.confirmed) return;
    try {
        await apiPostJson("/api/admin/users", {
            username: r.values.username.trim(),
            password: r.values.password,
            role: r.values.role,
        });
        _usersCache = null;  // tiles-tab dropdown must show the new user
        showToast("Usuário criado.", "success");
        selectTab("users");
    } catch (e) {
        showToast(`Erro ao criar usuário: ${e.message}`, "error");
    }
}

async function changeRole(userId, username, newRole) {
    const promoting = newRole === "admin";
    const r = await confirmDestructive({
        title: promoting ? `Promover ${username} a administrador?` : `Rebaixar ${username} a usuário?`,
        description: promoting
            ? "O usuário ganhará acesso ao painel administrativo (gerenciar projetos, usuários, exports). Não vira revisor automaticamente — isso é configurado por projeto."
            : "O usuário perderá o acesso ao painel administrativo. As participações em projetos e tiles atribuídos são preservados.",
        ids: [userId],
        confirmLabel: promoting ? "Promover" : "Rebaixar",
        danger: !promoting,
    });
    if (!r.confirmed) return;
    try {
        await apiPatchJson(`/api/admin/users/${userId}/role`, { role: newRole });
        _usersCache = null;  // role label changed; tiles-tab dropdown reads it
        showToast(promoting ? "Promovido a administrador." : "Rebaixado a usuário.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}

async function toggleActive(userId, active) {
    try {
        await apiPatchJson(`/api/admin/users/${userId}/active`, { active });
        _usersCache = null;  // tiles-tab dropdown filters out inactive users
        showToast(active ? "Usuário ativado." : "Usuário desativado.", "success");
        selectTab("users");
    } catch (e) { showToast(`Erro: ${e.message}`, "error"); }
}


// "Classe: <swatch> <nome>" line for the tile viewer of classification
// projects. textContent only — class names are admin-entered data.
function classificationLine(classId, projClasses) {
    const p = h("p", { class: "viewer-class-line" });
    p.appendChild(h("span", { class: "viewer-class-label", text: "Classe: " }));
    if (classId == null) {
        p.appendChild(h("span", { class: "dim", text: "— (ainda não classificado)" }));
        return p;
    }
    const cls = projClasses.find(c => c.id === classId);
    const sw = h("span", { class: "viewer-swatch" });
    sw.style.background = cls?.color || "var(--border-strong)";
    p.append(sw, h("strong", { text: cls ? cls.name : `#${classId}` }));
    return p;
}

// ---------- Maintenance ----------
// Operational view of the on-disk overlay cache. Domain config (layers,
// palette, members) lives in /api/projects/* and is edited on the Projetos
// tab — never duplicate it here. Heavier DB chores (recompute counts, verify,
// backup) are CLI-only (backend/scripts/).

async function renderMaintenance(root) {
    const { overlay_cache: cache } = await apiGet("/api/admin/maintenance/overview");
    if (!root.isConnected) return;  // tab changed while loading
    root.textContent = "";
    const clearBtn = button("Limpar cache", { id: "maint-clear-cache", icon: "trash-2", variant: "danger" });
    const refreshBtn = button("Atualizar", { id: "maint-refresh", icon: "refresh-cw" });
    const entries = cache.rendered_tiles + cache.empty_tiles;
    root.append(
        h("div", { class: "kpi-grid" },
            kpi({ icon: "layout-grid", label: "Tiles renderizados", value: fmtInt(cache.rendered_tiles),
                  sub: "com classificação desenhada", tone: "accent" }),
            kpi({ icon: "square-dashed-mouse-pointer", label: "Tiles vazios", value: fmtInt(cache.empty_tiles),
                  sub: "áreas sem máscara (cacheadas)", tone: "neutral" }),
            kpi({ icon: "hard-drive", label: "Tamanho em disco", value: fmtBytes(cache.file_size_bytes),
                  sub: `${fmtInt(entries)} entradas`, tone: "info" }),
            kpi({ icon: "layers", label: "Faixa de zoom", value: `${cache.min_zoom}–${cache.max_zoom}`,
                  sub: "níveis rasterizados", tone: "neutral" })),
        h("div", { class: "maint-row" },
            card({ title: "Cache do overlay administrativo", icon: "database",
                   subtitle: "Tiles do mapa do admin são rasterizados sob demanda e guardados aqui." },
                h("p", { class: "muted maint-lead",
                    text: "A cache é invalidada automaticamente a cada mutação de máscara — limpe manualmente apenas após mudança de paleta ou importação em massa." }),
                h("dl", { class: "maint-kv" },
                    h("dt", { text: "Arquivo" }), h("dd", {}, h("code", { text: cache.path })),
                    h("dt", { text: "Estado" }), h("dd", {}, h("span", { class: entries ? "chip ok" : "chip", text: entries ? "Em uso" : "Vazia" })))),
            card({ title: "Ações", icon: "wrench", cls: "maint-actions-card" },
                h("div", { class: "maint-actions" }, refreshBtn, clearBtn),
                h("p", { class: "muted maint-hint", text: "A cache se reconstrói à medida que você navega no mapa do admin." }))));
    refreshBtn.addEventListener("click", () => selectTab("maintenance"));
    clearBtn.addEventListener("click", async () => {
        const r = await confirmDestructive({
            title: "Limpar cache do overlay?",
            description: `Vai apagar ${entries} entrada(s) (${fmtBytes(cache.file_size_bytes)}). A próxima navegação no mapa do admin recria conforme a área visualizada.`,
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
