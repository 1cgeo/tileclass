// Admin tab "Projetos". Two views in the same tab:
//   - LIST: grid of project cards with filters/search/sort. Default.
//   - DETAIL: full-page view of one project (back button returns to LIST).
// CRUD always goes through modals (promptForm / openModal). Tile ingestion
// lives in the CLI (backend.scripts.import_points) — never in this UI.
import { apiGet, apiPostJson, apiPatchJson, apiPutJson, apiDelete, authHeader } from "../api.js";
import { showToast } from "../toast.js";
import { escapeHtml, KIND_LABELS } from "../utils.js";
import { confirmDestructive, promptForm, openModal } from "./modals.js";
import { syncAdminProjects } from "../admin.js";

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
    vector: "Features vetoriais (LineStrings) com atributos por feature.",
    classification: "Uma classe por tile inteiro.",
    detection: "Caixas (bounding boxes) com classe por caixa.",
};
const ATTR_TYPES = ["text", "number", "enum", "boolean"];

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

export async function renderProjects(root) {
    root.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando projetos...</div>`;
    let stats;
    try {
        stats = await apiGet("/api/admin/projects-stats");
    } catch (e) {
        root.innerHTML = `<p class="error">Erro: ${escapeHtml(e.message)}</p>`;
        return;
    }
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
    root.innerHTML = `
        <div class="projects-toolbar">
            <div class="projects-toolbar-row">
                <h3>Projetos <span class="muted" id="projects-count"></span></h3>
                <button id="btn-new-project" class="primary" type="button">+ Novo projeto</button>
            </div>
            <div class="projects-toolbar-row">
                <input type="search" id="projects-search" class="projects-search"
                       placeholder="Buscar por nome ou descrição..." autocomplete="off">
                <label class="projects-sort">
                    <span class="muted">Ordenar:</span>
                    <select id="projects-sort"></select>
                </label>
            </div>
            <div class="projects-toolbar-row projects-filter-row">
                <span class="projects-filter-label muted">Tipo:</span>
                <div class="projects-filters" id="kind-filters" role="tablist" aria-label="Filtro por tipo"></div>
            </div>
            <div class="projects-toolbar-row projects-filter-row">
                <span class="projects-filter-label muted">Status:</span>
                <div class="projects-filters" id="status-filters" role="tablist" aria-label="Filtro por status"></div>
            </div>
        </div>
        <div id="projects-list-host"></div>
    `;
    document.getElementById("btn-new-project").onclick = openCreateProjectModal;
    const search = document.getElementById("projects-search");
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

function _drawKindFilters() {
    const host = document.getElementById("kind-filters");
    if (!host) return;
    host.innerHTML = "";
    const opts = [
        ["all", "Todos"],
        ["raster", KIND_LABELS.raster],
        ["vector", KIND_LABELS.vector],
        ["classification", KIND_LABELS.classification],
        ["detection", KIND_LABELS.detection],
    ];
    for (const [value, label] of opts) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "project-filter-chip" + (_kindFilter === value ? " active" : "");
        btn.dataset.value = value;
        btn.textContent = label;
        btn.onclick = () => { _kindFilter = value; _drawKindFilters(); _redrawList(); };
        host.appendChild(btn);
    }
}

function _drawStatusFilters() {
    const host = document.getElementById("status-filters");
    if (!host) return;
    host.innerHTML = "";
    const opts = [["all", "Todos"], ["active", "Ativos"], ["inactive", "Inativos"]];
    for (const [value, label] of opts) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "project-filter-chip" + (_statusFilter === value ? " active" : "");
        btn.dataset.value = value;
        btn.textContent = label;
        btn.onclick = () => { _statusFilter = value; _drawStatusFilters(); _redrawList(); };
        host.appendChild(btn);
    }
}

function _drawSortSelect() {
    const sel = document.getElementById("projects-sort");
    if (!sel) return;
    sel.innerHTML = "";
    for (const [value, label] of Object.entries(_SORT_LABELS)) {
        const o = document.createElement("option");
        o.value = value;
        o.textContent = label;
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
    if (counter) counter.textContent = `(${filtered.length}/${_statsCache.length})`;
    host.innerHTML = "";
    if (!_statsCache.length) {
        host.appendChild(_emptyPortfolio());
        return;
    }
    if (!filtered.length) {
        const e = document.createElement("p");
        e.className = "muted";
        e.style.padding = "var(--space-4)";
        e.style.textAlign = "center";
        e.textContent = "Nenhum projeto bate com os filtros atuais.";
        host.appendChild(e);
        return;
    }
    host.appendChild(_buildProjectsTable(filtered));
}

function _buildProjectsTable(projects) {
    const wrap = document.createElement("div");
    wrap.className = "admin-table-wrap projects-table-wrap";
    const table = document.createElement("table");
    table.className = "admin-table projects-table";
    table.innerHTML = `
        <thead>
            <tr>
                <th>Nome</th>
                <th>Tipo</th>
                <th class="num">Tiles</th>
                <th>Progresso</th>
                <th class="num">Membros</th>
                <th>Status</th>
            </tr>
        </thead>
        <tbody></tbody>
    `;
    const tbody = table.querySelector("tbody");
    for (const p of projects) tbody.appendChild(_buildProjectRow(p));
    wrap.appendChild(table);
    return wrap;
}

function _buildProjectRow(p) {
    const tr = document.createElement("tr");
    tr.className = "project-row" + (p.active ? "" : " inactive");
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.setAttribute("aria-label", `Abrir projeto ${p.name}`);

    const nameTd = document.createElement("td");
    const nameWrap = document.createElement("div");
    nameWrap.className = "project-row-name";
    const name = document.createElement("strong"); name.textContent = p.name;
    nameWrap.appendChild(name);
    if (p.description) {
        const d = document.createElement("span");
        d.className = "muted project-row-desc";
        d.textContent = p.description;
        nameWrap.appendChild(d);
    }
    nameTd.appendChild(nameWrap);

    const kindTd = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = "chip info";
    chip.textContent = KIND_LABELS[p.kind] || p.kind;
    kindTd.appendChild(chip);

    const tilesTd = document.createElement("td");
    tilesTd.className = "num";
    tilesTd.textContent = p.total_tiles;

    const progressTd = document.createElement("td");
    const progWrap = document.createElement("div");
    progWrap.className = "project-row-progress";
    const bar = document.createElement("div");
    bar.className = "project-row-progress-bar";
    const fill = document.createElement("span");
    fill.style.width = `${Math.min(100, p.completion_percent || 0)}%`;
    bar.appendChild(fill);
    const pct = document.createElement("span");
    pct.className = "project-row-progress-pct";
    pct.textContent = `${p.completion_percent}%`;
    progWrap.append(bar, pct);
    progressTd.appendChild(progWrap);

    const membersTd = document.createElement("td");
    membersTd.className = "num";
    membersTd.textContent = p.member_count;

    const statusTd = document.createElement("td");
    const statusChip = document.createElement("span");
    statusChip.className = p.active ? "chip reviewed" : "chip warn";
    statusChip.textContent = p.active ? "ativo" : "inativo";
    statusTd.appendChild(statusChip);

    tr.append(nameTd, kindTd, tilesTd, progressTd, membersTd, statusTd);
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
    const wrap = document.createElement("div");
    wrap.className = "project-empty-state";
    const h = document.createElement("h3"); h.textContent = "Nenhum projeto ainda";
    const p = document.createElement("p");
    p.textContent = "Crie o primeiro projeto: defina tipo, classes/atributos e camadas de imagem.";
    const cta = document.createElement("button");
    cta.type = "button"; cta.className = "primary"; cta.textContent = "+ Criar primeiro projeto";
    cta.onclick = openCreateProjectModal;
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.style.fontSize = "var(--text-sm)";
    hint.textContent = "Depois importe tiles via CLI: python -m backend.scripts.import_points";
    wrap.append(h, p, cta, hint);
    return wrap;
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
        root.innerHTML = `<p class="error">Erro: ${escapeHtml(e.message)}</p>`;
        return;
    }
    root.innerHTML = "";
    root.appendChild(_renderBackBar(proj));
    root.appendChild(_renderOverviewSection(proj));
    root.appendChild(_renderClassesOrAttributesSection(proj));
    root.appendChild(_renderMembersSection(proj, members));
    root.appendChild(_renderExportSection(proj));
}

function _renderBackBar(proj) {
    const bar = document.createElement("div");
    bar.className = "projects-detail-bar";
    const left = document.createElement("div");
    left.className = "projects-detail-bar-left";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "projects-back-btn";
    back.innerHTML = "&larr; Projetos";
    back.onclick = () => {
        _view = "list";
        renderProjects(document.getElementById("admin-content"));
    };
    const title = document.createElement("h3");
    title.className = "projects-detail-title";
    const name = document.createElement("span"); name.textContent = proj.name;
    const idSpan = document.createElement("span"); idSpan.className = "muted"; idSpan.textContent = `#${proj.id}`;
    const kindChip = document.createElement("span"); kindChip.className = "chip info"; kindChip.textContent = KIND_LABELS[proj.kind] || proj.kind;
    title.append(name, idSpan, kindChip);
    if (!proj.active) {
        const inact = document.createElement("span");
        inact.className = "chip warn";
        inact.textContent = "Inativo";
        title.appendChild(inact);
    }
    left.append(back, title);
    bar.appendChild(left);

    const actions = document.createElement("div");
    actions.className = "projects-detail-actions";
    actions.append(
        _action("Editar projeto", () => openEditProjectModal(proj), "primary"),
        _action("Clonar", () => openCloneProjectModal(proj)),
        _action("Excluir", () => deleteProject(proj.id, proj.name), "danger"),
    );
    bar.appendChild(actions);
    return bar;
}

