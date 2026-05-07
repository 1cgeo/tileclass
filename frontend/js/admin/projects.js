// Admin tab: project CRUD + classes + members.
// Stays self-contained: every interaction (create, edit fields, replace
// classes, add/remove members) re-fetches the project list to keep the UI
// in sync with the backend cache invalidation.
import { apiGet, apiPostJson, apiPatchJson, apiPutJson, apiDelete } from "../api.js";
import { showToast } from "../toast.js";
import { escapeHtml } from "../utils.js";

// Each field accepts either a local mbtiles path (relative to backend/ or
// absolute) OR a remote tile-server URL template (Martin / TileServer-GL),
// e.g. https://martin.example.com/sat/{z}/{x}/{y}.webp. URLs must contain
// {z}, {x}, {y} placeholders.
const LAYER_FIELDS = [
    { key: "primary_mbtiles",          label: "Imagem primária",            required: true,  hint: "Path .mbtiles ou URL com {z}/{x}/{y}" },
    { key: "secondary_mbtiles",        label: "Imagem secundária",          required: false, hint: "Atalho D — path .mbtiles ou URL" },
    { key: "tertiary_mbtiles",         label: "Imagem terciária",           required: false, hint: "Atalho R — path .mbtiles ou URL" },
    { key: "ref_mask_primary_mbtiles", label: "Máscara de referência 1ª",   required: false, hint: "Atalho T — raster categorizado (path ou URL)" },
    { key: "ref_mask_secondary_mbtiles", label: "Máscara de referência 2ª", required: false, hint: "Atalho Y — raster categorizado (path ou URL)" },
];

export async function renderProjects(root) {
    root.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando projetos...</div>`;
    let projects = [];
    try {
        projects = await apiGet("/api/projects");
    } catch (e) {
        root.innerHTML = `<p class="error">Erro: ${escapeHtml(e.message)}</p>`;
        return;
    }
    root.innerHTML = `
        <div class="filter-bar">
            <button id="btn-new-project" class="primary">+ Novo projeto</button>
            <span class="muted">${projects.length} projeto(s)</span>
        </div>
        <div id="projects-list"></div>
        <div id="project-detail" class="project-detail hidden"></div>
    `;
    document.getElementById("btn-new-project").onclick = () => showProjectForm(root, null);
    renderProjectList(projects);
    if (projects.length) selectProject(projects[0].id);
}

function renderProjectList(projects) {
    const list = document.getElementById("projects-list");
    list.innerHTML = "";
    const tbl = document.createElement("table");
    tbl.className = "admin-table";
    tbl.innerHTML = `
        <thead><tr>
            <th>Nome</th><th>Tipo</th><th>Descrição</th><th>Status</th><th></th>
        </tr></thead>
        <tbody></tbody>
    `;
    const tbody = tbl.querySelector("tbody");
    for (const p of projects) {
        const kindChip = p.kind === "vector" ? "vetorial" : "matricial";
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td><strong>${escapeHtml(p.name)}</strong></td>
            <td>${kindChip}</td>
            <td>${escapeHtml(p.description || "")}</td>
            <td>${p.active ? "ativo" : "inativo"}</td>
            <td>
                <button class="link" data-act="edit" data-pid="${p.id}">Editar</button>
                ·
                <button class="link" data-act="clone" data-pid="${p.id}">Clonar</button>
            </td>
        `;
        for (const btn of tr.querySelectorAll("button")) {
            btn.onclick = btn.dataset.act === "clone"
                ? () => cloneProject(p)
                : () => selectProject(p.id);
        }
        tbody.appendChild(tr);
    }
    list.appendChild(tbl);
}

async function selectProject(projectId) {
    const detail = document.getElementById("project-detail");
    detail.classList.remove("hidden");
    detail.innerHTML = `<div class="loading-text"><span class="loading"></span> Carregando...</div>`;
    let proj, members;
    try {
        [proj, members] = await Promise.all([
            apiGet(`/api/projects/${projectId}`),
            apiGet(`/api/admin/projects/${projectId}/members`).catch(() => []),
        ]);
    } catch (e) {
        detail.innerHTML = `<p class="error">Erro: ${escapeHtml(e.message)}</p>`;
        return;
    }
    detail.innerHTML = renderProjectDetail(proj, members);
    wireProjectDetail(proj, members);
}

