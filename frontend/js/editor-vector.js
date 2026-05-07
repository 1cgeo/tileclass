// Vector tile editor — MapLibre interactive surface for projects of kind=vector.
//
// Activation contract (called by editor.js):
//   await enterVectorTile(tile, project)  // load + render features
//   getCurrentBody() -> string             // JSON FC for submit/pause
//   exitVectorTile()                       // dispose MapLibre + listeners
//   onAttributesUpdated(handler)           // (UI shows mode pill / etc.)
//
// The footer (submit/pause/problem/request-changes) lives in editor.js;
// this module only manages the canvas area + a sidebar attribute panel.
import { apiGet } from "./api.js";
import { showToast } from "./toast.js";
import { tileTransformRequest, makeRasterStyle } from "./maplib.js";
import {
    addFeature, removeFeature, updateFeatureProps, snapToEndpoint,
    makeHistory, push, undo, redo,
    validateBeforeSubmit, emptyFC, SNAP_TOLERANCE_DEG,
} from "./vector-core.js";

let _map = null;
let _project = null;
let _tile = null;
let _fc = emptyFC();
let _history = makeHistory();
let _tool = "pen";       // 'pen' | 'select'
let _draftCoords = null;  // [[lng, lat], ...] in-progress feature
let _selectedIdx = -1;
let _onChange = () => {};

const VECTOR_LAYERS = {
    committed: "vec-committed",
    selected: "vec-selected",
    draft: "vec-draft",
    handles: "vec-handles",
};

// -------- Public ------------------------------------------------------------

export async function enterVectorTile(tile, project, { onChange = () => {} } = {}) {
    _project = project;
    _tile = tile;
    _onChange = onChange;
    _history = makeHistory();
    _selectedIdx = -1;
    _draftCoords = null;
    _setupContainer();
    _ensureSidebarPanel();
    _bindToolButtons();
    _bindKeyboard();

    let text = "";
    try {
        const r = await fetch(`/api/tiles/${tile.id}/features`, {
            headers: _authHeaders(),
        });
        if (r.status === 200) text = await r.text();
        else if (r.status === 204) text = JSON.stringify(emptyFC());
        else throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        showToast(`Falha ao carregar features: ${e.message}`, "err");
        text = JSON.stringify(emptyFC());
    }
    try {
        _fc = JSON.parse(text);
    } catch {
        _fc = emptyFC();
    }
    _history = push(_history, _fc);

    const [w, s, e, n] = [tile.bbox_west, tile.bbox_south, tile.bbox_east, tile.bbox_north];
    const primaryUrl = window.tileclassPrimaryUrl || "";  // injected by editor.js
    const maxZoom = window.tileclassPrimaryMaxZoom ?? 22;
    if (_map) { try { _map.remove(); } catch {}; _map = null; }
    _map = new maplibregl.Map({
        container: "map-vector",
        style: makeRasterStyle(primaryUrl, maxZoom),
        bounds: [[w, s], [e, n]],
        fitBoundsOptions: { padding: 0, animate: false },
        interactive: true,
        attributionControl: false,
        renderWorldCopies: false,
        transformRequest: tileTransformRequest,
        // Pin north-up; rotation/pitch break vertex-screen math otherwise.
        pitchWithRotate: false,
        dragRotate: false,
    });
    _map.on("load", () => {
        _addFeatureLayers();
        _wireMapEvents();
        _refreshLayers();
    });
}

export function exitVectorTile() {
    if (_map) {
        try { _map.remove(); } catch {}
        _map = null;
    }
    document.getElementById("map-vector")?.classList.add("hidden");
    document.getElementById("vector-attr-panel")?.classList.add("hidden");
    _draftCoords = null;
    _selectedIdx = -1;
}

export function getCurrentBody() {
    return JSON.stringify(_fc);
}

export function validateForSubmit() {
    return validateBeforeSubmit(_fc, _project?.attributes || [], {
        topologyRequired: !!_project?.topology_required,
    });
}

// -------- Container / sidebar --------------------------------------------