function _action(label, onClick, extraClass = "") {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (extraClass) b.className = extraClass;
    b.onclick = onClick;
    return b;
}

function _renderOverviewSection(proj) {
    const sec = document.createElement("section");
    sec.className = "project-section";
    const h = document.createElement("h4");
    h.textContent = "Visão geral";
    sec.appendChild(h);
    if (proj.description) {
        const p = document.createElement("p");
        p.className = "muted project-section-lead";
        p.textContent = proj.description;
        sec.appendChild(p);
    }
    const grid = document.createElement("div");
    grid.className = "project-meta-grid";
    const tileMeters = proj.tile_meters
        ?? (proj.tile_px || 256) * (proj.meters_per_pixel || 2.5);
    grid.append(
        _metaItem("Tipo", KIND_LABELS[proj.kind] || proj.kind),
        _metaItem("Status", proj.active ? "Ativo" : "Inativo"),
        _metaItem("Tile (px)", `${proj.tile_px ?? 256} × ${proj.tile_px ?? 256}`),
        _metaItem("Resolução", `${(proj.meters_per_pixel ?? 2.5).toFixed(2)} m/px`),
        _metaItem("Tile no chão", `${tileMeters.toFixed(1)} m`),
        _metaItem("Geometria", proj.tile_geometry_locked ? "Travada" : "Editável"),
    );
    sec.appendChild(grid);

    const layersH = document.createElement("h4");
    layersH.style.marginTop = "var(--space-4)";
    layersH.textContent = "Camadas";
    sec.appendChild(layersH);
    const layersList = document.createElement("dl");
    layersList.className = "project-layers-list";
    for (const f of LAYER_FIELDS) {
        const dt = document.createElement("dt"); dt.textContent = f.label;
        const dd = document.createElement("dd");
        const v = proj[f.key];
        if (v) {
            const code = document.createElement("code");
            code.textContent = v;
            dd.appendChild(code);
        } else {
            dd.className = "muted";
            dd.textContent = "—";
        }
        layersList.append(dt, dd);
    }
    sec.appendChild(layersList);
    return sec;
}

