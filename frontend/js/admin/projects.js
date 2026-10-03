// Admin tab "Projetos". Two views in the same tab:
//   - LIST: filterable table of projects (search, kind, status, sort). Default.
//   - DETAIL: one project as sectioned cards (overview, configuration,
//     layers, classes, members, export). Back button returns to LIST.
// CRUD always goes through modals (promptForm / openModal). Tile ingestion
// lives in the CLI (backend.scripts.import_points) — never in this UI.
import { apiGet, apiPostJson, apiPatchJson, apiPutJson, apiDelete, authHeader } from "../api.js";
import { showToast } from "../toast.js";
import { escapeHtml, KIND_LABELS } from "../utils.js";
import { fmtInt, fmtPct, fmtDecimal, fmtEtaDays } from "../admin-core.js";
import { confirmDestructive, promptForm, openModal } from "./modals.js";
import { syncAdminProjects } from "../admin.js";
import {
    h, icon, button, iconButton, avatar, card, progressBar, emptyState, menuButton,
} from "./ui.js";

const LAYER_FIELDS = [
    { key: "primary_mbtiles",            label: "Imagem primária",            required: true,  hint: "Caminho .mbtiles, URL com {z}/{x}/{y} ou bingmaps://{z}/{x}/{y}" },
    { key: "secondary_mbtiles",          label: "Imagem secundária (D)",      required: false, hint: "Atalho D — caminho .mbtiles, URL ou bingmaps://" },
    { key: "tertiary_mbtiles",           label: "Imagem terciária (R)",       required: false, hint: "Atalho R — caminho .mbtiles, URL ou bingmaps://" },
    { key: "ref_mask_primary_mbtiles",   label: "Máscara de referência 1ª (T)", required: false, hint: "Atalho T — raster categorizado" },
    { key: "ref_mask_secondary_mbtiles", label: "Máscara de referência 2ª (Y)", required: false, hint: "Atalho Y — raster categorizado" },
];

// KIND_LABELS comes from utils.js — shared with editor and dashboard.
const KIND_HINTS = {
    raster: "Máscara per-pixel (1 byte por pixel).",
    classification: "Uma classe por tile inteiro.",
};
const KIND_ICONS = { raster: "paintbrush", classification: "tag" };

// View state (kept across renders so filters/sort/selection survive when the
// admin leaves the tab and comes back).
let _view = "list";       // "list" | "detail"
let _selectedProjectId = null;
let _searchQuery = "";
let _kindFilter = "all";
let _statusFilter = "all";
let _sortKey = "name";
let _statsCache = [];

const _SORT_LABELS = {
    name: "Nome (A→Z)",
    completion: "Conclusão (maior)",
    tiles: "Mais tiles",
    created: "Mais recentes",
};
function _sortCompare(a, b) {
    if (_sortKey === "name") return a.name.localeCompare(b.name, "pt-BR");
    if (_sortKey === "completion") return (b.completion_percent || 0) - (a.completion_percent || 0);
    if (_sortKey === "tiles") return (b.total_tiles || 0) - (a.total_tiles || 0);
    return (b.id || 0) - (a.id || 0);
}

function _pageActions() {
    const slot = document.getElementById("admin-page-actions");
    if (slot) slot.textContent = "";
    return slot;
}