function _setupContainer() {
    // Hide raster canvas-stack and show map-vector div alongside it.
    const stack = document.getElementById("canvas-stack");
    if (stack) stack.style.display = "none";
    let mv = document.getElementById("map-vector");
    if (!mv) {
        mv = document.createElement("div");
        mv.id = "map-vector";
        mv.className = "map-vector";
        const wrap = document.querySelector(".canvas-wrap");
        if (wrap) wrap.appendChild(mv);
    }
    mv.classList.remove("hidden");
    mv.style.display = "block";
}

function _ensureSidebarPanel() {
    const left = document.querySelector(".sidebar-right") || document.querySelector(".sidebar");
    let panel = document.getElementById("vector-attr-panel");
    if (!panel) {
        panel = document.createElement("aside");
        panel.id = "vector-attr-panel";
        panel.className = "vector-attr-panel";
        document.body.appendChild(panel);
    }
    panel.classList.remove("hidden");
    panel.innerHTML = `
        <h3>Ferramentas vetoriais</h3>
        <div class="vec-tools">
            <button data-vtool="pen" class="vec-tool active" title="Pen (P)">Caneta</button>
            <button data-vtool="select" class="vec-tool" title="Selecionar (V)">Selecionar</button>
        </div>
        <div class="vec-hint" id="vector-hint">
            Pen: clique adiciona vértice; duplo-clique encerra; ESC cancela.
        </div>
        <div id="vector-attr-form" class="vector-attr-form hidden"></div>
        <p class="vec-meta" id="vector-meta">${_fc.features.length} feature(s)</p>
    `;
}

function _bindToolButtons() {
    document.querySelectorAll("[data-vtool]").forEach(b => {
        b.onclick = () => _setTool(b.dataset.vtool);
    });
}

function _setTool(t) {
    _tool = t;
    for (const b of document.querySelectorAll("[data-vtool]")) {
        b.classList.toggle("active", b.dataset.vtool === t);
    }
    _draftCoords = null;
    _selectedIdx = -1;
    _refreshAttrForm();
    _refreshLayers();
    const hint = document.getElementById("vector-hint");
    if (hint) {
        hint.textContent = t === "pen"
            ? "Pen: clique adiciona vértice; duplo-clique encerra; ESC cancela."
            : "Selecionar: clique numa linha → editar atributos; Del remove.";
    }
}

function _bindKeyboard() {
    if (window._vectorKbBound) return;
    window._vectorKbBound = true;
    window.addEventListener("keydown", (ev) => {
        if (!_map) return;
        if (ev.target?.tagName === "INPUT" || ev.target?.tagName === "TEXTAREA"
            || ev.target?.tagName === "SELECT") return;
        if (ev.key === "p" || ev.key === "P") _setTool("pen");
        else if (ev.key === "v" || ev.key === "V") _setTool("select");
        else if (ev.key === "Escape" && _draftCoords) {
            _draftCoords = null;
            _refreshLayers();
        } else if ((ev.key === "Delete" || ev.key === "Backspace")
                   && _tool === "select" && _selectedIdx >= 0) {
            _commit(removeFeature(_fc, _selectedIdx));
            _selectedIdx = -1;
            _refreshAttrForm();
        } else if ((ev.ctrlKey || ev.metaKey) && ev.key === "z") {
            const r = undo(_history, _fc);
            _history = r.history;
            _fc = r.snapshot;
            _refreshLayers(); _refreshMeta();
        } else if ((ev.ctrlKey || ev.metaKey) && (ev.key === "y" || (ev.shiftKey && ev.key === "Z"))) {
            const r = redo(_history, _fc);
            _history = r.history;
            _fc = r.snapshot;
            _refreshLayers(); _refreshMeta();
        }
    });
}

// -------- MapLibre: layers + events --------------------------------------