function renderProjectDetail(proj, members) {
    const isVector = proj.kind === "vector";
    const layerRows = LAYER_FIELDS.map(f => {
        const v = proj[f.key] || "";
        return `
            <label class="field">
                <span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
                <input type="text" data-field="${f.key}" value="${escapeHtml(v)}"
                       placeholder="${escapeHtml(f.hint || "")}">
            </label>
        `;
    }).join("");
    const classesRows = (proj.classes || []).map((c, i) => `
        <tr data-idx="${i}">
            <td><input type="number" min="1" max="254" data-cf="id" value="${c.id}" style="width:5em"></td>
            <td><input type="text" data-cf="name" value="${escapeHtml(c.name)}"></td>
            <td><input type="color" data-cf="color" value="${escapeHtml(c.color)}"></td>
            <td><button class="link" data-rm-cls="${i}">remover</button></td>
        </tr>
    `).join("");
    const attrRows = (proj.attributes || []).map((a, i) => renderAttrRow(a, i)).join("");
    const membersRows = (members || []).map(m => `
        <tr>
            <td>${escapeHtml(m.username)}</td>
            <td>${escapeHtml(m.global_role)}</td>
            <td>
                <select data-mem-uid="${m.id}">
                    <option value="operator" ${m.project_role === "operator" ? "selected" : ""}>operator</option>
                    <option value="reviewer" ${m.project_role === "reviewer" ? "selected" : ""}>reviewer</option>
                    <option value="admin" ${m.project_role === "admin" ? "selected" : ""}>admin</option>
                </select>
            </td>
            <td><button class="link" data-rm-mem="${m.id}">remover</button></td>
        </tr>
    `).join("");
    const inactiveBadge = proj.active ? "" :
        ' <span class="chip warn">desativado</span>';
    const kindBadge = isVector
        ? ' <span class="chip info">vetorial</span>'
        : ' <span class="chip">matricial</span>';
    const maskCompleteBlock = isVector ? `
            <label class="field">
                <input type="checkbox" data-field="topology_required" ${proj.topology_required ? "checked" : ""}>
                Validar topologia (drenagem como grafo: direção + conectividade + sem ciclos)
            </label>
    ` : `
            <label class="field">
                <input type="checkbox" data-field="mask_complete_required" ${proj.mask_complete_required ? "checked" : ""}>
                Exigir máscara completa para submeter
            </label>
    `;
    const geoLocked = proj.tile_geometry_locked;
    const tileMeters = (proj.tile_px || 256) * (proj.meters_per_pixel || 2.5);
    const lockedHint = geoLocked
        ? ' <span class="muted">(travado: projeto já tem tiles)</span>'
        : "";
    const tilePxTooltip = isVector
        ? "Em projeto vetorial, afeta só a resolução do thumbnail/overlay rasterizado — o editor MapLibre desenha em qualquer escala."
        : "Lado da máscara em pixels (canvas + bytes do PNG = tile_px²).";
    const geometryBlock = `
            <fieldset class="field" style="display:grid;grid-template-columns:auto auto;gap:8px;">
                <legend>Geometria do tile${lockedHint}</legend>
                <label class="field" title="${escapeHtml(tilePxTooltip)}">
                    <span>Lado em pixels (tile_px)</span>
                    <input type="number" step="1" min="1" max="4096"
                           data-field="tile_px"
                           value="${proj.tile_px ?? 256}"
                           ${geoLocked ? "disabled" : ""}>
                </label>
                <label class="field" title="Resolução no chão por pixel (m/px). Tile cobre tile_px × m/px metros.">
                    <span>Metros por pixel</span>
                    <input type="number" step="0.1" min="0.1" max="100"
                           data-field="meters_per_pixel"
                           value="${proj.meters_per_pixel ?? 2.5}"
                           ${geoLocked ? "disabled" : ""}>
                </label>
                <p class="muted" style="grid-column:1/3">
                    Tile no chão: <strong>${tileMeters.toFixed(1)} m × ${tileMeters.toFixed(1)} m</strong>
                    (${proj.tile_px || 256}×${proj.tile_px || 256} px @ ${(proj.meters_per_pixel ?? 2.5).toFixed(2)} m/px).
                    ${geoLocked
                        ? "Geometria fica imutável a partir do primeiro tile inserido."
                        : "Após o primeiro tile, esses campos travam (mask bytes e bbox dependem deles)."}
                </p>
            </fieldset>
    `;
    const classesOrAttrsSection = isVector ? `
        <section class="project-section">
            <h4>Atributos</h4>
            <p class="muted">Schema de propriedades por feature. Renomear/relabel é livre. Remover uma chave só é permitido quando nenhuma feature a referencia.</p>
            <table class="admin-table">
                <thead><tr>
                    <th>Chave</th><th>Label</th><th>Tipo</th><th>Obrig.</th>
                    <th>Opções (enum, vírgula)</th><th></th>
                </tr></thead>
                <tbody id="attrs-tbody">${attrRows}</tbody>
            </table>
            <button id="btn-add-attr">+ Adicionar atributo</button>
            <button class="primary" id="btn-save-attrs">Salvar atributos</button>
        </section>
    ` : `
        <section class="project-section">
            <h4>Classes</h4>
            <p class="muted">Renomear/recolorir é livre. Remover classes só é permitido se o projeto não tiver tiles.</p>
            <table class="admin-table">
                <thead><tr><th>ID</th><th>Nome</th><th>Cor</th><th></th></tr></thead>
                <tbody id="classes-tbody">${classesRows}</tbody>
            </table>
            <button id="btn-add-class">+ Adicionar classe</button>
            <button class="primary" id="btn-save-classes">Salvar classes</button>
        </section>
    `;
    return `
        <h3>
            ${escapeHtml(proj.name)}
            <span class="muted">(id ${proj.id})</span>${kindBadge}${inactiveBadge}
        </h3>

        <section class="project-section">
            <h4>Configuração</h4>
            <label class="field">
                <span>Nome</span>
                <input type="text" data-field="name" value="${escapeHtml(proj.name)}">
            </label>
            <label class="field">
                <span>Descrição</span>
                <input type="text" data-field="description" value="${escapeHtml(proj.description || "")}">
            </label>
            ${maskCompleteBlock}
            ${geometryBlock}
            <label class="field">
                <input type="checkbox" data-field="active" ${proj.active ? "checked" : ""}>
                Ativo
            </label>
            ${layerRows}
            <div class="form-actions">
                <button class="primary" id="btn-save-project">Salvar</button>
                <button id="btn-delete-project" class="danger">Excluir projeto</button>
            </div>
            <p class="muted">Excluir só funciona quando o projeto não tem tiles. Para parar de distribuir novas tarefas mas manter o histórico, desmarque <strong>Ativo</strong>.</p>
        </section>

        ${classesOrAttrsSection}

        <section class="project-section">
            <h4>Membros</h4>
            <table class="admin-table">
                <thead><tr><th>Usuário</th><th>Role global</th><th>Role no projeto</th><th></th></tr></thead>
                <tbody id="members-tbody">${membersRows}</tbody>
            </table>
            <div class="filter-bar">
                <input type="number" id="add-mem-uid" placeholder="ID do usuário" style="width:8em">
                <select id="add-mem-role">
                    <option value="operator">operator</option>
                    <option value="reviewer">reviewer</option>
                    <option value="admin">admin</option>
                </select>
                <button id="btn-add-member">Adicionar membro</button>
            </div>
        </section>
    `;
}