export async function renderProjects(root) {
    root.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando projetos...</div>`;
    let stats;
    try {
        stats = await apiGet("/api/admin/projects-stats");
    } catch (e) {
        root.textContent = "";
        root.appendChild(card({}, emptyState({ icon: "triangle-alert", title: "Erro ao carregar", text: e.message })));
        return;
    }
    if (!root.isConnected) return;  // tab changed while loading
    _statsCache = stats;
    if (_view === "detail" && _selectedProjectId != null
        && stats.some(p => p.id === _selectedProjectId)) {
        await _renderDetailView(root, _selectedProjectId);
    } else {
        _view = "list";
        _renderListView(root);
    }
}

// ---- LIST VIEW ------------------------------------------------------------

function _renderListView(root) {
    _pageActions()?.appendChild(button("Novo projeto", {
        id: "btn-new-project", icon: "plus", variant: "primary", onClick: openCreateProjectModal,
    }));
    root.textContent = "";
    const search = h("input", { type: "search", id: "projects-search", class: "projects-search",
                                placeholder: "Buscar por nome ou descrição…", autocomplete: "off",
                                "aria-label": "Buscar projetos" });
    const sort = h("select", { id: "projects-sort", "aria-label": "Ordenar" });
    root.append(
        h("div", { class: "toolbar projects-toolbar" },
            h("label", { class: "input-icon toolbar-search" }, icon("search"), search),
            h("div", { class: "segmented projects-filters", id: "kind-filters", role: "group", "aria-label": "Filtro por tipo" }),
            h("div", { class: "segmented projects-filters", id: "status-filters", role: "group", "aria-label": "Filtro por status" }),
            h("span", { class: "toolbar-spacer" }),
            h("label", { class: "toolbar-field projects-sort" }, h("span", { class: "dim", text: "Ordenar" }), sort)),
        h("div", { class: "results-bar" }, h("span", { id: "projects-count", class: "results-count" })),
        h("div", { id: "projects-list-host" }));
    search.value = _searchQuery;
    // Debounce so typing fast doesn't redraw the whole list on every keystroke.
    let searchTimer;
    search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            _searchQuery = search.value.trim().toLowerCase();
            _redrawList();
        }, 150);
    });
    _drawKindFilters();
    _drawStatusFilters();
    _drawSortSelect();
    _redrawList();
}

function _drawChipGroup(hostId, opts, current, onPick) {
    const host = document.getElementById(hostId);
    if (!host) return;
    host.textContent = "";
    for (const [value, label] of opts) {
        const b = h("button", { type: "button", class: `project-filter-chip${current === value ? " active" : ""}`,
                                dataset: { value }, text: label, "aria-pressed": String(current === value) });
        b.onclick = () => onPick(value);
        host.appendChild(b);
    }
}

function _drawKindFilters() {
    _drawChipGroup("kind-filters", [
        ["all", "Todos os tipos"],
        ["raster", KIND_LABELS.raster],
        ["classification", KIND_LABELS.classification],
    ], _kindFilter, (v) => { _kindFilter = v; _drawKindFilters(); _redrawList(); });
}

function _drawStatusFilters() {
    _drawChipGroup("status-filters", [["all", "Todos"], ["active", "Ativos"], ["inactive", "Inativos"]],
        _statusFilter, (v) => { _statusFilter = v; _drawStatusFilters(); _redrawList(); });
}

function _drawSortSelect() {
    const sel = document.getElementById("projects-sort");
    if (!sel) return;
    sel.textContent = "";
    for (const [value, label] of Object.entries(_SORT_LABELS)) {
        const o = h("option", { value, text: label });
        if (_sortKey === value) o.selected = true;
        sel.appendChild(o);
    }
    sel.onchange = () => { _sortKey = sel.value; _redrawList(); };
}

function _redrawList() {
    const host = document.getElementById("projects-list-host");
    const counter = document.getElementById("projects-count");
    if (!host) return;
    const filtered = _statsCache.filter(p => {
        if (_kindFilter !== "all" && p.kind !== _kindFilter) return false;
        if (_statusFilter === "active" && !p.active) return false;
        if (_statusFilter === "inactive" && p.active) return false;
        if (_searchQuery) {
            const hay = `${p.name} ${p.description || ""}`.toLowerCase();
            if (!hay.includes(_searchQuery)) return false;
        }
        return true;
    }).sort(_sortCompare);
    if (counter) {
        counter.textContent = "";
        counter.append(h("strong", { class: "tabular", text: fmtInt(filtered.length) }),
            ` de ${fmtInt(_statsCache.length)} projetos`);
    }
    host.textContent = "";
    if (!_statsCache.length) {
        host.appendChild(_emptyPortfolio());
        return;
    }
    if (!filtered.length) {
        host.appendChild(card({}, emptyState({ icon: "search", title: "Nenhum projeto encontrado",
            text: "Nenhum projeto bate com os filtros atuais." })));
        return;
    }
    host.appendChild(_buildProjectsTable(filtered));
}

function _buildProjectsTable(projects) {
    const table = h("table", { class: "table admin-table projects-table" },
        h("thead", {}, h("tr", {},
            h("th", { text: "Projeto" }), h("th", { text: "Tipo" }),
            h("th", { class: "num", text: "Tiles" }), h("th", { text: "Progresso" }),
            h("th", { class: "num", text: "Ritmo" }), h("th", { class: "num", text: "Membros" }),
            h("th", { text: "Status" }), h("th", { class: "col-chevron", "aria-hidden": "true" }))));
    const tbody = h("tbody");
    for (const p of projects) tbody.appendChild(_buildProjectRow(p));
    table.appendChild(tbody);
    return h("div", { class: "card card-flush projects-table-wrap" }, h("div", { class: "table-wrap admin-table-wrap" }, table));
}

function _buildProjectRow(p) {
    const tr = h("tr", {
        class: `project-row${p.active ? "" : " inactive"}`, tabIndex: 0, role: "button",
        "aria-label": `Abrir projeto ${p.name}`,
    });
    tr.append(
        h("td", {}, h("div", { class: "project-row-name" },
            h("span", { class: `project-glyph kind-${p.kind}` }, icon(KIND_ICONS[p.kind] || "folder")),
            h("span", { class: "project-row-text" },
                h("strong", { text: p.name }),
                p.description ? h("span", { class: "muted project-row-desc", text: p.description }) : null))),
        h("td", {}, h("span", { class: "chip", text: KIND_LABELS[p.kind] || p.kind })),
        h("td", { class: "num", text: fmtInt(p.total_tiles) }),
        h("td", {}, h("span", { class: "progress-cell" },
            progressBar(p.completion_percent, { label: "concluído" }),
            h("span", { class: "tabular progress-cell-pct", text: fmtPct(p.completion_percent) }))),
        h("td", { class: "num", text: p.rate_per_day ? `${fmtDecimal(p.rate_per_day, 1)}/dia` : "—" }),
        h("td", { class: "num", text: fmtInt(p.member_count) }),
        h("td", {}, h("span", { class: p.active ? "chip ok" : "chip", text: p.active ? "Ativo" : "Inativo" })),
        h("td", { class: "col-chevron" }, icon("chevron-right", "icon-sm")));
    const open = () => {
        _selectedProjectId = p.id;
        _view = "detail";
        renderProjects(document.getElementById("admin-content"));
    };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            open();
        }
    });
    return tr;
}

function _emptyPortfolio() {
    return card({}, emptyState({
        icon: "folder-open", title: "Nenhum projeto ainda",
        text: "Crie o primeiro projeto: defina tipo, classes e camadas de imagem. Depois importe tiles via CLI (python -m backend.scripts.import_points).",
        action: button("Criar primeiro projeto", { icon: "plus", variant: "primary", onClick: openCreateProjectModal }),
    }));
}

// ---- DETAIL VIEW ----------------------------------------------------------

async function _renderDetailView(root, projectId) {
    root.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando...</div>`;
    let proj, members;
    try {
        [proj, members] = await Promise.all([
            apiGet(`/api/projects/${projectId}`),
            apiGet(`/api/admin/projects/${projectId}/members`).catch(() => []),
        ]);
    } catch (e) {
        root.textContent = "";
        root.appendChild(card({}, emptyState({ icon: "triangle-alert", title: "Erro ao carregar", text: e.message })));
        return;
    }
    if (!root.isConnected) return;
    const stats = _statsCache.find(s => s.id === projectId) || {};
    _pageActions();
    root.textContent = "";
    root.append(
        _renderBackBar(proj),
        _renderOverviewSection(proj, stats),
        h("div", { class: "project-grid" },
            h("div", { class: "project-col" },
                _renderClassesSection(proj),
                _renderMembersSection(proj, members)),
            h("div", { class: "project-col" },
                _renderExportSection(proj),
                _renderConfigSection(proj),
                _renderLayersSection(proj))));
}