function _addFeatureLayers() {
    _map.addSource(VECTOR_LAYERS.committed, {
        type: "geojson", data: _fc,
    });
    _map.addLayer({
        id: VECTOR_LAYERS.committed,
        type: "line",
        source: VECTOR_LAYERS.committed,
        paint: {
            "line-color": "#377eb8",
            "line-width": 3,
            "line-opacity": 0.8,
        },
    });
    _map.addSource(VECTOR_LAYERS.selected, {
        type: "geojson", data: emptyFC(),
    });
    _map.addLayer({
        id: VECTOR_LAYERS.selected,
        type: "line",
        source: VECTOR_LAYERS.selected,
        paint: {
            "line-color": "#ff7f00",
            "line-width": 4,
        },
    });
    _map.addSource(VECTOR_LAYERS.draft, {
        type: "geojson", data: emptyFC(),
    });
    _map.addLayer({
        id: VECTOR_LAYERS.draft,
        type: "line",
        source: VECTOR_LAYERS.draft,
        paint: {
            "line-color": "#e41a1c",
            "line-width": 3,
            "line-dasharray": [2, 2],
        },
    });
    _map.addSource(VECTOR_LAYERS.handles, {
        type: "geojson", data: emptyFC(),
    });
    _map.addLayer({
        id: VECTOR_LAYERS.handles,
        type: "circle",
        source: VECTOR_LAYERS.handles,
        paint: {
            "circle-radius": 4,
            "circle-color": "#ffffff",
            "circle-stroke-color": "#e41a1c",
            "circle-stroke-width": 2,
        },
    });
}

function _wireMapEvents() {
    _map.on("click", (e) => {
        if (_tool === "pen") _onPenClick(e.lngLat);
        else if (_tool === "select") _onSelectClick(e);
    });
    _map.on("dblclick", (e) => {
        if (_tool === "pen" && _draftCoords && _draftCoords.length >= 2) {
            e.preventDefault();
            _finishDraft();
        }
    });
}

function _onPenClick(lngLat) {
    const point = [lngLat.lng, lngLat.lat];
    const snapped = snapToEndpoint(_fc, point);
    const v = snapped || point;
    if (!_draftCoords) _draftCoords = [v];
    else _draftCoords.push(v);
    _refreshLayers();
}

function _finishDraft() {
    if (!_draftCoords || _draftCoords.length < 2) return;
    // Snap first/last vertex too if there's an existing endpoint nearby.
    const first = snapToEndpoint(_fc, _draftCoords[0]) || _draftCoords[0];
    const last = snapToEndpoint(_fc, _draftCoords[_draftCoords.length - 1])
                  || _draftCoords[_draftCoords.length - 1];
    const coords = [first, ..._draftCoords.slice(1, -1), last];
    const initialProps = {};
    for (const a of _project?.attributes || []) {
        if (a.type === "boolean") initialProps[a.key] = false;
    }
    _commit(addFeature(_fc, coords, initialProps));
    _draftCoords = null;
    // Auto-select so the operator can fill attributes immediately.
    _selectedIdx = _fc.features.length - 1;
    _refreshAttrForm();
}

function _onSelectClick(e) {
    const features = _map.queryRenderedFeatures(e.point, {
        layers: [VECTOR_LAYERS.committed],
    });
    if (!features.length) {
        _selectedIdx = -1;
        _refreshAttrForm();
        _refreshLayers();
        return;
    }
    // Match the rendered feature back to its index in _fc.
    const ge = features[0].geometry;
    const idx = _fc.features.findIndex(f =>
        f.geometry.type === "LineString"
        && JSON.stringify(f.geometry.coordinates) === JSON.stringify(ge.coordinates),
    );
    if (idx >= 0) {
        _selectedIdx = idx;
        _refreshAttrForm();
        _refreshLayers();
    }
}

// -------- Refresh helpers ------------------------------------------------

function _refreshLayers() {
    if (!_map || !_map.isStyleLoaded()) return;
    _map.getSource(VECTOR_LAYERS.committed).setData(_fc);
    const sel = _selectedIdx >= 0
        ? { type: "FeatureCollection", features: [_fc.features[_selectedIdx]] }
        : emptyFC();
    _map.getSource(VECTOR_LAYERS.selected).setData(sel);
    const draft = _draftCoords && _draftCoords.length >= 2 ? {
        type: "FeatureCollection",
        features: [{
            type: "Feature",
            geometry: { type: "LineString", coordinates: _draftCoords },
            properties: {},
        }],
    } : emptyFC();
    _map.getSource(VECTOR_LAYERS.draft).setData(draft);
    const handles = _draftCoords ? {
        type: "FeatureCollection",
        features: _draftCoords.map(c => ({
            type: "Feature",
            geometry: { type: "Point", coordinates: c },
            properties: {},
        })),
    } : emptyFC();
    _map.getSource(VECTOR_LAYERS.handles).setData(handles);
}