function wireProjectDetail(proj, members) {
    const btnSave = document.getElementById("btn-save-project");
    if (btnSave) btnSave.onclick = () => saveProjectFields(proj.id);
    const btnDelete = document.getElementById("btn-delete-project");
    if (btnDelete) btnDelete.onclick = () => deleteProject(proj.id, proj.name);
    const btnAddCls = document.getElementById("btn-add-class");
    if (btnAddCls) btnAddCls.onclick = () => addClassRow();
    const btnSaveCls = document.getElementById("btn-save-classes");
    if (btnSaveCls) btnSaveCls.onclick = () => saveClasses(proj.id);
    const btnAddMem = document.getElementById("btn-add-member");
    if (btnAddMem) btnAddMem.onclick = () => addMember(proj.id);
    document.querySelectorAll("[data-rm-mem]").forEach(b => {
        b.onclick = () => removeMember(proj.id, parseInt(b.dataset.rmMem, 10));
    });
    document.querySelectorAll("[data-mem-uid]").forEach(s => {
        s.onchange = () => upsertMemberRole(proj.id, parseInt(s.dataset.memUid, 10), s.value);
    });
    document.querySelectorAll("[data-rm-cls]").forEach(b => {
        b.onclick = () => {
            const tr = document.querySelector(`tr[data-idx="${b.dataset.rmCls}"]`);
            if (tr) tr.remove();
        };
    });
    // Vector-only attribute editor
    const btnAddAttr = document.getElementById("btn-add-attr");
    if (btnAddAttr) btnAddAttr.onclick = () => addAttrRow();
    const btnSaveAttrs = document.getElementById("btn-save-attrs");
    if (btnSaveAttrs) btnSaveAttrs.onclick = () => saveAttributes(proj.id);
    document.querySelectorAll("[data-rm-attr]").forEach(b => {
        b.onclick = () => {
            const tr = document.querySelector(`tr[data-aidx="${b.dataset.rmAttr}"]`);
            if (tr) tr.remove();
        };
    });
}