function _renderBackBar(proj) {
    const back = button("Projetos", { icon: "arrow-left", variant: "ghost btn-sm projects-back-btn", onClick: () => {
        _view = "list";
        renderProjects(document.getElementById("admin-content"));
    } });
    const title = h("h2", { class: "projects-detail-title" },
        h("span", { class: `project-glyph project-glyph-lg kind-${proj.kind}` }, icon(KIND_ICONS[proj.kind] || "folder")),
        h("span", { class: "projects-detail-name" },
            h("span", { text: proj.name }),
            h("span", { class: "projects-detail-meta" },
                h("span", { class: "chip accent", text: KIND_LABELS[proj.kind] || proj.kind }),
                h("span", { class: proj.active ? "chip ok" : "chip warn", text: proj.active ? "Ativo" : "Inativo" }),
                h("span", { class: "dim mono", text: `#${proj.id}` }))));
    const actions = h("div", { class: "projects-detail-actions" },
        button("Editar projeto", { icon: "pencil", variant: "primary", onClick: () => openEditProjectModal(proj) }),
        menuButton(() => [
            { label: "Clonar projeto…", icon: "copy", onClick: () => openCloneProjectModal(proj) },
            "sep",
            { label: "Excluir projeto…", icon: "trash-2", danger: true, onClick: () => deleteProject(proj.id, proj.name) },
        ], "Mais ações do projeto"));
    const bar = h("div", { class: "projects-detail-bar" },
        h("div", { class: "projects-detail-bar-left" }, back, title,
            proj.description ? h("p", { class: "muted projects-detail-desc", text: proj.description }) : null),
        actions);
    return bar;
}

function _stat(label, value, sub) {
    return h("div", { class: "project-stat" },
        h("span", { class: "project-stat-label", text: label }),
        h("span", { class: "project-stat-value tabular", text: value }),
        sub ? h("span", { class: "project-stat-sub", text: sub }) : null);
}

function _renderOverviewSection(proj, stats) {
    const pct = stats.completion_percent || 0;
    return h("section", { class: "card project-overview" },
        h("div", { class: "project-overview-progress" },
            h("div", { class: "project-overview-head" },
                h("span", { class: "eyebrow", text: "Progresso" }),
                h("span", { class: "tabular project-overview-pct", text: fmtPct(pct) })),
            progressBar(pct, { label: "Progresso do projeto" }),
            h("span", { class: "muted", text: `${fmtInt(stats.classified_tiles || 0)} de ${fmtInt(stats.total_tiles || 0)} tiles classificados` })),
        h("div", { class: "project-stats" },
            _stat("Tiles", fmtInt(stats.total_tiles || 0), `${fmtInt((stats.total_tiles || 0) - (stats.classified_tiles || 0))} a classificar`),
            _stat("Ritmo", stats.rate_per_day ? `${fmtDecimal(stats.rate_per_day, 1)}/dia` : "—", "últimos 7 dias"),
            _stat("Previsão", fmtEtaDays(stats.eta_days), "para concluir"),
            _stat("Membros", fmtInt(stats.member_count || 0), "com acesso")));
}

