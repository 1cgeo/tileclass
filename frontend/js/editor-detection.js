// Detection tile editor — MapLibre interactive surface for kind=detection.
//
// Activation contract (called by editor.js, mirrors editor-vector.js):
//   await enterDetectionTile(tile, project)  // load + render boxes
//   getCurrentBody() -> string                // JSON FC for submit/pause
//   validateForSubmit() -> string[]           // empty when ok
//   exitDetectionTile()                        // dispose MapLibre + listeners
//
// Operator picks an active class, drags a rectangle to create a box, or uses
// the select tool to re-class / delete. The footer lives in editor.js.
import { authHeader } from "./api.js";
import { showToast } from "./toast.js";
import { tileTransformRequest, makeRasterStyle } from "./maplib.js";
import {
    addBox, removeFeature, updateFeatureProps, hitTest, isDegenerate,
    makeHistory, push, undo, redo, validateBeforeSubmit, emptyFC,
} from "./detection-core.js";

let _map = null;
let _project = null;
let _tile = null;
let _fc = emptyFC();
let _history = makeHistory();
let _tool = "draw";        // 'draw' | 'select'
let _activeClassId = null;
let _drawStart = null;      // [lng, lat] while dragging a new box
let _draftRect = null;      // [[lng,lat],[lng,lat]] in-progress box corners
let _selectedIdx = -1;
let _kbHandler = null;

const LAYERS = {
    fill: "det-fill",
    outline: "det-outline",
    selected: "det-selected",
    draft: "det-draft",
};

// -------- Public ------------------------------------------------------------