function readField(field) {
    const el = document.querySelector(`[data-field="${field}"]`);
    if (!el) return undefined;
    if (el.type === "checkbox") return el.checked;
    return el.value;
}

async function saveProjectFields(projectId) {
    const fields = {};
    for (const f of ["name", "description", "mask_complete_required",
                      "topology_required", "active"]) {
        const v = readField(f);
        if (v !== undefined) fields[f] = v;
    }
    // Geometry fields are disabled when the project has any tile, so the
    // input is absent from the form. readField returns undefined and the
    // PATCH never touches the server-side lock branch.
    const tilePxEl = document.querySelector('[data-field="tile_px"]');
    if (tilePxEl && !tilePxEl.disabled) fields.tile_px = parseInt(tilePxEl.value, 10);
    const mppEl = document.querySelector('[data-field="meters_per_pixel"]');
    if (mppEl && !mppEl.disabled) fields.meters_per_pixel = parseFloat(mppEl.value);
    for (const f of LAYER_FIELDS) {
        const v = readField(f.key);
        if (v !== undefined) fields[f.key] = v;
    }
    try {
        await apiPatchJson(`/api/admin/projects/${projectId}`, fields);
        showToast("Projeto salvo.", "ok");
        await selectProject(projectId);
        await refreshProjectList();
    } catch (e) {
        showToast(`Falha ao salvar: ${e.message}`, "err", 6000);
    }
}

function addClassRow() {
    const tbody = document.getElementById("classes-tbody");
    const idx = tbody.children.length;
    const tr = document.createElement("tr");
    tr.dataset.idx = String(idx);
    tr.innerHTML = `
        <td><input type="number" min="1" max="254" data-cf="id" value="${idx + 1}" style="width:5em"></td>
        <td><input type="text" data-cf="name" value="nova"></td>
        <td><input type="color" data-cf="color" value="#888888"></td>
        <td><button class="link" data-rm-cls="${idx}">remover</button></td>
    `;
    tr.querySelector("[data-rm-cls]").onclick = () => tr.remove();
    tbody.appendChild(tr);
}

function readClasses() {
    const rows = document.querySelectorAll("#classes-tbody tr");
    const out = [];
    for (const r of rows) {
        const id = parseInt(r.querySelector('[data-cf="id"]').value, 10);
        const name = r.querySelector('[data-cf="name"]').value.trim();
        const color = r.querySelector('[data-cf="color"]').value;
        if (Number.isFinite(id) && name) out.push({ id, name, color });
    }
    return out;
}