function _renderConfigSection(proj) {
    const tileMeters = proj.tile_meters
        ?? (proj.tile_px || 256) * (proj.meters_per_pixel || 2.5);
    const item = (label, value, ic) => h("div", { class: "project-meta-item" },
        h("span", { class: "label" }, ic ? icon(ic, "icon-sm") : null, label),
        h("span", { class: "value", text: value }));
    return card({ title: "Configuração", icon: "settings" },
        h("div", { class: "project-meta-grid" },
            item("Tile", `${proj.tile_px ?? 256} × ${proj.tile_px ?? 256} px`, "scan"),
            item("Resolução", `${fmtDecimal(proj.meters_per_pixel ?? 2.5, 2)} m/px`, "target"),
            item("Tile no chão", `${fmtDecimal(tileMeters, 1)} m`, "maximize-2"),
            item("Geometria", proj.tile_geometry_locked ? "Travada" : "Editável", proj.tile_geometry_locked ? "lock" : "lock-open"),
            proj.kind === "raster"
                ? item("Máscara completa", proj.mask_complete_required === false ? "Opcional" : "Obrigatória", "list-checks")
                : null));
}

function _renderLayersSection(proj) {
    const list = h("ul", { class: "layer-list" });
    for (const f of LAYER_FIELDS) {
        const v = proj[f.key];
        list.appendChild(h("li", { class: `layer-row${v ? "" : " is-empty"}` },
            h("span", { class: "layer-icon" }, icon(f.key.startsWith("ref_") ? "layers" : "image", "icon-sm")),
            h("span", { class: "layer-text" },
                h("span", { class: "layer-label", text: f.label }),
                v ? h("code", { class: "layer-value", text: v, title: v })
                  : h("span", { class: "dim layer-value", text: "Não configurada" }))));
    }
    return card({ title: "Camadas de imagem", icon: "layers" }, list);
}

function _renderClassesSection(proj) {
    // Raster mask bytes encode class ids — once tiles exist, any class
    // edit (rename/recolor/remove) risks orphaning painted pixels.
    // Backend already blocks removal, but we lock the whole editor for
    // raster to keep the UX coherent: "if it's locked, don't tempt the
    // admin". Classification still allows rename/recolor.
    const classesLocked = proj.kind === "raster" && proj.tile_geometry_locked;
    const btn = button("Editar classes", { icon: "palette", variant: "btn-sm", onClick: () => openEditClassesModal(proj) });
    if (classesLocked) {
        btn.disabled = true;
        btn.title = "Projetos de segmentação não permitem editar classes depois do primeiro tile — os bytes da máscara guardam o id da classe e mudar quebra dados existentes.";
    }
    const classes = proj.classes || [];
    const body = classes.length
        ? h("ul", { class: "class-grid" }, classes.map(c => h("li", { class: "class-item" },
            h("span", { class: "class-swatch", style: { background: c.color } }),
            h("span", { class: "class-text" },
                h("span", { class: "class-name", text: c.name }),
                h("span", { class: "class-meta mono", text: `id ${c.id} · ${c.color}` })))))
        : emptyState({ icon: "palette", title: "Nenhuma classe cadastrada" });
    return card({
        title: "Classes", icon: "palette",
        subtitle: classesLocked ? "Travadas: o projeto já possui tiles. Para mudar a paleta, clone o projeto." : `${classes.length} classe(s)`,
        actions: [btn],
    }, body);
}

const _ROLE_LABELS = { operator: "Operador", reviewer: "Revisor", admin: "Admin" };

function _renderMembersSection(proj, members) {
    const add = button("Adicionar", { icon: "user-plus", variant: "btn-sm", onClick: () => openAddMemberModal(proj) });
    if (!members.length) {
        return card({ title: "Membros", icon: "users", actions: [add] },
            emptyState({ icon: "users", title: "Nenhum membro", text: "Adicione usuários para que possam trabalhar neste projeto." }));
    }
    const list = h("ul", { class: "member-list" });
    for (const m of members) {
        let roleCtl;
        if (m.project_role === "operator" || m.project_role === "reviewer") {
            roleCtl = h("select", { class: "member-role", "aria-label": `Papel de ${m.username}` },
                ["operator", "reviewer"].map(role => h("option", { value: role, text: _ROLE_LABELS[role] })));
            roleCtl.value = m.project_role;
            roleCtl.onchange = () => upsertMemberRole(proj.id, m.id, roleCtl.value);
        } else {
            roleCtl = h("span", { class: "chip accent", text: _ROLE_LABELS[m.project_role] || m.project_role });
        }
        list.appendChild(h("li", { class: `member-row${m.active === false || m.active === 0 ? " is-inactive" : ""}` },
            avatar(m.username, { size: "sm" }),
            h("span", { class: "member-text" },
                h("span", { class: "member-name", text: m.username }),
                h("span", { class: "member-sub", text: m.global_role === "admin" ? "Administrador do sistema" : "Usuário" })),
            roleCtl,
            iconButton("x", `Remover ${m.username}`, { onClick: () => removeMember(proj.id, m.id, m.username) })));
    }
    return card({ title: "Membros", icon: "users", subtitle: `${members.length} com acesso`, actions: [add], cls: "card-list" }, list);
}