function _metaItem(label, value) {
    const item = document.createElement("div");
    item.className = "project-meta-item";
    const l = document.createElement("div"); l.className = "label"; l.textContent = label;
    const v = document.createElement("div"); v.className = "value"; v.textContent = value;
    item.append(l, v);
    return item;
}

function _renderClassesOrAttributesSection(proj) {
    const sec = document.createElement("section");
    sec.className = "project-section";
    const h = document.createElement("h4");
    if (proj.kind === "vector") {
        h.append(_titleText("Atributos"),
                 _action("Editar atributos", () => openEditAttributesModal(proj), "primary"));
    } else {
        // Raster mask bytes encode class ids — once tiles exist, any class
        // edit (rename/recolor/remove) risks orphaning painted pixels.
        // Backend already blocks removal, but we lock the whole editor for
        // raster to keep the UX coherent: "if it's locked, don't tempt the
        // admin". Classification/detection still allow rename/recolor.
        const classesLocked = proj.kind === "raster" && proj.tile_geometry_locked;
        const btn = _action(
            "Editar classes",
            () => openEditClassesModal(proj),
            "primary",
        );
        if (classesLocked) {
            btn.disabled = true;
            btn.title = "Projetos de segmentação não permitem editar classes depois do primeiro tile — os bytes da máscara guardam o id da classe e mudar quebra dados existentes.";
        }
        h.append(_titleText("Classes"), btn);
    }
    sec.appendChild(h);
    if (proj.kind === "raster" && proj.tile_geometry_locked) {
        const p = document.createElement("p");
        p.className = "muted project-section-lead";
        p.textContent = "Classes estão travadas porque o projeto já possui tiles. Para mudar a paleta, clone o projeto.";
        sec.appendChild(p);
    }
    sec.appendChild(proj.kind === "vector"
        ? _renderAttributesReadonly(proj.attributes || [])
        : _renderClassesReadonly(proj.classes || []));
    return sec;
}