async function saveClasses(projectId) {
    const classes = readClasses();
    if (!classes.length) {
        showToast("Adicione pelo menos uma classe.", "err");
        return;
    }
    try {
        await apiPutJson(`/api/admin/projects/${projectId}/classes`, { classes });
        showToast("Classes salvas.", "ok");
        await selectProject(projectId);
    } catch (e) {
        showToast(`Falha: ${e.message}`, "err", 6000);
    }
}


// ---- Vector attributes editor ---------------------------------------------

const ATTR_TYPES = ["text", "number", "enum", "boolean"];

function renderAttrRow(a, idx) {
    const opts = (a.options || []).join(",");
    return `
        <tr data-aidx="${idx}">
            <td><input type="text" data-af="key" value="${escapeHtml(a.key || "")}"
                       placeholder="snake_case" style="width:10em"></td>
            <td><input type="text" data-af="label" value="${escapeHtml(a.label || "")}"></td>
            <td>
                <select data-af="type">
                    ${ATTR_TYPES.map(t => `<option value="${t}" ${a.type === t ? "selected" : ""}>${t}</option>`).join("")}
                </select>
            </td>
            <td><input type="checkbox" data-af="required" ${a.required ? "checked" : ""}></td>
            <td><input type="text" data-af="options" value="${escapeHtml(opts)}"
                       placeholder="opc1,opc2 (enum)"></td>
            <td><button class="link" data-rm-attr="${idx}">remover</button></td>
        </tr>
    `;
}

function addAttrRow() {
    const tbody = document.getElementById("attrs-tbody");
    if (!tbody) return;
    const idx = tbody.children.length;
    const wrap = document.createElement("tr");
    wrap.innerHTML = renderAttrRow({ type: "text" }, idx);
    const tr = wrap.firstElementChild;
    tr.querySelector("[data-rm-attr]").onclick = () => tr.remove();
    tbody.appendChild(tr);
}

function readAttributes() {
    const rows = document.querySelectorAll("#attrs-tbody tr");
    const out = [];
    for (const r of rows) {
        const key = r.querySelector('[data-af="key"]').value.trim();
        const label = r.querySelector('[data-af="label"]').value.trim();
        const type = r.querySelector('[data-af="type"]').value;
        const required = r.querySelector('[data-af="required"]').checked;
        const optStr = r.querySelector('[data-af="options"]').value.trim();
        const options = optStr
            ? optStr.split(",").map(s => s.trim()).filter(Boolean)
            : null;
        if (!key || !label) continue;
        const a = { key, label, type, required };
        if (options) a.options = options;
        out.push(a);
    }
    return out;
}

async function saveAttributes(projectId) {
    const attributes = readAttributes();
    try {
        await apiPutJson(
            `/api/admin/projects/${projectId}/attributes`,
            { attributes },
        );
        showToast("Atributos salvos.", "ok");
        await selectProject(projectId);
    } catch (e) {
        showToast(`Falha: ${e.message}`, "err", 6000);
    }
}

async function addMember(projectId) {
    const uid = parseInt(document.getElementById("add-mem-uid").value, 10);
    const role = document.getElementById("add-mem-role").value;
    if (!Number.isFinite(uid)) {
        showToast("Informe o ID do usuário.", "err");
        return;
    }
    try {
        await apiPostJson(`/api/admin/projects/${projectId}/members`, {
            user_id: uid, role,
        });
        await selectProject(projectId);
    } catch (e) {
        showToast(`Falha: ${e.message}`, "err", 6000);
    }
}

async function upsertMemberRole(projectId, userId, role) {
    try {
        await apiPostJson(`/api/admin/projects/${projectId}/members`, {
            user_id: userId, role,
        });
        showToast("Role atualizada.", "ok");
    } catch (e) {
        showToast(`Falha: ${e.message}`, "err", 6000);
    }
}

