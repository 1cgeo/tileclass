// Classification tile editor — MapLibre satellite + class-picker sidebar.
//
// Activation contract (called by editor.js):
//   await enterClassificationTile(tile, project)
//   getCurrentBody() -> string         // JSON {class_id} for submit
//   validateForSubmit() -> string[]    // empty when ok (class selected)
//   exitClassificationTile()           // dispose MapLibre + DOM
import { authHeader } from "./api.js";
import { showToast } from "./toast.js";
import { tileTransformRequest, makeRasterStyle } from "./maplib.js";
import { escapeHtml } from "./utils.js";

let _map = null;
let _project = null;
let _tile = null;
let _selectedClassId = null;


export async function enterClassificationTile(tile, project) {
    _project = project;
    _tile = tile;
    _selectedClassId = null;
    _setupContainer();
    _renderClassPanel();

    // Pending tiles have no body — skip the GET to save a round-trip on
    // first open. Re-opened tiles (in_progress / in_review / classified)
    // need it to restore the previously chosen class.
    if (tile.status !== "pending") {
        try {
            const r = await fetch(`/api/tiles/${tile.id}/classification`, {
                headers: authHeader(),
            });
            if (r.status === 200) {
                const body = await r.json();
                if (typeof body?.class_id === "number") {
                    _selectClass(body.class_id);
                }
            }
        } catch (e) {
            showToast(`Falha ao carregar classe atual: ${e.message}`, "err");
        }
    }

    const [w, s, e, n] = [tile.bbox_west, tile.bbox_south, tile.bbox_east, tile.bbox_north];
    const primaryUrl = window.tileclassPrimaryUrl || "";
    const maxZoom = window.tileclassPrimaryMaxZoom ?? 22;
    if (_map) { try { _map.remove(); } catch {}; _map = null; }
    _map = new maplibregl.Map({
        container: "map-classification",
        style: makeRasterStyle(primaryUrl, maxZoom),
        bounds: [[w, s], [e, n]],
        fitBoundsOptions: { padding: 0, animate: false },
        interactive: true,
        attributionControl: false,
        renderWorldCopies: false,
        transformRequest: tileTransformRequest,
        pitchWithRotate: false,
        dragRotate: false,
    });
}

export function exitClassificationTile() {
    if (_map) {
        try { _map.remove(); } catch {}
        _map = null;
    }
    document.getElementById("map-classification")?.classList.add("hidden");
    document.getElementById("classification-panel")?.classList.add("hidden");
    // Restore the raster canvas-stack hidden by _setupContainer.
    const stack = document.getElementById("canvas-stack");
    if (stack) stack.style.display = "";
    _selectedClassId = null;
}

export function getCurrentBody() {
    return JSON.stringify({ class_id: _selectedClassId });
}

export function validateForSubmit() {
    if (_selectedClassId == null) {
        return ["Selecione uma classe antes de submeter."];
    }
    return [];
}


function _setupContainer() {
    const stack = document.getElementById("canvas-stack");
    if (stack) stack.style.display = "none";
    let mv = document.getElementById("map-classification");
    if (!mv) {
        mv = document.createElement("div");
        mv.id = "map-classification";
        mv.className = "map-classification";
        const wrap = document.querySelector(".canvas-wrap");
        if (wrap) wrap.appendChild(mv);
    }
    mv.classList.remove("hidden");
    mv.style.display = "block";
}

function _renderClassPanel() {
    let panel = document.getElementById("classification-panel");
    if (!panel) {
        panel = document.createElement("aside");
        panel.id = "classification-panel";
        panel.className = "classification-panel";
        document.body.appendChild(panel);
    }
    panel.classList.remove("hidden");
    const classes = _project?.classes || [];
    panel.innerHTML = `
        <h3>Escolha a classe</h3>
        <p class="vec-hint">Clique na classe que melhor descreve o tile, então use o botão Submeter no rodapé.</p>
        <div class="classification-list" id="classification-list">
            ${classes.map(c => `
                <button class="classification-class" data-class-id="${c.id}">
                    <span class="classification-swatch" style="background:${escapeHtml(c.color)}"></span>
                    <span class="classification-name">${escapeHtml(c.name)}</span>
                </button>
            `).join("")}
        </div>
        <p class="vec-meta" id="classification-status">Nenhuma classe selecionada.</p>
    `;
    for (const b of panel.querySelectorAll("[data-class-id]")) {
        b.onclick = () => _selectClass(parseInt(b.dataset.classId, 10));
    }
}

function _selectClass(cid) {
    _selectedClassId = cid;
    const panel = document.getElementById("classification-panel");
    if (!panel) return;
    for (const b of panel.querySelectorAll("[data-class-id]")) {
        b.classList.toggle("active", parseInt(b.dataset.classId, 10) === cid);
    }
    const cls = (_project?.classes || []).find(c => c.id === cid);
    const status = document.getElementById("classification-status");
    if (status) {
        status.textContent = cls ? `Selecionado: ${cls.name}` : "Nenhuma classe selecionada.";
    }
    // Footer submit button flips out of incomplete state when a class is chosen.
    window.tcRefreshSubmit?.();
}