function _titleText(text) {
    const span = document.createElement("span");
    span.textContent = text;
    return span;
}

function _renderClassesReadonly(classes) {
    if (!classes.length) {
        return _emptyP("Nenhuma classe cadastrada.");
    }
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>ID</th><th>Nome</th><th>Cor</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const c of classes) {
        const tr = document.createElement("tr");
        const idTd = document.createElement("td"); idTd.textContent = c.id;
        const nameTd = document.createElement("td"); nameTd.textContent = c.name;
        const colorTd = document.createElement("td");
        const swatch = document.createElement("span");
        swatch.style.cssText = `display:inline-block;width:16px;height:16px;background:${c.color};border:1px solid rgba(0,0,0,.2);border-radius:3px;margin-right:6px;vertical-align:middle;`;
        const code = document.createElement("code"); code.textContent = c.color;
        colorTd.append(swatch, code);
        tr.append(idTd, nameTd, colorTd);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return table;
}

function _renderAttributesReadonly(attrs) {
    if (!attrs.length) return _emptyP("Nenhum atributo cadastrado.");
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>Chave</th><th>Label</th><th>Tipo</th><th>Obrigatório</th><th>Opções</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const a of attrs) {
        const tr = document.createElement("tr");
        for (const v of [a.key, a.label, a.type, a.required ? "sim" : "não",
                         (a.options || []).join(", ")]) {
            const td = document.createElement("td");
            td.textContent = v;
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return table;
}

function _emptyP(text) {
    const p = document.createElement("p");
    p.className = "muted project-section-lead";
    p.textContent = text;
    return p;
}

function _renderMembersSection(proj, members) {
    const sec = document.createElement("section");
    sec.className = "project-section";
    const h = document.createElement("h4");
    h.append(_titleText("Membros"),
             _action("+ Adicionar membro", () => openAddMemberModal(proj), "primary"));
    sec.appendChild(h);
    if (!members.length) {
        sec.appendChild(_emptyP("Nenhum membro adicionado. Use o botão acima para conceder acesso."));
        return sec;
    }
    const table = document.createElement("table");
    table.className = "admin-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>Usuário</th><th>Papel global</th><th>Papel no projeto</th><th>Ações</th></tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const m of members) {
        const tr = document.createElement("tr");
        const userTd = document.createElement("td"); userTd.textContent = m.username;
        const grTd = document.createElement("td"); grTd.textContent = m.global_role;
        const roleTd = document.createElement("td");
        const sel = document.createElement("select");
        for (const role of ["operator", "reviewer"]) {
            const o = document.createElement("option");
            o.value = role; o.textContent = role;
            if (m.project_role === role) o.selected = true;
            sel.appendChild(o);
        }
        sel.onchange = () => upsertMemberRole(proj.id, m.id, sel.value);
        roleTd.appendChild(sel);
        const actTd = document.createElement("td");
        actTd.appendChild(_action("Remover", () => removeMember(proj.id, m.id, m.username), "danger"));
        tr.append(userTd, grTd, roleTd, actTd);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    sec.appendChild(table);
    return sec;
}

function _renderExportSection(proj) {
    const sec = document.createElement("section");
    sec.className = "project-section";
    const h = document.createElement("h4");
    h.textContent = "Exportar dados";
    sec.appendChild(h);
    const p = document.createElement("p");
    p.className = "muted project-section-lead";
    p.innerHTML = `Formato: <strong>${escapeHtml(exportFormatLabel(proj.kind))}</strong>. ZIP inclui um manifest com status/autoria/bbox por tile.`;
    sec.appendChild(p);
    const bar = document.createElement("div");
    bar.className = "filter-bar";
    const select = document.createElement("select");
    select.id = "export-status";
    // "Somente classificados" was removed: exporting tiles that haven't gone
    // through review is rarely the right call — the reviewer step is where
    // the dataset gets its quality stamp. Admins who need the lower-quality
    // pool can still run the CLI export directly with --status=classified.
    for (const [v, label] of [
        ["reviewed", "Somente revisados"],
        ["reviewed_classified", "Revisados + classificados"],
    ]) {
        const o = document.createElement("option");
        o.value = v; o.textContent = label;
        select.appendChild(o);
    }
    const btn = document.createElement("button");
    btn.id = "btn-export";
    btn.className = "primary";
    btn.textContent = "Gerar export (ZIP)";
    btn.onclick = () => runExportJob(proj.id, select.value, btn);
    bar.append(select, btn);
    sec.appendChild(bar);
    const line = document.createElement("p");
    line.className = "muted";
    line.id = "export-status-line";
    sec.appendChild(line);
    return sec;
}

function exportFormatLabel(kind) {
    if (kind === "vector") return "GeoJSON (.geojson) + manifest.csv";
    if (kind === "classification") return "CSV (classifications.csv)";
    if (kind === "detection") return "GeoJSON de caixas (.geojson) + manifest.csv";
    return "GeoTIFF (.tif, EDGV) + manifest.csv";
}

// ---- Modais (create / edit / clone / members / classes / attrs) -----------

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
                <label class="modal-form-field hidden" id="pf-topo-row">
                    <span><input type="checkbox" id="pf-topology-required"
                                 ${proj?.topology_required ? "checked" : ""}> Validar topologia (grafo de drenagem)</span>
                </label>
                <label class="modal-form-field hidden" id="pf-box-row">
                    <span><input type="checkbox" id="pf-box-required"
                                 ${proj?.box_required ? "checked" : ""}> Exigir ≥1 caixa para submeter</span>
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
            <fieldset class="modal-form-fieldset hidden" id="pf-attrs-block">
                <legend>Atributos iniciais</legend>
                <div id="pf-attrs-editor"></div>
                <span class="modal-form-help">
                    ${isEdit
                        ? "Use \"Editar atributos\" no painel para alterar depois."
                        : "Adicione no mínimo 1 atributo."}
                </span>
            </fieldset>
        </div>
    `;
    const kindSel = host.querySelector("#pf-kind");
    const refreshKindUI = () => {
        const kind = kindSel.value;
        const hint = host.querySelector("#pf-kind-hint");
        if (hint) hint.textContent = KIND_HINTS[kind] || "";
        const isVector = kind === "vector";
        const isClassification = kind === "classification";
        const isDetection = kind === "detection";
        host.querySelector("#pf-mask-row").classList.toggle(
            "hidden", isVector || isClassification || isDetection);
        host.querySelector("#pf-topo-row").classList.toggle("hidden", !isVector);
        host.querySelector("#pf-box-row").classList.toggle("hidden", !isDetection);
        if (!isEdit) {
            host.querySelector("#pf-classes-block").classList.toggle("hidden", isVector);
            host.querySelector("#pf-attrs-block").classList.toggle("hidden", !isVector);
        }
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
        hint.textContent = `Tile no chão: ${ground.toFixed(1)} m × ${ground.toFixed(1)} m (${p}×${p} px @ ${m.toFixed(2)} m/px).${lockText}`;
    };
    px.addEventListener("input", refreshGeom);
    mpp.addEventListener("input", refreshGeom);
    refreshGeom();

    if (!isEdit) {
        _renderClassesEditor(host.querySelector("#pf-classes-editor"),
                             [{ id: 1, name: "classe_1", color: "#377eb8" }]);
        _renderAttributesEditor(host.querySelector("#pf-attrs-editor"),
                                [{ key: "tipo", label: "Tipo", type: "text", required: false, options: [] }]);
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
    if (kind === "vector") {
        const tr = host.querySelector("#pf-topology-required");
        if (tr) body.topology_required = tr.checked;
    }
    if (kind === "detection") {
        const br = host.querySelector("#pf-box-required");
        if (br) body.box_required = br.checked;
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
        if (kind === "vector") {
            body.attributes = _readAttributesEditor(host.querySelector("#pf-attrs-editor"));
            if (!body.attributes.length) {
                showToast("Adicione pelo menos 1 atributo.", "error");
                return null;
            }
        } else {
            body.classes = _readClassesEditor(host.querySelector("#pf-classes-editor"));
            if (!body.classes.length) {
                showToast("Adicione pelo menos 1 classe.", "error");
                return null;
            }
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

async function openEditAttributesModal(proj) {
    const result = await openModal({
        title: `Atributos — ${proj.name}`,
        size: "lg",
        submitLabel: "Salvar atributos",
        render: (host) => _renderAttributesEditor(host, proj.attributes || []),
        onSubmit: (host) => ({ attributes: _readAttributesEditor(host) }),
    });
    if (!result.confirmed) return;
    try {
        await apiPutJson(`/api/admin/projects/${proj.id}/attributes`, result.payload);
        showToast("Atributos salvos.", "success");
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
        <button type="button" data-act="add-class" class="add-row-btn">+ Adicionar classe</button>
    `;
    const tbody = host.querySelector("tbody");
    const addRow = (c) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td><input type="number" min="1" max="254" data-cf="id" value="${c.id}"></td>
            <td><input type="text" data-cf="name" value="${escapeHtml(c.name)}"></td>
            <td><input type="color" data-cf="color" value="${escapeHtml(c.color)}"></td>
            <td><button type="button" class="danger" data-act="remove">Remover</button></td>
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

function _renderAttributesEditor(host, initial) {
    host.innerHTML = `
        <table class="admin-table editor-table">
            <thead><tr>
                <th>Chave</th><th>Label</th><th style="width:8em">Tipo</th><th style="width:5em">Obrig.</th>
                <th>Opções (enum, vírgula)</th><th style="width:7em"></th>
            </tr></thead>
            <tbody></tbody>
        </table>
        <button type="button" data-act="add-attr" class="add-row-btn">+ Adicionar atributo</button>
    `;
    const tbody = host.querySelector("tbody");
    const addRow = (a) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td><input type="text" data-af="key" value="${escapeHtml(a.key || "")}" placeholder="snake_case"></td>
            <td><input type="text" data-af="label" value="${escapeHtml(a.label || "")}"></td>
            <td>
                <select data-af="type">
                    ${ATTR_TYPES.map(t => `<option value="${t}" ${a.type === t ? "selected" : ""}>${t}</option>`).join("")}
                </select>
            </td>
            <td><input type="checkbox" data-af="required" ${a.required ? "checked" : ""}></td>
            <td><input type="text" data-af="options"
                       value="${escapeHtml((a.options || []).join(","))}"
                       placeholder="opc1,opc2 (enum)"></td>
            <td><button type="button" class="danger" data-act="remove">Remover</button></td>
        `;
        tr.querySelector('[data-act="remove"]').onclick = () => tr.remove();
        tbody.appendChild(tr);
    };
    for (const a of initial) addRow(a);
    host.querySelector('[data-act="add-attr"]').onclick = () =>
        addRow({ key: "", label: "", type: "text", required: false, options: [] });
}

function _readAttributesEditor(host) {
    const out = [];
    for (const r of host.querySelectorAll("tbody tr")) {
        const key = r.querySelector('[data-af="key"]').value.trim();
        const label = r.querySelector('[data-af="label"]').value.trim();
        const type = r.querySelector('[data-af="type"]').value;
        const required = r.querySelector('[data-af="required"]').checked;
        const optStr = r.querySelector('[data-af="options"]').value.trim();
        const options = optStr ? optStr.split(",").map(s => s.trim()).filter(Boolean) : null;
        if (!key || !label) continue;
        const a = { key, label, type, required };
        if (options) a.options = options;
        out.push(a);
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

async function runExportJob(projectId, status, btn) {
    const line = document.getElementById("export-status-line");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Gerando...";
    const setLine = (t) => { if (line) line.textContent = t; };
    try {
        const job = await apiPostJson(
            `/api/admin/projects/${projectId}/export-jobs?status=${status}`, {});
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
        btn.textContent = prev;
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
