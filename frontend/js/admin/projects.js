// Admin tab: project CRUD + classes + members.
// Stays self-contained: every interaction (create, edit fields, replace
// classes, add/remove members) re-fetches the project list to keep the UI
// in sync with the backend cache invalidation.
import { apiGet, apiPostJson, apiPatchJson, apiPutJson, apiDelete } from "../api.js";
import { showToast } from "../toast.js";
import { escapeHtml } from "../utils.js";

const LAYER_FIELDS = [
    { key: "primary_mbtiles",          label: "Imagem primária (.mbtiles)",          required: true,  hint: "Caminho relativo a backend/ ou absoluto" },
    { key: "secondary_mbtiles",        label: "Imagem secundária (.mbtiles)",        required: false, hint: "Atalho D — opcional" },
    { key: "tertiary_mbtiles",         label: "Imagem terciária (.mbtiles)",         required: false, hint: "Atalho R — opcional" },
    { key: "ref_mask_primary_mbtiles", label: "Máscara de referência primária",      required: false, hint: "Atalho T — opcional, raster categorizado" },
    { key: "ref_mask_secondary_mbtiles", label: "Máscara de referência secundária", required: false, hint: "Atalho Y — opcional, raster categorizado" },
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
            <th>Nome</th><th>Descrição</th><th>Status</th><th>Máscara completa?</th><th></th>
        </tr></thead>
        <tbody></tbody>
    `;
    const tbody = tbl.querySelector("tbody");
    for (const p of projects) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td><strong>${escapeHtml(p.name)}</strong></td>
            <td>${escapeHtml(p.description || "")}</td>
            <td>${p.active ? "ativo" : "inativo"}</td>
            <td>${p.mask_complete_required ? "sim" : "não"}</td>
            <td><button class="link" data-pid="${p.id}">Editar</button></td>
        `;
        tr.querySelector("button").onclick = () => selectProject(p.id);
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
    return `
        <h3>${escapeHtml(proj.name)} <span class="muted">(id ${proj.id})</span></h3>

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
            <label class="field">
                <input type="checkbox" data-field="mask_complete_required" ${proj.mask_complete_required ? "checked" : ""}>
                Exigir máscara completa para submeter
            </label>
            <label class="field">
                <input type="checkbox" data-field="active" ${proj.active ? "checked" : ""}>
                Ativo
            </label>
            ${layerRows}
            <button class="primary" id="btn-save-project">Salvar</button>
        </section>

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
}

function readField(field) {
    const el = document.querySelector(`[data-field="${field}"]`);
    if (!el) return undefined;
    if (el.type === "checkbox") return el.checked;
    return el.value;
}

async function saveProjectFields(projectId) {
    const fields = {};
    for (const f of ["name", "description", "mask_complete_required", "active"]) {
        const v = readField(f);
        if (v !== undefined) fields[f] = v;
    }
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
                <span>Descrição</span>
                <input type="text" id="np-description">
            </label>
            <label class="field">
                <input type="checkbox" id="np-mask-required" checked>
                Exigir máscara completa para submeter
            </label>
            ${LAYER_FIELDS.map(f => `
                <label class="field">
                    <span>${escapeHtml(f.label)}${f.required ? " *" : ""}</span>
                    <input type="text" id="np-${f.key}" placeholder="${escapeHtml(f.hint || "")}">
                </label>
            `).join("")}
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
            <button class="primary" id="btn-create-project">Criar</button>
        </section>
    `;
    document.querySelector("[data-rm-cls]").onclick = (ev) => ev.target.closest("tr").remove();
    document.getElementById("btn-add-class").onclick = () => addClassRow();
    document.getElementById("btn-create-project").onclick = async () => {
        const body = {
            name: document.getElementById("np-name").value.trim(),
            description: document.getElementById("np-description").value,
            mask_complete_required: document.getElementById("np-mask-required").checked,
            classes: readClasses(),
        };
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