function _renderExportSection(proj) {
    const select = h("select", { id: "export-status" });
    // "Somente classificados" was removed: exporting tiles that haven't gone
    // through review is rarely the right call — the reviewer step is where
    // the dataset gets its quality stamp. Admins who need the lower-quality
    // pool can still run the CLI export directly with --status=classified.
    for (const [v, label] of [
        ["reviewed", "Somente revisados"],
        ["reviewed_classified", "Revisados + classificados"],
    ]) select.appendChild(h("option", { value: v, text: label }));
    const fields = h("div", { class: "export-fields" },
        h("label", { class: "field" }, h("span", { class: "field-label", text: "Tiles incluídos" }), select));
    // Raster only: the EDGV remap LUT is meaningful only for the legacy
    // 6-class palette (ids 1..6). "auto" lets the backend decide per project.
    let remapSelect = null;
    if (proj.kind !== "classification") {
        remapSelect = h("select", { id: "export-remap",
            title: "Automático: aplica o remap EDGV só quando a paleta do projeto é exatamente as classes 1..6" });
        for (const [v, label] of [
            ["auto", "Automático (padrão)"],
            ["edgv", "EDGV"],
            ["raw", "IDs originais"],
        ]) remapSelect.appendChild(h("option", { value: v, text: label }));
        fields.appendChild(h("label", { class: "field" },
            h("span", { class: "field-label", text: "Remapeamento de classes" }), remapSelect));
    }
    const btn = button("Gerar export (ZIP)", { id: "btn-export", icon: "download", variant: "primary" });
    btn.onclick = () => runExportJob(proj.id, select.value, remapSelect ? remapSelect.value : null, btn);
    const line = h("p", { class: "muted export-status-line", id: "export-status-line", role: "status" });
    return card({ title: "Exportar dataset", icon: "download", cls: "export-card",
                  subtitle: `${exportFormatLabel(proj.kind)} · manifest com status, autoria e bbox por tile` },
        fields, h("div", { class: "export-actions" }, btn), line);
}

function exportFormatLabel(kind) {
    if (kind === "classification") return "CSV (classifications.csv)";
    return "GeoTIFF + manifest.csv";
}

// ---- Modais (create / edit / clone / members / classes) -------------------

async function openCreateProjectModal() {
    const result = await openModal({
        title: "Criar projeto",
        size: "xl",
        submitLabel: "Criar",
        render: (host) => _renderProjectFormBody(host, null),
        onSubmit: (host) => _readProjectFormBody(host, null),
    });
    if (!result.confirmed) return;
    try {
        const proj = await apiPostJson("/api/admin/projects", result.payload);
        showToast(`Projeto "${proj.name}" criado.`, "success");
        _selectedProjectId = proj.id;
        _view = "detail";
        await refreshProjectList();
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha ao criar: ${e.message}`, "error", 8000);
    }
}

async function openEditProjectModal(proj) {
    const result = await openModal({
        title: `Editar "${proj.name}"`,
        size: "lg",
        submitLabel: "Salvar",
        render: (host) => _renderProjectFormBody(host, proj),
        onSubmit: (host) => _readProjectFormBody(host, proj),
    });
    if (!result.confirmed) return;
    try {
        await apiPatchJson(`/api/admin/projects/${proj.id}`, result.payload);
        showToast("Projeto salvo.", "success");
        await refreshProjectList();
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha ao salvar: ${e.message}`, "error", 6000);
    }
}