export async function enterDetectionTile(tile, project) {
    _project = project;
    _tile = tile;
    _history = makeHistory();
    _selectedIdx = -1;
    _drawStart = null;
    _draftRect = null;
    _tool = "draw";
    const classes = project?.classes || [];
    _activeClassId = classes.length ? classes[0].id : null;

    _setupContainer();
    _ensureSidebarPanel();
    _bindKeyboard();

    let text = "";
    try {
        const r = await fetch(`/api/tiles/${tile.id}/features`, { headers: authHeader() });
        if (r.status === 200) text = await r.text();
        else if (r.status === 204) text = JSON.stringify(emptyFC());
        else throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        showToast(`Falha ao carregar caixas: ${e.message}`, "err");
        text = JSON.stringify(emptyFC());
    }
    try { _fc = JSON.parse(text); } catch { _fc = emptyFC(); }
    _history = push(_history, _fc);

    const [w, s, e, n] = [tile.bbox_west, tile.bbox_south, tile.bbox_east, tile.bbox_north];
    const primaryUrl = window.tileclassPrimaryUrl || "";
    const maxZoom = window.tileclassPrimaryMaxZoom ?? 22;
    if (_map) { try { _map.remove(); } catch {}; _map = null; }
    _map = new maplibregl.Map({
        container: "map-detection",
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
    _map.on("load", () => {
        _addLayers();
        _wireMapEvents();
        _applyToolMapState();
        _refreshLayers();
    });
}

export function exitDetectionTile() {
    if (_map) { try { _map.remove(); } catch {}; _map = null; }
    if (_kbHandler) {
        window.removeEventListener("keydown", _kbHandler);
        _kbHandler = null;
    }
    document.getElementById("map-detection")?.classList.add("hidden");
    document.getElementById("detection-panel")?.classList.add("hidden");
    const stack = document.getElementById("canvas-stack");
    if (stack) stack.style.display = "";
    _drawStart = null;
    _draftRect = null;
    _selectedIdx = -1;
}

export function getCurrentBody() {
    return JSON.stringify(_fc);
}

export function validateForSubmit() {
    const allowed = (_project?.classes || []).map(c => c.id);
    return validateBeforeSubmit(_fc, allowed, {
        boxRequired: !!_project?.box_required,
    });
}

// -------- Container / sidebar --------------------------------------------

function _setupContainer() {
    const stack = document.getElementById("canvas-stack");
    if (stack) stack.style.display = "none";
    let md = document.getElementById("map-detection");
    if (!md) {
        md = document.createElement("div");
        md.id = "map-detection";
        md.className = "map-vector";  // reuse the full-area map styling
        const wrap = document.querySelector(".canvas-wrap");
        if (wrap) wrap.appendChild(md);
    }
    md.classList.remove("hidden");
    md.style.display = "block";
}

function _ensureSidebarPanel() {
    let panel = document.getElementById("detection-panel");
    if (!panel) {
        panel = document.createElement("aside");
        panel.id = "detection-panel";
        panel.className = "vector-attr-panel";  // reuse panel styling
        document.body.appendChild(panel);
    }
    panel.classList.remove("hidden");
    _renderPanel();
}

function _renderPanel() {
    const panel = document.getElementById("detection-panel");
    if (!panel) return;
    const classes = _project?.classes || [];
    const classBtns = classes.map(c => `
        <button class="det-class" data-class-id="${c.id}"
                style="border-left:10px solid ${esc(c.color)}"
                title="${esc(c.name)}">${esc(c.name)}</button>
    `).join("");
    panel.innerHTML = `
        <h3>Caixas (detecção)</h3>
        <div class="vec-tools">
            <button data-dtool="draw" class="vec-tool active" title="Desenhar (B)">Desenhar</button>
            <button data-dtool="select" class="vec-tool" title="Selecionar (V)">Selecionar</button>
        </div>
        <div class="vec-hint" id="detection-hint">
            Desenhar: arraste para criar uma caixa da classe ativa.
        </div>
        <p class="det-active-label">Classe ativa:</p>
        <div class="det-classes" id="det-class-picker">${classBtns}</div>
        <div id="detection-sel" class="vector-attr-form hidden"></div>
        <p class="vec-meta" id="detection-meta">${_fc.features.length} caixa(s)</p>
    `;
    panel.querySelectorAll("[data-dtool]").forEach(b => {
        b.onclick = () => _setTool(b.dataset.dtool);
    });
    _bindClassPicker();
    _highlightActiveClass();
}

function _bindClassPicker() {
    document.querySelectorAll("#det-class-picker [data-class-id]").forEach(b => {
        b.onclick = () => {
            const cid = Number(b.dataset.classId);
            if (_tool === "select" && _selectedIdx >= 0) {
                // Re-class the selected box.
                _commit(updateFeatureProps(_fc, _selectedIdx, { class_id: cid }));
                _refreshSelForm();
            }
            _activeClassId = cid;
            _highlightActiveClass();
        };
    });
}

function _highlightActiveClass() {
    document.querySelectorAll("#det-class-picker [data-class-id]").forEach(b => {
        b.classList.toggle("active", Number(b.dataset.classId) === _activeClassId);
    });
}

function _setTool(t) {
    _tool = t;
    for (const b of document.querySelectorAll("[data-dtool]")) {
        b.classList.toggle("active", b.dataset.dtool === t);
    }
    _drawStart = null;
    _draftRect = null;
    _selectedIdx = -1;
    _applyToolMapState();
    _refreshSelForm();
    _refreshLayers();
    const hint = document.getElementById("detection-hint");
    if (hint) {
        hint.textContent = t === "draw"
            ? "Desenhar: arraste para criar uma caixa da classe ativa."
            : "Selecionar: clique numa caixa → trocar classe / Del remove. Arraste para mover o mapa.";
    }
}

function _applyToolMapState() {
    if (!_map) return;
    // In draw mode, dragging paints a box, so pan must be off.
    if (_tool === "draw") _map.dragPan.disable();
    else _map.dragPan.enable();
}

function _bindKeyboard() {
    if (_kbHandler) return;
    _kbHandler = (ev) => {
        if (["INPUT", "TEXTAREA", "SELECT"].includes(ev.target?.tagName)) return;
        if (ev.key === "b" || ev.key === "B") _setTool("draw");
        else if (ev.key === "v" || ev.key === "V") _setTool("select");
        else if (ev.key === "Escape" && _drawStart) {
            _drawStart = null; _draftRect = null; _refreshLayers();
        } else if ((ev.key === "Delete" || ev.key === "Backspace")
                   && _tool === "select" && _selectedIdx >= 0) {
            _commit(removeFeature(_fc, _selectedIdx));
            _selectedIdx = -1;
            _refreshSelForm();
        } else if ((ev.ctrlKey || ev.metaKey) && ev.key === "z") {
            const r = undo(_history, _fc); _history = r.history; _fc = r.snapshot;
            _selectedIdx = -1; _refreshLayers(); _refreshMeta(); _refreshSelForm();
        } else if ((ev.ctrlKey || ev.metaKey) && (ev.key === "y" || (ev.shiftKey && ev.key === "Z"))) {
            const r = redo(_history, _fc); _history = r.history; _fc = r.snapshot;
            _selectedIdx = -1; _refreshLayers(); _refreshMeta(); _refreshSelForm();
        }
    };
    window.addEventListener("keydown", _kbHandler);
}

// -------- MapLibre: layers + events --------------------------------------

function _classColorExpr() {
    // ["match", ["get","class_id"], id, color, ..., fallback]
    const expr = ["match", ["get", "class_id"]];
    for (const c of _project?.classes || []) expr.push(c.id, c.color);
    expr.push("#e41a1c");
    return expr;
}

function _addLayers() {
    const color = _classColorExpr();
    _map.addSource(LAYERS.fill, { type: "geojson", data: _fc });
    _map.addLayer({
        id: LAYERS.fill, type: "fill", source: LAYERS.fill,
        paint: { "fill-color": color, "fill-opacity": 0.18 },
    });
    _map.addLayer({
        id: LAYERS.outline, type: "line", source: LAYERS.fill,
        paint: { "line-color": color, "line-width": 2 },
    });
    _map.addSource(LAYERS.selected, { type: "geojson", data: emptyFC() });
    _map.addLayer({
        id: LAYERS.selected, type: "line", source: LAYERS.selected,
        paint: { "line-color": "#ffffff", "line-width": 3, "line-dasharray": [1, 1] },
    });
    _map.addSource(LAYERS.draft, { type: "geojson", data: emptyFC() });
    _map.addLayer({
        id: LAYERS.draft, type: "line", source: LAYERS.draft,
        paint: { "line-color": "#ffcc00", "line-width": 2, "line-dasharray": [2, 2] },
    });
}

function _wireMapEvents() {
    _map.on("mousedown", (e) => {
        if (_tool !== "draw") return;
        _drawStart = [e.lngLat.lng, e.lngLat.lat];
        _draftRect = null;
    });
    _map.on("mousemove", (e) => {
        if (_tool !== "draw" || !_drawStart) return;
        _draftRect = [_drawStart, [e.lngLat.lng, e.lngLat.lat]];
        _refreshDraft();
    });
    _map.on("mouseup", (e) => {
        if (_tool === "draw" && _drawStart) {
            const end = [e.lngLat.lng, e.lngLat.lat];
            if (_activeClassId != null && !isDegenerate(_drawStart, end)) {
                _commit(addBox(_fc, _drawStart, end, { class_id: _activeClassId }));
            }
            _drawStart = null;
            _draftRect = null;
            _refreshDraft();
            return;
        }
    });
    _map.on("click", (e) => {
        if (_tool === "select") _onSelectClick(e);
    });
}

function _onSelectClick(e) {
    const idx = hitTest(_fc, [e.lngLat.lng, e.lngLat.lat]);
    _selectedIdx = idx;
    if (idx >= 0) {
        // Sync the active class to the selected box for quick re-class.
        const cid = (_fc.features[idx].properties || {}).class_id;
        if (Number.isInteger(cid)) { _activeClassId = cid; _highlightActiveClass(); }
    }
    _refreshSelForm();
    _refreshLayers();
}

// -------- Refresh helpers ------------------------------------------------

function _refreshLayers() {
    if (!_map || !_map.isStyleLoaded()) return;
    _map.getSource(LAYERS.fill).setData(_fc);
    const sel = _selectedIdx >= 0 && _fc.features[_selectedIdx]
        ? { type: "FeatureCollection", features: [_fc.features[_selectedIdx]] }
        : emptyFC();
    _map.getSource(LAYERS.selected).setData(sel);
    _refreshDraft();
}

function _refreshDraft() {
    if (!_map || !_map.getSource(LAYERS.draft)) return;
    let data = emptyFC();
    if (_draftRect) {
        const [p0, p1] = _draftRect;
        const w = Math.min(p0[0], p1[0]), e = Math.max(p0[0], p1[0]);
        const s = Math.min(p0[1], p1[1]), n = Math.max(p0[1], p1[1]);
        data = {
            type: "FeatureCollection",
            features: [{
                type: "Feature",
                geometry: { type: "LineString", coordinates: [[w, s], [e, s], [e, n], [w, n], [w, s]] },
                properties: {},
            }],
        };
    }
    _map.getSource(LAYERS.draft).setData(data);
}

function _refreshMeta() {
    const m = document.getElementById("detection-meta");
    if (m) m.textContent = `${_fc.features.length} caixa(s)`;
}

function _commit(newFc) {
    _history = push(_history, _fc);
    _fc = newFc;
    _refreshLayers();
    _refreshMeta();
}

function _refreshSelForm() {
    const form = document.getElementById("detection-sel");
    if (!form) return;
    if (_selectedIdx < 0 || !_fc.features[_selectedIdx]) {
        form.classList.add("hidden");
        form.innerHTML = "";
        return;
    }
    const cid = (_fc.features[_selectedIdx].properties || {}).class_id;
    const cls = (_project?.classes || []).find(c => c.id === cid);
    form.classList.remove("hidden");
    form.innerHTML = `
        <h4>Caixa #${_selectedIdx + 1}</h4>
        <p>Classe: <strong>${cls ? esc(cls.name) : "—"}</strong>
           <br><span class="vec-hint">clique numa classe acima para trocar</span></p>
        <button id="detection-delete" class="danger">Remover caixa</button>
    `;
    document.getElementById("detection-delete").onclick = () => {
        _commit(removeFeature(_fc, _selectedIdx));
        _selectedIdx = -1;
        _refreshSelForm();
    };
}

function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