async function removeMember(projectId, userId) {
    if (!confirm("Remover este membro?")) return;
    try {
        await apiDelete(`/api/admin/projects/${projectId}/members/${userId}`);
        await selectProject(projectId);
    } catch (e) {
        showToast(`Falha: ${e.message}`, "err", 6000);
    }
}

async function deleteProject(projectId, projectName) {
    if (!confirm(`Excluir o projeto "${projectName}"? Esta ação só funciona se o projeto não tiver tiles.`)) return;
    try {
        await apiDelete(`/api/admin/projects/${projectId}`);
        showToast("Projeto excluído.", "ok");
        await refreshProjectList();
        document.getElementById("project-detail").classList.add("hidden");
    } catch (e) {
        showToast(`Falha ao excluir: ${e.message}`, "err", 8000);
    }
}


async function cloneProject(p) {
    const suggested = `${p.name}_copia`;
    const name = (prompt(
        `Nome do novo projeto (clone de "${p.name}"):`,
        suggested,
    ) || "").trim();
    if (!name) return;
    try {
        const created = await apiPostJson(
            `/api/admin/projects/${p.id}/clone`, { name },
        );
        showToast(`Projeto "${created.name}" criado.`, "ok");
        await refreshProjectList();
        await selectProject(created.id);
    } catch (e) {
        showToast(`Falha ao clonar: ${e.message}`, "err", 6000);
    }
}


async function refreshProjectList() {
    try {
        const projects = await apiGet("/api/projects");
        renderProjectList(projects);
    } catch {}
}