function _renderProjectFormBody(host, proj) {
    const isEdit = !!proj;
    const initialKind = proj?.kind || "raster";
    host.innerHTML = `
        <div class="modal-form-fields">
            <div class="modal-form-row">
                <label class="modal-form-field" style="flex:1;">
                    <span class="modal-form-label">Nome *</span>
                    <input type="text" id="pf-name" required maxlength="80"
                           value="${escapeHtml(proj?.name || "")}">
                </label>
                <label class="modal-form-field" style="flex:0 0 200px;">
                    <span class="modal-form-label">Tipo *</span>
                    <select id="pf-kind" ${isEdit ? "disabled" : ""}>
                        ${Object.entries(KIND_LABELS).map(([v, label]) =>
                            `<option value="${v}" ${initialKind === v ? "selected" : ""}>${label}</option>`
                        ).join("")}
                    </select>
                </label>
            </div>
            <span class="modal-form-help" id="pf-kind-hint">${escapeHtml(KIND_HINTS[initialKind] || "")}</span>
            ${isEdit ? '<span class="modal-form-help">O tipo é imutável — clone o projeto para mudá-lo.</span>' : ""}

            <label class="modal-form-field">
                <span class="modal-form-label">Descrição</span>
                <input type="text" id="pf-description" maxlength="200"
                       value="${escapeHtml(proj?.description || "")}">
            </label>

            <label class="modal-form-field" id="pf-active-wrap" ${isEdit ? "" : 'style="display:none"'}>
                <span class="modal-form-label">
                    <input type="checkbox" id="pf-active" ${proj?.active !== false ? "checked" : ""}>
                    Projeto ativo
                </span>
                <span class="modal-form-help">Inativos não recebem novas atribuições — histórico é preservado.</span>
            </label>

            <fieldset class="modal-form-fieldset" id="pf-flags">
                <legend>Validações</legend>
                <label class="modal-form-field" id="pf-mask-row">
                    <span><input type="checkbox" id="pf-mask-required"
                                 ${proj?.mask_complete_required !== false ? "checked" : ""}> Exigir máscara completa</span>
                </label>
            </fieldset>

            <fieldset class="modal-form-fieldset">
                <legend>Geometria do tile</legend>
                <div class="modal-form-row">
                    <label class="modal-form-field">
                        <span class="modal-form-label">Lado (px)</span>
                        <input type="number" id="pf-tile-px" min="1" max="4096" step="1"
                               value="${proj?.tile_px ?? 256}"
                               ${proj?.tile_geometry_locked ? "disabled" : ""}>
                    </label>
                    <label class="modal-form-field">
                        <span class="modal-form-label">Metros por pixel</span>
                        <input type="number" id="pf-mpp" min="0.1" max="100" step="0.1"
                               value="${proj?.meters_per_pixel ?? 2.5}"
                               ${proj?.tile_geometry_locked ? "disabled" : ""}>
                    </label>
                </div>
                <span class="modal-form-help" id="pf-geom-hint"></span>
            </fieldset>

            <fieldset class="modal-form-fieldset">
                <legend>Camadas</legend>
                ${LAYER_FIELDS.map(f => `
                    <label class="modal-form-field">
                        <span class="modal-form-label">${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
                        <input type="text" data-layer="${f.key}"
                               value="${escapeHtml(proj?.[f.key] || "")}"
                               placeholder="${escapeHtml(f.hint)}">
                    </label>
                `).join("")}
            </fieldset>

            <fieldset class="modal-form-fieldset ${isEdit ? "hidden" : ""}" id="pf-classes-block">
                <legend>Classes iniciais</legend>
                <div id="pf-classes-editor"></div>
                <span class="modal-form-help">
                    ${isEdit
                        ? "Use \"Editar classes\" no painel principal para alterar depois."
                        : "Adicione no mínimo 1 classe."}
                </span>
            </fieldset>
        </div>
    `;
    const kindSel = host.querySelector("#pf-kind");
    const refreshKindUI = () => {
        const kind = kindSel.value;
        const hint = host.querySelector("#pf-kind-hint");
        if (hint) hint.textContent = KIND_HINTS[kind] || "";
        host.querySelector("#pf-flags").classList.toggle("hidden", kind !== "raster");
    };
    kindSel.onchange = refreshKindUI;
    refreshKindUI();

    const px = host.querySelector("#pf-tile-px");
    const mpp = host.querySelector("#pf-mpp");
    const hint = host.querySelector("#pf-geom-hint");
    const refreshGeom = () => {
        const p = parseInt(px.value, 10) || 256;
        const m = parseFloat(mpp.value) || 2.5;
        const ground = p * m;
        const lockText = proj?.tile_geometry_locked
            ? " Geometria já travada (projeto tem tiles)."
            : " Após o primeiro tile, esses campos travam.";
        const g = fmtDecimal(ground, 1);
        hint.textContent = `Tile no chão: ${g} m × ${g} m (${p}×${p} px a ${fmtDecimal(m, 2)} m/px).${lockText}`;
    };
    px.addEventListener("input", refreshGeom);
    mpp.addEventListener("input", refreshGeom);
    refreshGeom();

    if (!isEdit) {
        _renderClassesEditor(host.querySelector("#pf-classes-editor"),
                             [{ id: 1, name: "classe_1", color: "#377eb8" }]);
    }
}

function _readProjectFormBody(host, proj) {
    const isEdit = !!proj;
    const name = host.querySelector("#pf-name").value.trim();
    if (!name) {
        showToast("Informe o nome.", "error");
        return null;
    }
    const kind = host.querySelector("#pf-kind").value;
    const tilePx = parseInt(host.querySelector("#pf-tile-px").value, 10);
    const mpp = parseFloat(host.querySelector("#pf-mpp").value);
    const body = {
        name,
        description: host.querySelector("#pf-description").value.trim(),
    };
    const geomDirty = (isEdit && !proj.tile_geometry_locked) || !isEdit;
    if (geomDirty) {
        body.tile_px = Number.isFinite(tilePx) ? tilePx : 256;
        body.meters_per_pixel = Number.isFinite(mpp) ? mpp : 2.5;
    }
    if (!isEdit) {
        body.kind = kind;
    } else {
        body.active = host.querySelector("#pf-active").checked;
    }
    if (kind === "raster") {
        const mc = host.querySelector("#pf-mask-required");
        if (mc) body.mask_complete_required = mc.checked;
    }
    for (const f of LAYER_FIELDS) {
        const el = host.querySelector(`[data-layer="${f.key}"]`);
        const v = el ? el.value.trim() : "";
        if (isEdit) {
            body[f.key] = v || null;
        } else if (v) {
            body[f.key] = v;
        } else if (f.required) {
            showToast(`Campo obrigatório: ${f.label}`, "error");
            return null;
        }
    }
    if (!isEdit) {
        body.classes = _readClassesEditor(host.querySelector("#pf-classes-editor"));
        if (!body.classes.length) {
            showToast("Adicione pelo menos 1 classe.", "error");
            return null;
        }
    }
    return body;
}

async function openCloneProjectModal(proj) {
    const r = await promptForm({
        title: `Clonar "${proj.name}"`,
        description: "Cria um projeto novo com a mesma configuração (classes, layers, geometria). Membros NÃO são copiados.",
        submitLabel: "Clonar",
        fields: [
            { key: "name", label: "Nome do novo projeto", type: "text",
              required: true, maxLength: 80,
              defaultValue: `${proj.name}_copia` },
        ],
    });
    if (!r.confirmed) return;
    try {
        const created = await apiPostJson(
            `/api/admin/projects/${proj.id}/clone`, { name: r.values.name.trim() },
        );
        showToast(`Projeto "${created.name}" criado.`, "success");
        _selectedProjectId = created.id;
        _view = "detail";
        await refreshProjectList();
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha ao clonar: ${e.message}`, "error", 6000);
    }
}

async function openAddMemberModal(proj) {
    let users = [];
    try { users = await apiGet("/api/admin/users"); }
    catch (e) { showToast(`Falha ao carregar usuários: ${e.message}`, "error"); return; }
    const eligible = users.filter(u => u.active);
    if (!eligible.length) {
        showToast("Sem usuários ativos para adicionar.", "error");
        return;
    }
    const r = await promptForm({
        title: `Adicionar membro a "${proj.name}"`,
        submitLabel: "Adicionar",
        fields: [
            { key: "user_id", label: "Usuário", type: "select", required: true,
              options: eligible.map(u => ({ value: u.id, label: u.username })) },
            { key: "role", label: "Papel no projeto", type: "select", required: true,
              defaultValue: "operator",
              options: [
                  { value: "operator", label: "Operador" },
                  { value: "reviewer", label: "Revisor" },
              ],
              help: "Operador classifica; Revisor revisa o que outros classificaram. Administradores globais que querem trabalhar no projeto entram como Operador ou Revisor." },
        ],
    });
    if (!r.confirmed) return;
    try {
        await apiPostJson(`/api/admin/projects/${proj.id}/members`, {
            user_id: parseInt(r.values.user_id, 10),
            role: r.values.role,
        });
        showToast("Membro adicionado.", "success");
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha: ${e.message}`, "error", 6000);
    }
}