function _refreshMeta() {
    const m = document.getElementById("vector-meta");
    if (m) m.textContent = `${_fc.features.length} feature(s)`;
}

function _commit(newFc) {
    _history = push(_history, _fc);
    _fc = newFc;
    _refreshLayers();
    _refreshMeta();
    _onChange();
}

// -------- Attribute form -------------------------------------------------

function _refreshAttrForm() {
    const form = document.getElementById("vector-attr-form");
    if (!form) return;
    if (_selectedIdx < 0 || !_fc.features[_selectedIdx]) {
        form.classList.add("hidden");
        form.innerHTML = "";
        return;
    }
    const props = _fc.features[_selectedIdx].properties || {};
    const schema = _project?.attributes || [];
    const directionRow = _project?.topology_required ? `
        <label class="field">
            <span>direção *</span>
            <select data-attr-key="direction">
                <option value="forward" ${props.direction === "forward" ? "selected" : ""}>→ forward</option>
                <option value="reverse" ${props.direction === "reverse" ? "selected" : ""}>← reverse</option>
                <option value="both" ${props.direction === "both" ? "selected" : ""}>↔ both</option>
            </select>
        </label>
    ` : "";
    const fields = schema
        .filter(a => a.key !== "direction")
        .map(a => _attrFieldHTML(a, props[a.key]))
        .join("");
    form.classList.remove("hidden");
    form.innerHTML = `
        <h4>Atributos da feature #${_selectedIdx + 1}</h4>
        ${directionRow}
        ${fields}
        <button id="vector-delete-feature" class="danger">Remover feature</button>
    `;
    form.querySelectorAll("[data-attr-key]").forEach(el => {
        el.onchange = () => {
            const key = el.dataset.attrKey;
            const value = el.type === "checkbox" ? el.checked
                        : el.type === "number" ? (el.value === "" ? null : Number(el.value))
                        : el.value;
            _commit(updateFeatureProps(_fc, _selectedIdx, { [key]: value }));
        };
    });
    document.getElementById("vector-delete-feature").onclick = () => {
        _commit(removeFeature(_fc, _selectedIdx));
        _selectedIdx = -1;
        _refreshAttrForm();
    };
}

function _attrFieldHTML(a, value) {
    const safe = (v) => (v == null ? "" : String(v).replace(/"/g, "&quot;"));
    const reqMark = a.required ? " *" : "";
    if (a.type === "enum") {
        const opts = (a.options || []).map(o =>
            `<option value="${safe(o)}" ${value === o ? "selected" : ""}>${safe(o)}</option>`,
        ).join("");
        return `
            <label class="field">
                <span>${safe(a.label)}${reqMark}</span>
                <select data-attr-key="${safe(a.key)}">
                    <option value="">—</option>
                    ${opts}
                </select>
            </label>
        `;
    }
    if (a.type === "boolean") {
        return `
            <label class="field">
                <input type="checkbox" data-attr-key="${safe(a.key)}" ${value ? "checked" : ""}>
                ${safe(a.label)}
            </label>
        `;
    }
    const inputType = a.type === "number" ? "number" : "text";
    return `
        <label class="field">
            <span>${safe(a.label)}${reqMark}</span>
            <input type="${inputType}" data-attr-key="${safe(a.key)}"
                   value="${safe(value)}">
        </label>
    `;
}

// -------- Auth header passthrough ----------------------------------------

function _authHeaders() {
    try {
        const tok = JSON.parse(localStorage.getItem("tileclass_tokens"))?.access_token;
        return tok ? { Authorization: `Bearer ${tok}` } : {};
    } catch {
        return {};
    }
}