function showProjectForm(root, _) {
    const detail = document.getElementById("project-detail");
    detail.classList.remove("hidden");
    detail.innerHTML = `
        <h3>Novo projeto</h3>
        <section class="project-section">
            <label class="field">
                <span>Nome *</span>
                <input type="text" id="np-name" required>
            </label>
            <label class="field">
                <span>Tipo *</span>
                <select id="np-kind">
                    <option value="raster">Matricial (máscara pixel)</option>
                    <option value="vector">Vetorial (linhas com atributos)</option>
                </select>
            </label>
            <p class="muted">O tipo é imutável após a criação — para mudar, clone para um projeto novo.</p>
            <label class="field">
                <span>Descrição</span>
                <input type="text" id="np-description">
            </label>
            <label class="field" id="np-mask-row">
                <input type="checkbox" id="np-mask-required" checked>
                Exigir máscara completa para submeter
            </label>
            <label class="field hidden" id="np-topo-row">
                <input type="checkbox" id="np-topology-required">
                Validar topologia (drenagem como grafo)
            </label>
            <fieldset class="field" style="display:grid;grid-template-columns:auto auto;gap:8px;">
                <legend>Geometria do tile</legend>
                <label class="field">
                    <span>Lado em pixels (tile_px)</span>
                    <input type="number" id="np-tile-px" step="1" min="1" max="4096" value="256">
                </label>
                <label class="field">
                    <span>Metros por pixel</span>
                    <input type="number" id="np-mpp" step="0.1" min="0.1" max="100" value="2.5">
                </label>
                <p class="muted" style="grid-column:1/3" id="np-geom-derived">
                    Tile no chão: 640.0 m × 640.0 m (256×256 px @ 2.50 m/px).
                    <br>Após o primeiro tile inserido, esses valores travam — mask bytes e bbox dependem deles.
                </p>
            </fieldset>
            ${LAYER_FIELDS.map(f => `
                <label class="field">
                    <span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
                    <input type="text" id="np-${f.key}" placeholder="${escapeHtml(f.hint || "")}">
                </label>
            `).join("")}

            <div id="np-classes-block">
                <h4>Classes iniciais</h4>
                <table class="admin-table">
                    <thead><tr><th>ID</th><th>Nome</th><th>Cor</th><th></th></tr></thead>
                    <tbody id="classes-tbody">
                        <tr data-idx="0">
                            <td><input type="number" min="1" max="254" data-cf="id" value="1" style="width:5em"></td>
                            <td><input type="text" data-cf="name" value="classe_1"></td>
                            <td><input type="color" data-cf="color" value="#377eb8"></td>
                            <td><button class="link" data-rm-cls="0">remover</button></td>
                        </tr>
                    </tbody>
                </table>
                <button id="btn-add-class">+ Adicionar classe</button>
            </div>

            <div id="np-attrs-block" class="hidden">
                <h4>Atributos iniciais</h4>
                <p class="muted">Schema das propriedades por feature. Pode editar depois.</p>
                <table class="admin-table">
                    <thead><tr>
                        <th>Chave</th><th>Label</th><th>Tipo</th><th>Obrig.</th>
                        <th>Opções (enum, vírgula)</th><th></th>
                    </tr></thead>
                    <tbody id="attrs-tbody">
                        ${renderAttrRow({key: "tipo", label: "Tipo", type: "text"}, 0)}
                    </tbody>
                </table>
                <button id="btn-add-attr">+ Adicionar atributo</button>
            </div>

            <button class="primary" id="btn-create-project">Criar</button>
        </section>
    `;
    const kindSel = document.getElementById("np-kind");
    const refreshKindUI = () => {
        const isVector = kindSel.value === "vector";
        document.getElementById("np-classes-block").classList.toggle("hidden", isVector);
        document.getElementById("np-attrs-block").classList.toggle("hidden", !isVector);
        document.getElementById("np-mask-row").classList.toggle("hidden", isVector);
        document.getElementById("np-topo-row").classList.toggle("hidden", !isVector);
    };
    kindSel.onchange = refreshKindUI;
    refreshKindUI();

    // Live "tile no chão" derived label so admins see the consequence of
    // changing px / m/px before submitting the form.
    const pxInp = document.getElementById("np-tile-px");
    const mppInp = document.getElementById("np-mpp");
    const derivedP = document.getElementById("np-geom-derived");
    const refreshGeomDerived = () => {
        const px = parseInt(pxInp.value, 10) || 256;
        const mpp = parseFloat(mppInp.value) || 0;
        const m = px * mpp;
        derivedP.innerHTML = `
            Tile no chão: <strong>${m.toFixed(1)} m × ${m.toFixed(1)} m</strong>
            (${px}×${px} px @ ${mpp.toFixed(2)} m/px).
            <br>Após o primeiro tile inserido, esses valores travam — mask bytes e bbox dependem deles.
        `;
    };
    pxInp.addEventListener("input", refreshGeomDerived);
    mppInp.addEventListener("input", refreshGeomDerived);

    document.querySelector("[data-rm-cls]").onclick = (ev) => ev.target.closest("tr").remove();
    document.getElementById("btn-add-class").onclick = () => addClassRow();
    document.getElementById("btn-add-attr").onclick = () => addAttrRow();
    document.querySelector("[data-rm-attr]").onclick = (ev) => ev.target.closest("tr").remove();

    document.getElementById("btn-create-project").onclick = async () => {
        const isVector = kindSel.value === "vector";
        const body = {
            name: document.getElementById("np-name").value.trim(),
            kind: kindSel.value,
            description: document.getElementById("np-description").value,
            tile_px: parseInt(pxInp.value, 10) || 256,
            meters_per_pixel: parseFloat(mppInp.value) || 2.5,
        };
        if (isVector) {
            body.topology_required = document.getElementById("np-topology-required").checked;
            body.attributes = readAttributes();
        } else {
            body.mask_complete_required = document.getElementById("np-mask-required").checked;
            body.classes = readClasses();
        }
        for (const f of LAYER_FIELDS) {
            const v = document.getElementById(`np-${f.key}`).value.trim();
            if (v) body[f.key] = v;
            else if (f.required) {
                showToast(`Campo obrigatório: ${f.label}`, "err");
                return;
            }
        }
        try {
            const proj = await apiPostJson("/api/admin/projects", body);
            showToast("Projeto criado.", "ok");
            await refreshProjectList();
            await selectProject(proj.id);
        } catch (e) {
            showToast(`Falha ao criar: ${e.message}`, "err", 8000);
        }
    };
}