async function openEditClassesModal(proj) {
    const result = await openModal({
        title: `Classes — ${proj.name}`,
        size: "lg",
        submitLabel: "Salvar classes",
        render: (host) => _renderClassesEditor(host, proj.classes || []),
        onSubmit: (host) => {
            const classes = _readClassesEditor(host);
            if (!classes.length) {
                showToast("Adicione pelo menos 1 classe.", "error");
                return null;
            }
            return { classes };
        },
    });
    if (!result.confirmed) return;
    try {
        await apiPutJson(`/api/admin/projects/${proj.id}/classes`, result.payload);
        showToast("Classes salvas.", "success");
        await refreshProjectList();
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha: ${e.message}`, "error", 6000);
    }
}

function _renderClassesEditor(host, initial) {
    host.innerHTML = `
        <table class="admin-table editor-table">
            <thead><tr>
                <th style="width:6em">ID</th>
                <th>Nome</th>
                <th style="width:6em">Cor</th>
                <th style="width:8em"></th>
            </tr></thead>
            <tbody></tbody>
        </table>
        <button type="button" data-act="add-class" class="add-row-btn btn-sm">+ Adicionar classe</button>
    `;
    const tbody = host.querySelector("tbody");
    const addRow = (c) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td><input type="number" min="1" max="254" data-cf="id" value="${c.id}"></td>
            <td><input type="text" data-cf="name" value="${escapeHtml(c.name)}"></td>
            <td><input type="color" data-cf="color" value="${escapeHtml(c.color)}"></td>
            <td><button type="button" class="ghost danger btn-sm" data-act="remove">Remover</button></td>
        `;
        tr.querySelector('[data-act="remove"]').onclick = () => tr.remove();
        tbody.appendChild(tr);
    };
    for (const c of initial) addRow(c);
    host.querySelector('[data-act="add-class"]').onclick = () => {
        const nextId = (tbody.querySelectorAll('[data-cf="id"]').length
            ? Math.max(...[...tbody.querySelectorAll('[data-cf="id"]')]
                .map(i => parseInt(i.value, 10) || 0)) + 1
            : 1);
        addRow({ id: nextId, name: `classe_${nextId}`, color: "#888888" });
    };
}

