// Classification tile editor — MapLibre satellite + class-picker sidebar.
//
// Activation contract (called by editor.js):
//   await enterClassificationTile(tile, project)
//   getCurrentBody() -> string         // JSON {class_id} for submit
//   validateForSubmit() -> string[]    // empty when ok (class selected)
//   handleKeyDown(ev) -> boolean       // digit shortcuts; true when consumed
//   exitClassificationTile()           // dispose MapLibre + DOM
import { authHeader } from "./api.js";
import { showToast } from "./toast.js";
import { tileTransformRequest, makeRasterStyle } from "./maplib.js";
import { escapeHtml } from "./utils.js";
import { classIdForKey, tileFrameGeoJSON } from "./classification-core.js";

let _map = null;
let _project = null;
let _tile = null;
let _selectedClassId = null;
// Bumped on every enter/exit. An enter that resumes after its await with a
// stale token was superseded (tile/project switched mid-fetch) and must not
// touch the panel or create a map — otherwise the previous tile's class could
// be pre-selected on the new tile and submitted unnoticed.
let _enterSeq = 0;


export async function enterClassificationTile(tile, project) {
    const seq = ++_enterSeq;
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
            if (seq !== _enterSeq) return;
            if (r.status === 200) {
                const body = await r.json();
                if (typeof body?.class_id === "number") {
                    _selectClass(body.class_id);
                }
            }
        } catch (e) {
            if (seq !== _enterSeq) return;
            showToast(`Falha ao carregar classe atual: ${e.message}`, "err");
        }
    }
    if (seq !== _enterSeq) return;

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
    const map = _map;
    map.on("load", () => _addTileFrame(map, tile));
}

// Dim everything outside the tile and outline its bbox, so the operator
// classifies the tile itself and not the surrounding context.
function _addTileFrame(map, tile) {
    const { shade, outline } = tileFrameGeoJSON(tile);
    const accent = getComputedStyle(document.documentElement)
        .getPropertyValue("--accent").trim() || "#4fc3f7";
    map.addSource("tile-shade", { type: "geojson", data: shade });
    map.addSource("tile-outline", { type: "geojson", data: outline });
    map.addLayer({
        id: "tile-shade", type: "fill", source: "tile-shade",
        paint: { "fill-color": "#000000", "fill-opacity": 0.55 },
    });
    map.addLayer({
        id: "tile-outline", type: "line", source: "tile-outline",
        paint: { "line-color": accent, "line-width": 2 },
    });
}

// Digit 1–9 selects the Nth class. editor.js calls this after its own
// text-focus / modal guards, so it only sees keys meant for the editor.
export function handleKeyDown(ev) {
    if (!_tile || ev.ctrlKey || ev.altKey || ev.metaKey) return false;
    const cid = classIdForKey(ev.key, _project?.classes);
    if (cid == null) return false;
    ev.preventDefault();
    _selectClass(cid);
    return true;
}

export function exitClassificationTile() {
    _enterSeq++;
    _tile = null;
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
        <p class="classification-hint">Clique na classe (ou tecle 1–9) que melhor descreve a área dentro do contorno, então use o botão Submeter no rodapé.</p>
        <div class="classification-list" id="classification-list">
            ${classes.map((c, i) => `
                <button class="classification-class" data-class-id="${c.id}">
                    <span class="classification-swatch" style="background:${escapeHtml(c.color)}"></span>
                    <span class="classification-name">${escapeHtml(c.name)}</span>
                    ${i < 9 ? `<span class="kbd">${i + 1}</span>` : ""}
                </button>
            `).join("")}
        </div>
        <p class="classification-meta" id="classification-status">Nenhuma classe selecionada.</p>
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