function _readClassesEditor(host) {
    const out = [];
    for (const r of host.querySelectorAll("tbody tr")) {
        const id = parseInt(r.querySelector('[data-cf="id"]').value, 10);
        const name = r.querySelector('[data-cf="name"]').value.trim();
        const color = r.querySelector('[data-cf="color"]').value;
        if (Number.isFinite(id) && name) out.push({ id, name, color });
    }
    return out;
}

// ---- I/O helpers ----------------------------------------------------------

async function upsertMemberRole(projectId, userId, role) {
    try {
        await apiPostJson(`/api/admin/projects/${projectId}/members`, {
            user_id: userId, role,
        });
        showToast("Papel atualizado.", "success");
    } catch (e) {
        showToast(`Falha: ${e.message}`, "error", 6000);
    }
}

async function removeMember(projectId, userId, username) {
    const r = await confirmDestructive({
        title: `Remover ${username} do projeto?`,
        description: "O usuário perde acesso ao projeto. Tiles atribuídos a ele continuam atribuídos.",
        ids: [userId],
        confirmLabel: "Remover",
    });
    if (!r.confirmed) return;
    try {
        await apiDelete(`/api/admin/projects/${projectId}/members/${userId}`);
        showToast("Membro removido.", "success");
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha: ${e.message}`, "error", 6000);
    }
}

async function deleteProject(projectId, projectName) {
    const r = await confirmDestructive({
        title: `Excluir projeto "${projectName}"?`,
        description: "Só funciona se o projeto não tem nenhum tile. Caso contrário, desative-o em vez de excluir.",
        ids: [projectId],
        confirmLabel: "Excluir projeto",
    });
    if (!r.confirmed) return;
    try {
        await apiDelete(`/api/admin/projects/${projectId}`);
        showToast("Projeto excluído.", "success");
        _view = "list";
        _selectedProjectId = null;
        await refreshProjectList();
        await renderProjects(document.getElementById("admin-content"));
    } catch (e) {
        showToast(`Falha ao excluir: ${e.message}`, "error", 8000);
    }
}

// ---- Export job -----------------------------------------------------------

async function runExportJob(projectId, status, remap, btn) {
    const line = document.getElementById("export-status-line");
    const prev = [...btn.childNodes];
    btn.disabled = true;
    btn.textContent = "";
    btn.append(h("span", { class: "loading loading-sm" }), "Gerando...");
    const setLine = (t) => { if (line) line.textContent = t; };
    try {
        const qs = new URLSearchParams({ status });
        if (remap) qs.set("remap", remap);
        const job = await apiPostJson(
            `/api/admin/projects/${projectId}/export-jobs?${qs}`, {});
        setLine("Export em andamento...");
        const done = await _pollExportJob(job.id, setLine);
        if (done.state === "error") throw new Error(done.error || "falha no processamento");
        await _downloadJobArtifact(done.id);
        setLine(`Pronto: ${done.tile_count ?? "?"} tile(s).`);
        showToast(`Export gerado: ${done.tile_count ?? "?"} tile(s).`, "success");
    } catch (e) {
        setLine("");
        showToast(`Falha no export: ${e.message}`, "error", 6000);
    } finally {
        btn.disabled = false;
        btn.replaceChildren(...prev);
    }
}

function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function _pollExportJob(jobId, setLine) {
    for (let i = 0; i < 600; i++) {
        const job = await apiGet(`/api/admin/export-jobs/${jobId}`);
        if (job.state === "done" || job.state === "error") return job;
        setLine(job.state === "running" ? "Processando..." : "Na fila...");
        await _sleep(1500);
    }
    throw new Error("tempo esgotado aguardando o export");
}

async function _downloadJobArtifact(jobId) {
    const res = await fetch(`/api/admin/export-jobs/${jobId}/download`, { headers: authHeader() });
    if (!res.ok) {
        let msg = res.statusText;
        try { const b = await res.json(); msg = b?.detail?.error || msg; } catch {}
        throw new Error(msg);
    }
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const fname = m ? m[1] : `export_job_${jobId}.zip`;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

async function refreshProjectList() {
    try {
        const [stats] = await Promise.all([
            apiGet("/api/admin/projects-stats"),
            syncAdminProjects(),
        ]);
        _statsCache = stats;
    } catch {}
}
