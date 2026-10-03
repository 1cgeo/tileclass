// Canvas editor: MapLibre renders the XYZ satellite underlay; two overlay canvases
// (mask + cursor) sit georeferenced on top of the tile bbox.
import { apiGet, apiGetBlob, apiPostBytes, apiPostJson, onSessionWarning, logout as apiLogout } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap, disposeMap, setMapBbox, setOverlayVisible } from "./maplib.js";

// Context view shows CONTEXT_FACTOR × CONTEXT_FACTOR tiles around the
// paintable center (paintable is the central 1/CONTEXT_FACTOR). The CSS
// custom property `--ctx` on #map-satellite is set from this same constant
// at init so the satellite div size/offset stays in sync — bumping the
// factor here is enough to widen the visible context.
const CONTEXT_FACTOR = 7;
function expandBbox(bbox, factor) {
    const [w, s, e, n] = bbox;
    const cx = (w + e) / 2, cy = (s + n) / 2;
    const hw = (e - w) * factor / 2, hh = (n - s) * factor / 2;
    return [cx - hw, cy - hh, cx + hw, cy + hh];
}
import { hexToRgb, blobToImage, truncateName, kindLabel, withProjectParam, icon, ICON_SPRITE, statusLabel } from "./utils.js";
import { openModal } from "./admin/modals.js";
import {
    paintAt as corePaintAt,
    paintLine as corePaintLine,
    floodFill as coreFloodFill,
    toUndoEntry,
    applyPatch as coreApplyPatch,
    screenToLogical as coreScreenToLogical,
} from "./mask-core.js";
import { saveBackup as bkSave, loadBackup as bkLoad, clearBackup as bkClear } from "./backup.js";
import {
    resolveShortcut, gridGeometry, canReusePreload, heartbeatNeedsResume,
    topmostOpenModal, isAnyModalOpen, modalCancelControl,
    KEY_TO_OVERLAY, MAX_NUMBER_SHORTCUTS,
    fitZoom, queueSummary, initials, rasterSubmitState, readCanvasPalette, tileFacts,
} from "./editor-core.js";
import {
    enterClassificationTile, exitClassificationTile,
    getCurrentBody as getClassificationBody,
    validateForSubmit as validateClassification,
    handleKeyDown as classificationKeyDown,
} from "./editor-classification.js";

// Tile geometry is per-project (project.tile_px). These are mutated on
// project load via setTileGeometry. DISPLAY is the on-screen canvas size
// in CSS pixels — independent of TILE; SCALE renders TILE-space onto it.
let TILE = 256;
const DISPLAY = 768;
let SCALE = DISPLAY / TILE;
let PIXELS = TILE * TILE;
const BRUSH_SIZES = [1, 3, 5, 7, 11];
// Ground size of one mask pixel (projects.meters_per_pixel) — drives the grid
// line thickness. Set in setTileGeometry.
let METERS_PER_PIXEL = 2.5;

// --- State ---
let currentTile = null;
let currentUser = null;
let classes = [];
let classesById = {};
let colorLut = new Uint8ClampedArray(256 * 4);
let darkLut = new Uint8ClampedArray(256 * 4); // for outlines
let colorLut32 = new Uint32Array(colorLut.buffer);
let mask = new Uint8Array(PIXELS);
let filledCount = 0;
let activeClass = 1;
let tool = "brush";
let brushSizeIdx = 1;
let maskOpacity = 0.5;
let outlineOnly = false;
let maskHidden = false;
let missingHighlight = false;  // H: persistent highlight of unfilled (255) pixels
let nextMissingCursor = 0;     // N: walks through missing pixels in raster order
let tileserverUrl = "";
let tileserverMaxZoom = 22;
// Overlays are hold-to-show; each cfg is { url, minZoom?, maxZoom? } or null
// for layers the project has not configured.
const overlayCfg = { secondary: null, tertiary: null, ref_primary: null, ref_secondary: null };
const overlayHeld = { secondary: false, tertiary: false, ref_primary: false, ref_secondary: false };
let activeProjectId = null;
let maskCompleteRequired = true;
let todayCount = 0;

// Preload cache for the "next" tile while user paints. `_preloadToken` is
// bumped whenever the cache is invalidated so an in-flight preload that
// started before a pause/problem/switch can't repopulate it afterwards.
let preloadedNext = null;
let _preloadToken = 0;
function clearPreload() {
    preloadedNext = null;
    _preloadToken++;
}

// Test hook only — never gate production logic on this.
let _tileReady = false;

// Undo/redo
const undoStack = [];
const redoStack = [];
const MAX_UNDO = 50;

// Drawing gesture
let drawing = false;
let gestureTouched = null;
let lastPx = null;

// --- DOM / canvases ---
const canvasMask = document.getElementById("canvas-mask");
const ctxMask = canvasMask.getContext("2d");
const canvasCursor = document.getElementById("canvas-cursor");
const ctxCursor = canvasCursor.getContext("2d");

// Offscreen TILE×TILE for mask imageData. Reallocated on project change
// via setTileGeometry — the editor handles operators with multiple projects
// at different sizes.
let maskImageData = ctxMask.createImageData(TILE, TILE);
// Uint32 view over maskImageData's pixel buffer for fast per-pixel writes in
// writeMaskPixels. MUST be rebuilt whenever maskImageData is reallocated
// (setTileGeometry) — a stale view writes to an orphaned buffer and the mask
// silently stops rendering on screen (the underlying `mask` array stays valid).
let maskData32 = new Uint32Array(maskImageData.data.buffer);
const offCanvas = document.createElement("canvas");
offCanvas.width = TILE; offCanvas.height = TILE;
const offCtx = offCanvas.getContext("2d");

function setTileGeometry(px, metersPerPixel = 2.5) {
    TILE = px;
    METERS_PER_PIXEL = metersPerPixel > 0 ? metersPerPixel : 2.5;
    PIXELS = TILE * TILE;
    SCALE = DISPLAY / TILE;
    maskImageData = ctxMask.createImageData(TILE, TILE);
    maskData32 = new Uint32Array(maskImageData.data.buffer);
    offCanvas.width = TILE; offCanvas.height = TILE;
    mask = new Uint8Array(PIXELS);
    filledCount = 0;
    undoStack.length = 0;
    redoStack.length = 0;
}

let satMap = null;
// Config the live satMap was built from (see currentMapKey).
let satMapKey = null;
function currentMapKey() {
    return JSON.stringify([tileserverUrl, tileserverMaxZoom, overlayCfg]);
}

// Zoom/pan state for the canvas-stack. zoom=1 fits the paintable area in the
// viewport; higher values zoom into the 256x256 tile. panX/panY are in CSS
// pixels at the current zoom (applied AFTER scale via translate).
let zoom = 1;
let panX = 0, panY = 0;
// MIN_ZOOM lets the operator shrink the stack until the full satellite
// context (CONTEXT_FACTOR × tile) fits in the original paintable area's
// footprint. A small extra margin (×0.9) lets them go slightly further to
// see the whole context without it touching the viewport edges.
const MIN_ZOOM = 0.9 / CONTEXT_FACTOR;
const MAX_ZOOM = 16;
let spaceHeld = false;
// Tracks that Z is physically still down after a Ctrl+Z chord. Without this,
// the OS autorepeat on Z (after Ctrl is released first) would fire keydowns
// with ctrlKey=false and trip the opacity shortcut.
let zSuppressed = false;
let panning = false;
let panStart = null;  // { clientX, clientY, panX, panY }

const canvasGrid = document.getElementById("canvas-grid");
const ctxGrid = canvasGrid.getContext("2d");
// Paint-pixel grid: one line per mask pixel, ~0.5 m thick on the ground.
// Geometry (cell, line width, visibility at the current zoom) comes from
// editor-core.gridGeometry, recomputed on every draw so tile_px /
// meters_per_pixel changes and zoom are always reflected.

let _editorInitialized = false;

// Theme-dependent canvas colors (see CANVAS_PALETTE_VARS in editor-core).
let palette = {};
function refreshCanvasPalette() {
    const cs = getComputedStyle(document.getElementById("view-editor"));
    palette = readCanvasPalette((name) => cs.getPropertyValue(name));
}

function renderUserBadge(user) {
    document.getElementById("user-label").textContent = user.username;
    const av = document.getElementById("user-avatar");
    if (av) {
        av.textContent = initials(user.username);
        av.title = user.username;
    }
}

export async function initEditor(user) {
    currentUser = user;
    if (_editorInitialized) {
        renderUserBadge(user);
        await enterEditor();
        return;
    }
    // Test hook: when ?test=1 is present in the URL, expose internal state so
    // E2E tests can inspect the mask and drive state transitions. Never enabled
    // in normal use. Do not add production logic that depends on __tcTest__.
    if (new URLSearchParams(location.search).get("test") === "1") {
        window.__tcTest__ = {
            get mask() { return mask; },
            get currentTile() { return currentTile; },
            get filledCount() { return filledCount; },
            get undoStackLen() { return undoStack.length; },
            get redoStackLen() { return redoStack.length; },
            get maskHidden() { return maskHidden; },
            get preloadedNextId() { return preloadedNext?.tile?.id ?? null; },
            get pausedPromptVisible() {
                const el = document.getElementById("paused-resume-screen");
                return !!el && !el.classList.contains("hidden");
            },
            get tileReady() { return _tileReady; },
            // Primary imagery URL / overlay layers of the live MapLibre map
            // (read from the map's style, not from module config).
            get satSourceUrl() {
                try { return satMap?.getStyle()?.sources?.sat?.tiles?.[0] ?? null; }
                catch { return null; }
            },
            get satOverlayKeys() {
                try {
                    return Object.keys(satMap?.getStyle()?.sources || {}).filter(k => k !== "sat");
                } catch { return []; }
            },
            // Fire the heartbeat now instead of waiting for the interval.
            heartbeatNow() {
                return currentTile ? sendHeartbeat(currentTile.id) : Promise.resolve();
            },
        };
    }
    renderUserBadge(user);
    refreshCanvasPalette();
    // Drive #map-satellite size/offset from CONTEXT_FACTOR — keeps CSS in sync
    // when the constant is bumped without editing both files.
    document.getElementById("map-satellite").style.setProperty("--ctx", CONTEXT_FACTOR);
    onSessionWarning(() => {
        showToast("Sua sessão expira em breve. Submeta seu trabalho e faça login novamente.", "warn", 30_000);
    });
    await refreshActiveProjectConfig();
    buildClassPanel();
    buildColorLut();
    attachEvents();
    applySecondaryButtonLabels();
    // Expose to non-raster editors so they can refresh the submit button
    // after every state mutation without importing this module (avoids the
    // circular import: editor.js already imports editor-classification.js).
    window.tcRefreshSubmit = refreshSubmitState;
    // Canvas colors are token-driven: re-read and repaint on theme switch.
    window.addEventListener("tc-themechange", () => {
        refreshCanvasPalette();
        blitMask();
        drawGrid();
    });
    // Keep the paint area fitted to the stage while the operator hasn't
    // zoomed/panned away from the fitted view.
    let resizeRaf = 0;
    window.addEventListener("resize", () => {
        cancelAnimationFrame(resizeRaf);
        resizeRaf = requestAnimationFrame(() => {
            if (zoom === _fittedZoom && panX === 0 && panY === 0) resetView();
        });
    });
    _editorInitialized = true;
    await enterEditor();
}


// ---- Project bootstrap ------------------------------------------------------

const LS_ACTIVE_PROJECT = "tileclass_active_project_id";

async function refreshActiveProjectConfig() {
    /** Pick the active project (last-used → first), load its full config,
     * paint the header chip. The "trocar" button + Ctrl+P open the picker
     * modal when the user belongs to more than one project. */
    const projects = await apiGet("/api/projects");
    if (!projects || !projects.length) {
        showToast("Nenhum projeto disponível para o seu usuário.", "err", 8000);
        throw new Error("no projects");
    }
    let saved = null;
    try { saved = parseInt(localStorage.getItem(LS_ACTIVE_PROJECT), 10); } catch {}
    const initial = projects.find(p => p.id === saved) || projects[0];
    activeProjectId = initial.id;
    try { localStorage.setItem(LS_ACTIVE_PROJECT, String(activeProjectId)); } catch {}
    await loadProjectConfig(activeProjectId);
    _renderProjectHeader(projects);
}

// Header chip: name + kind chip always visible; the "trocar" button only
// surfaces when the user belongs to >1 project. Called whenever the active
// project is set/loaded.
function _renderProjectHeader(projects) {
    const proj = window.tileclassActiveProject;
    const nameEl = document.getElementById("editor-project-name");
    const kindEl = document.getElementById("editor-project-kind");
    if (nameEl) nameEl.textContent = proj?.name || "—";
    if (kindEl) {
        kindEl.textContent = kindLabel(proj?.kind);
        kindEl.className = "chip project-kind-chip";
        kindEl.dataset.kind = proj?.kind || "";
    }
    // Single project: the control stays as a read-only label (no chevron).
    const btn = document.getElementById("btn-switch-project");
    if (btn) {
        const single = !projects || projects.length <= 1;
        btn.disabled = single;
        btn.classList.toggle("single", single);
        btn.title = single ? "Projeto ativo" : "Trocar de projeto (Ctrl+P)";
    }
}

async function _switchProject(newId, { autoLoadNext = false } = {}) {
    if (!Number.isFinite(newId) || newId === activeProjectId) return;
    activeProjectId = newId;
    try { localStorage.setItem(LS_ACTIVE_PROJECT, String(newId)); } catch {}
    currentTile = null;
    stopHeartbeat();
    clearPreload();
    // Tear down the previous project's per-kind editor before swapping
    // config (avoids a leaked MapLibre map / hidden raster canvas).
    exitActiveKindEditor();
    await loadProjectConfig(newId);
    buildClassPanel();
    buildColorLut();
    // Reload the project list so the chip + "trocar" visibility stay in sync.
    try {
        const projects = await apiGet("/api/projects");
        _renderProjectHeader(projects);
    } catch {}
    // `autoLoadNext` skips enterEditor's resume/idle flow and jumps straight
    // to `loadNext` — used by the cross-project auto-fallback so the
    // operator transitions seamlessly into the new queue.
    if (autoLoadNext) await loadNext();
    else await enterEditor();
}

// The generic shell modal (#modal-shell) lives inside #view-admin, which is
// display:none while the editor is shown — a modal there would be invisible.
// While the switcher is open it is hosted at <body> level, then put back.
async function withVisibleShell(fn) {
    const shell = document.getElementById("modal-shell");
    if (!shell) return fn();
    const parent = shell.parentNode, next = shell.nextSibling;
    document.body.appendChild(shell);
    shell.classList.add("editor-shell");
    try {
        return await fn();
    } finally {
        shell.classList.remove("editor-shell");
        parent.insertBefore(shell, next);
    }
}

function _switcherItem(p, list) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "project-switcher-item";
    if (p.id === activeProjectId) item.classList.add("active", "current");
    item.dataset.pid = String(p.id);
    item.setAttribute("aria-pressed", String(p.id === activeProjectId));

    const ic = document.createElement("span");
    ic.className = "project-switcher-icon";
    ic.appendChild(icon(p.kind === "classification" ? "tag" : "layers"));

    const body = document.createElement("span");
    body.className = "project-switcher-body";
    const top = document.createElement("span");
    top.className = "project-switcher-top";
    const name = document.createElement("span");
    name.className = "project-switcher-name";
    name.textContent = p.name;
    const kind = document.createElement("span");
    kind.className = "chip project-kind-chip";
    kind.dataset.kind = p.kind || "";
    kind.textContent = kindLabel(p.kind);
    top.append(name, kind);
    if (p.id === activeProjectId) {
        const cur = document.createElement("span");
        cur.className = "project-switcher-current";
        cur.textContent = "atual";
        top.appendChild(cur);
    }
    const bottom = document.createElement("span");
    bottom.className = "project-switcher-bottom";
    const count = document.createElement("span");
    count.className = "project-switcher-count" + (p.available > 0 ? "" : " empty");
    count.textContent = p.available > 0
        ? `${p.available} disponíve${p.available === 1 ? "l" : "is"}`
        : "Sem trabalho no momento";
    bottom.appendChild(count);
    // Breakdown: what's pending vs already assigned vs awaiting review.
    if (p.available > 0) {
        const parts = [];
        if (p.assigned_to_me) parts.push(`${p.assigned_to_me} atribuído${p.assigned_to_me === 1 ? "" : "s"} a você`);
        if (p.pending) parts.push(`${p.pending} pendente${p.pending === 1 ? "" : "s"}`);
        if (p.review_queue) parts.push(`${p.review_queue} para revisar`);
        const detail = document.createElement("span");
        detail.className = "project-switcher-detail";
        detail.textContent = parts.join(" · ");
        bottom.appendChild(detail);
    }
    body.append(top, bottom);

    const check = document.createElement("span");
    check.className = "project-switcher-check";
    check.appendChild(icon("check"));

    item.append(ic, body, check);
    item.onclick = () => {
        list.querySelectorAll(".project-switcher-item.active").forEach(el => {
            el.classList.remove("active");
            el.setAttribute("aria-pressed", "false");
        });
        item.classList.add("active");
        item.setAttribute("aria-pressed", "true");
    };
    // Double-click picks and confirms in one go.
    item.ondblclick = () => document.getElementById("shell-ok")?.click();
    return item;
}

async function openSwitchProjectModal() {
    let workload;
    try {
        workload = await apiGet("/api/me/projects");
    } catch (e) {
        showToast(`Erro ao listar projetos: ${e.message}`, "err");
        return;
    }
    if (!workload.length) return;
    const result = await withVisibleShell(() => openModal({
        title: "Trocar de projeto",
        size: "md",
        submitLabel: "Abrir projeto",
        render: (host) => {
            host.replaceChildren();
            const hint = document.createElement("p");
            hint.className = "hint project-switcher-hint";
            hint.textContent = "Escolha o projeto em que vai trabalhar. A fila e as classes mudam junto.";
            const list = document.createElement("div");
            list.className = "project-switcher-list";
            list.setAttribute("role", "list");
            host.append(hint, list);
            for (const p of workload) list.appendChild(_switcherItem(p, list));
        },
        onSubmit: (host) => {
            const sel = host.querySelector(".project-switcher-item.active");
            const pid = sel ? parseInt(sel.dataset.pid, 10) : null;
            return Number.isFinite(pid) ? { pid } : null;
        },
    }));
    if (!result.confirmed) return;
    await _switchProject(result.payload.pid);
}

async function loadProjectConfig(projectId) {
    const proj = await apiGet(`/api/projects/${projectId}`);
    classes = proj.classes || [];
    classesById = Object.fromEntries(classes.map(c => [c.id, c]));
    maskCompleteRequired = !!proj.mask_complete_required;
    // Apply per-project tile geometry. setTileGeometry rebuilds the mask
    // and offscreen surfaces — must run before any blit/paint code reads TILE.
    setTileGeometry(proj.tile_px || 256, proj.meters_per_pixel);
    const layers = proj.layers || {};
    const primary = layers.primary;
    tileserverUrl = primary?.url || "";
    tileserverMaxZoom = primary?.max_zoom ?? 22;
    // editor-classification reads these from window — keeps that module decoupled
    // from this file's module-scoped state.
    window.tileclassPrimaryUrl = tileserverUrl;
    window.tileclassPrimaryMaxZoom = tileserverMaxZoom;
    window.tileclassActiveProject = proj;
    for (const k of Object.keys(overlayCfg)) {
        const info = layers[k];
        overlayCfg[k] = info && info.url
            ? { url: info.url, minZoom: info.min_zoom ?? 0, maxZoom: info.max_zoom ?? 22 }
            : null;
    }
    // The satellite map bakes the primary URL / max zoom / overlay sources
    // into its style at creation. Another project's imagery needs a fresh
    // map — renderSatellite recreates it on the next tile.
    if (satMap && satMapKey !== currentMapKey()) {
        for (const key of Object.keys(overlayHeld)) releaseOverlay(key);
        satMap = disposeMap(satMap);
        satMapKey = null;
    }
    // data-kind drives CSS: hides raster-only chrome (sidebars, undo/redo,
    // pause-in-classification) when the project isn't raster.
    const view = document.getElementById("view-editor");
    if (view) view.dataset.kind = proj.kind || "raster";
    refreshShortcutsBadges();
}

function refreshShortcutsBadges() {
    for (const el of document.querySelectorAll("[data-overlay-key]")) {
        const layer = KEY_TO_OVERLAY[el.getAttribute("data-overlay-key")];
        el.classList.toggle("hidden", !overlayCfg[layer]);
    }
}

// Admins shouldn't logout when they cancel the "start next tile" / "no tiles"
// / "paused tile" prompts — they should bounce back to the admin panel.
// applies once (role doesn't change mid-session); the click handlers all go
// through exitEditorOrLogout which checks role at click time.
function applySecondaryButtonLabels() {
    const label = currentUser?.role === "admin" ? "Voltar ao painel" : "Sair";
    for (const id of ["idle-logout", "paused-resume-logout", "no-tiles-logout"]) {
        const el = document.getElementById(id);
        if (el) el.textContent = label;
    }
}

function exitEditorOrLogout() {
    if (currentUser?.role === "admin") {
        document.getElementById("btn-go-admin").click();
    } else {
        document.getElementById("btn-logout").click();
    }
}

export async function enterEditor() {
    // display:none while toggled to admin invalidates MapLibre's layout — resize
    // on re-entry. Keep the existing tile so unsaved work isn't refetched away.
    if (currentTile && satMap) {
        requestAnimationFrame(() => satMap && satMap.resize());
        return;
    }
    // Paused tiles need an explicit confirmation before /resume restarts the timer.
    let resume = null;
    try {
        [, , resume] = await Promise.all([
            loadTodayCount(),
            loadQueueStats(),
            apiGet(withProjectParam("/api/tiles/assigned", activeProjectId)).catch(() => null),
        ]);
    } catch {}
    if (resume) {
        if (resume.paused_at) {
            showPausedResumeScreen(resume);
            return;
        }
        await loadTile(resume);
        preloadNext();
        return;
    }
    showIdleScreen("Pronto para começar", "Verificando próximo tile...", { previewNext: true });
}

async function loadTodayCount() {
    try {
        const r = await apiGet(withProjectParam("/api/me/stats-today", activeProjectId));
        todayCount = r.count || 0;
        document.getElementById("today-count").textContent = todayCount;
    } catch {}
}

async function loadQueueStats() {
    try {
        const q = await apiGet(withProjectParam("/api/tiles/queue-stats", activeProjectId));
        const s = queueSummary(q);
        const el = document.getElementById("queue-progress");
        el.title = s.title;
        el.classList.toggle("hidden", s.total === 0);
        document.getElementById("queue-bar-reviewed").style.width = `${s.reviewedPct}%`;
        document.getElementById("queue-bar-classified").style.width = `${s.classifiedOnlyPct}%`;
        document.getElementById("queue-text").textContent = s.text;
    } catch {}
}

function buildColorLut() {
    for (let v = 0; v < 256; v++) {
        colorLut[v*4] = 0; colorLut[v*4+1] = 0; colorLut[v*4+2] = 0; colorLut[v*4+3] = 0;
        darkLut[v*4] = 0; darkLut[v*4+1] = 0; darkLut[v*4+2] = 0; darkLut[v*4+3] = 0;
    }
    for (const c of classes) {
        const [r, g, b] = hexToRgb(c.color);
        colorLut[c.id*4] = r;
        colorLut[c.id*4+1] = g;
        colorLut[c.id*4+2] = b;
        colorLut[c.id*4+3] = 255;
        // darker variant for outlines
        darkLut[c.id*4] = Math.max(0, r - 80);
        darkLut[c.id*4+1] = Math.max(0, g - 80);
        darkLut[c.id*4+2] = Math.max(0, b - 80);
        darkLut[c.id*4+3] = 255;
    }
}

function buildClassPanel() {
    const ul = document.getElementById("class-list");
    ul.replaceChildren();
    classes.forEach((c, i) => {
        const li = document.createElement("li");
        li.dataset.id = c.id;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.title = i < MAX_NUMBER_SHORTCUTS ? `${c.name} (${i + 1})` : c.name;
        const sw = document.createElement("span");
        sw.className = "swatch"; sw.style.backgroundColor = c.color;
        const name = document.createElement("span");
        name.className = "name"; name.textContent = c.name;
        li.append(sw, name);
        if (i < MAX_NUMBER_SHORTCUTS) {
            const key = document.createElement("span");
            key.className = "kbd key"; key.textContent = String(i + 1);
            li.appendChild(key);
        }
        li.addEventListener("click", () => setActiveClass(c.id));
        ul.appendChild(li);
    });
    const count = document.getElementById("class-count");
    if (count) count.textContent = String(classes.length);
    if (classes.length) setActiveClass(classes[0].id);
}

function setActiveClass(id) {
    activeClass = id;
    document.querySelectorAll("#class-list li").forEach(li => {
        const on = Number(li.dataset.id) === id;
        li.classList.toggle("active", on);
        li.setAttribute("aria-selected", String(on));
    });
    // feedback: brief border flash on canvas stack (class color is data)
    const stack = document.getElementById("canvas-stack");
    stack.style.transition = "box-shadow 80ms";
    stack.style.boxShadow = `0 0 0 3px ${classesById[id]?.color || "var(--accent)"} inset`;
    setTimeout(() => { stack.style.boxShadow = ""; }, 120);
}

// --- Loading / saving ---
async function loadNext() {
    clearCanvasFlash();
    // Take the cache and invalidate it up front: whatever /next returns, the
    // old preload is consumed (or discarded) exactly once.
    const cached = preloadedNext;
    clearPreload();
    try {
        let t = await apiGet(withProjectParam("/api/tiles/next", activeProjectId));
        if (!t) {
            if (await _tryFallbackToOtherProject()) return;
            showNoTilesScreen();
            return;
        }
        let maskBytes = null;
        if (t.paused_at) {
            // /next keeps a manual pause intact and hands the paused tile
            // back. "Iniciar tile" is the operator's explicit go-ahead, so
            // resume it the same way the paused-resume prompt does.
            ({ tile: t, maskBytes } = await resumeWithMask(t.id));
        } else if (canReusePreload(cached, t)) {
            // Same tile at the same version: the preloaded mask is current.
            maskBytes = cached.maskBytes;
        }
        await loadTile(t, maskBytes);
        // Fire and forget preload of the next candidate
        preloadNext();
    } catch (e) {
        showToast(`Erro ao carregar: ${e.message}`, "error");
    }
}

// POST /resume and fetch the saved mask in parallel (halves resume latency).
// Classification has no mask (/image → 415). A failed mask fetch is
// non-fatal: /resume already unpaused the tile (a retry would 409), and
// loadTile() fetches the mask itself when none is preloaded.
async function resumeWithMask(tileId) {
    const [tile, maskBlob] = await Promise.all([
        apiPostJson(`/api/tiles/${tileId}/resume`, {}),
        isClassificationProject()
            ? Promise.resolve(null)
            : apiGetBlob(`/api/tiles/${tileId}/image`, { retries: 1 }).catch(() => null),
    ]);
    return { tile, maskBytes: maskBlob ? await maskFromBlob(maskBlob) : null };
}

// Guard against re-entrant fallback: a single "no tiles" doesn't bounce us
// around the portfolio in a tight loop. Reset on every successful load.
let _fallbackInFlight = false;

// When the active project runs out of work, check whether any other project
// the user belongs to still has tiles waiting. Switch + load so the operator
// keeps working without manually picking the next project. Returns true
// when a switch happened (caller skips the "no tiles" screen).
async function _tryFallbackToOtherProject() {
    if (_fallbackInFlight) return false;
    _fallbackInFlight = true;
    try {
        let workload;
        try { workload = await apiGet("/api/me/projects"); } catch { return false; }
        const next = (workload || []).find(p =>
            p.id !== activeProjectId && (p.available || 0) > 0
        );
        if (!next) return false;
        const fromName = window.tileclassActiveProject?.name || "este projeto";
        showToast(`Sem tiles em "${fromName}". Indo para "${next.name}".`,
                  "info", 4000);
        await _switchProject(next.id, { autoLoadNext: true });
        return true;
    } finally {
        _fallbackInFlight = false;
    }
}

async function preloadNext() {
    const token = ++_preloadToken;
    preloadedNext = null;
    // Exclude the open tile: it is assigned to us, so the peek's resume
    // branch would otherwise return it (with its initial mask) as "next".
    let url = withProjectParam("/api/tiles/next-preview", activeProjectId);
    if (currentTile) url += `${url.includes("?") ? "&" : "?"}exclude_tile_id=${currentTile.id}`;
    try {
        const peek = await apiGet(url);
        if (token !== _preloadToken) return;
        if (!peek) return;
        // Classification has no mask body (/image is raster-only → 415).
        if (isClassificationProject()) {
            preloadedNext = { tile: peek, maskBytes: null };
            return;
        }
        const blob = await apiGetBlob(`/api/tiles/${peek.id}/image`, { retries: 2, timeout: 8_000 });
        const maskBytes = await maskFromBlob(blob);
        if (token !== _preloadToken) return;
        preloadedNext = { tile: peek, maskBytes };
    } catch (e) {
        if (token !== _preloadToken) return;
        preloadedNext = null;
        if (e?.timeout) showToast("Não foi possível pré-carregar o próximo tile.", "warn", 4000);
    }
}

function isClassificationProject() {
    return window.tileclassActiveProject?.kind === "classification";
}

// Tear down the classification editor (MapLibre map + listeners) and restore
// the raster canvas-stack. exitClassificationTile() is idempotent, so this is
// safe regardless of the previous kind. Called before loading a tile and
// before switching projects, so a project/kind change never leaks a WebGL
// context or leaves the canvas hidden.
function exitActiveKindEditor() {
    exitClassificationTile();
}

// data-tile="open|none" on #view-editor: CSS quiets tile-bound chrome (review
// callouts, footer actions, stage HUD) while no tile is open.
function setTileOpen(open) {
    document.getElementById("view-editor").dataset.tile = open ? "open" : "none";
}

// Right-panel "Detalhes do tile": status chip + center / footprint facts.
function renderTileDetails(t) {
    const facts = tileFacts(t, window.tileclassActiveProject);
    const chip = document.getElementById("tile-status-chip");
    if (chip) {
        chip.className = t ? `chip ${t.status}` : "chip hidden";
        chip.textContent = t ? statusLabel(t.status) : "";
    }
    for (const k of ["lat", "lon", "footprint", "resolution"]) {
        const el = document.getElementById(`tile-fact-${k}`);
        if (el) el.textContent = facts ? facts[k] : "—";
    }
}

// Header tile identity: "#id" + name (mono). Cleared with null.
function renderTileLabel(t) {
    const el = document.getElementById("tile-name-label");
    el.replaceChildren();
    if (!t) { el.removeAttribute("title"); return; }
    const id = document.createElement("span");
    id.className = "tile-id";
    id.textContent = `#${t.id}`;
    const name = document.createElement("span");
    name.className = "tile-name";
    name.textContent = truncateName(t.name);
    el.append(id, name);
    el.title = `Tile ${t.name} (#${t.id})`;
}

async function loadTile(t, preloadedMask = null) {
    _tileReady = false;
    exitActiveKindEditor();
    currentTile = t;
    undoStack.length = 0; redoStack.length = 0;
    updateUndoRedoButtons();
    hideNoTilesScreen();
    hideIdleScreen();
    hidePausedResumeScreen();
    renderTileLabel(t);
    renderTileDetails(t);
    setTileOpen(true);
    // Browser-tab title carries the tile id so the operator can find the right
    // tab when they have several open (review queue, multiple projects).
    document.title = `Tile #${t.id} — TileClass`;
    const reviewBanner = document.getElementById("review-banner");
    const modePill = document.getElementById("mode-pill");
    if (t.status === "in_review") {
        document.getElementById("review-banner-text").textContent =
            `Classificado por ${t.classified_by_username || "?"}`;
        reviewBanner.classList.remove("hidden");
        if (modePill) {
            modePill.textContent = "REVISAR";
            modePill.classList.remove("mode-classify");
            modePill.classList.add("mode-review");
        }
    } else {
        reviewBanner.classList.add("hidden");
        if (modePill) {
            modePill.textContent = "CLASSIFICAR";
            modePill.classList.remove("mode-review");
            modePill.classList.add("mode-classify");
        }
    }
    if (isClassificationProject()) {
        // Classification: MapLibre satellite + class-picker sidebar; no canvas.
        await enterClassificationTile(t, window.tileclassActiveProject);
        refreshRequestChangesButton(t);
        loadReviewNoteBanner(t.id);
        startHeartbeat(t.id);
        refreshSubmitState();
        _tileReady = true;
        return;
    }
    if (preloadedMask) mask = preloadedMask;
    else await loadMaskFromServer(t.id);
    // Server-reported filled count is the source of truth; client recounts
    // locally after each paint, but this reconciles anything that might drift
    // (e.g. historical mask saved with class=0).
    if (typeof t.filled_pixels === "number") {
        filledCount = t.filled_pixels;
    } else {
        recountFilled();
    }
    tryRestoreBackup();
    renderMaskFull();
    renderSatellite(t);
    resetView();
    drawGrid();
    updateProgress();
    refreshRequestChangesButton(t);
    loadReviewNoteBanner(t.id);
    startHeartbeat(t.id);
    _tileReady = true;
}


// ---- Heartbeat: keeps the auto-pause sweep aware that the tile is alive.
// One ping right on load, then every 60s (well below the server's 5min
// timeout), plus one when the page becomes visible again (laptop wake).
// A failed ping is ignored — the next one retries. If the server answers
// `not_active` (it auto-paused the tile while the machine slept, or an admin
// paused it) the tile is still ours: /resume it and adopt the new version,
// otherwise every later submit would fail with 409 tile_modified. The local
// mask is kept as is.
let _heartbeatTimer = null;

function startHeartbeat(tileId) {
    stopHeartbeat();
    sendHeartbeat(tileId);
    _heartbeatTimer = setInterval(() => {
        if (!currentTile || currentTile.id !== tileId) {
            stopHeartbeat();
            return;
        }
        sendHeartbeat(tileId);
    }, 60_000);
}

let _heartbeatInFlight = null;
function sendHeartbeat(tileId) {
    if (_heartbeatInFlight) return _heartbeatInFlight;
    _heartbeatInFlight = _heartbeatOnce(tileId).finally(() => { _heartbeatInFlight = null; });
    return _heartbeatInFlight;
}

async function _heartbeatOnce(tileId) {
    const stillOpen = () => currentTile && currentTile.id === tileId;
    if (!stillOpen()) return;
    let resp;
    try { resp = await apiPostJson(`/api/tiles/${tileId}/heartbeat`, {}); }
    catch { return; }
    if (!stillOpen() || !heartbeatNeedsResume(resp)) return;
    let refreshed;
    try { refreshed = await apiPostJson(`/api/tiles/${tileId}/resume`, {}); }
    catch { return; }  // no longer ours / not paused: the submit path reports it
    if (!stillOpen() || !refreshed || refreshed.id !== tileId) return;
    // Keep client-only fields (e.g. classified_by_username), take the
    // server's status/version/paused_at.
    currentTile = { ...currentTile, ...refreshed };
    saveBackup();  // re-stamp the backup with the new version
    showToast("Tile retomado após inatividade.", "info", 4000);
}

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && currentTile && _heartbeatTimer) {
        sendHeartbeat(currentTile.id);
    }
});

function stopHeartbeat() {
    if (_heartbeatTimer) {
        clearInterval(_heartbeatTimer);
        _heartbeatTimer = null;
    }
}

function refreshRequestChangesButton(t) {
    const btn = document.getElementById("btn-request-changes");
    if (!btn) return;
    btn.classList.toggle("hidden", t?.status !== "in_review");
}

async function loadReviewNoteBanner(tileId) {
    const banner = document.getElementById("review-note-banner");
    if (!banner) return;
    try {
        const note = await apiGet(`/api/tiles/${tileId}/review-note`);
        if (!note) {
            banner.classList.add("hidden");
            return;
        }
        document.getElementById("review-note-text").textContent = note.note;
        document.getElementById("review-note-meta").textContent =
            `${note.by_username || "?"} · ${(note.created_at || "").slice(0, 16).replace("T", " ")}`;
        banner.classList.remove("hidden");
    } catch {
        banner.classList.add("hidden");
    }
}

// Decode a single-band PNG mask blob into a TILE² Uint8Array (red channel).
async function maskFromBlob(blob) {
    const img = await blobToImage(blob);
    const tmp = document.createElement("canvas");
    tmp.width = TILE; tmp.height = TILE;
    const tctx = tmp.getContext("2d");
    tctx.drawImage(img, 0, 0, TILE, TILE);
    const data = tctx.getImageData(0, 0, TILE, TILE).data;
    const m = new Uint8Array(PIXELS);
    for (let i = 0; i < PIXELS; i++) m[i] = data[i * 4];
    return m;
}

async function loadMaskFromServer(tileId) {
    mask = await maskFromBlob(await apiGetBlob(`/api/tiles/${tileId}/image`));
}

function tryRestoreBackup() {
    // Auto-restore: if localStorage holds a different mask for the tile we
    // just loaded, overwrite in-memory mask silently. Backups only exist when
    // the user had unsynced work (F5, browser crash); the canonical save+clear
    // path makes server == backup any other time. Caller re-renders.
    const restored = bkLoad(backupId(), PIXELS);
    if (!restored) return;
    // Collect diffs once: lets us skip a no-op restore AND seed an undo entry
    // that reverts to the server state (Ctrl+Z right after a restore).
    const touched = new Map();
    for (let i = 0; i < PIXELS; i++) {
        if (restored[i] !== mask[i]) touched.set(i, mask[i]);
    }
    if (touched.size === 0) return;
    mask = restored;
    recountFilled();
    pushUndo(touched);
    showToast("Trabalho local restaurado.", "success", 2500);
}

// Backups are keyed by (user, tile) and stamped with the tile version, so a
// backup from another user or an earlier assignment cycle never resurfaces.
function backupId(tile = currentTile) {
    return { userId: currentUser?.id ?? null, tileId: tile?.id, version: tile?.version ?? null };
}

function saveBackup() {
    if (!currentTile) return;
    bkSave(backupId(), mask);
}

function clearBackup(tile = currentTile) {
    if (tile) bkClear(backupId(tile));
}

function recountFilled() {
    let c = 0;
    for (let i = 0; i < PIXELS; i++) if (mask[i] !== 255) c++;
    filledCount = c;
    updateProgress();
}

const _pctFmt = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
function updateProgress() {
    document.getElementById("pixel-count").textContent = filledCount.toLocaleString("pt-BR");
    const total = document.getElementById("pixel-total");
    if (total) total.textContent = PIXELS.toLocaleString("pt-BR");
    const pct = 100 * filledCount / PIXELS;
    // Never round a nearly-complete mask up to "100".
    const shown = filledCount < PIXELS ? Math.min(pct, 99.9) : 100;
    document.getElementById("pixel-pct").textContent = _pctFmt.format(shown);
    const line = document.getElementById("progress-line");
    line.classList.toggle("complete", filledCount === PIXELS);
    const bar = document.getElementById("progress-bar-fill");
    if (bar) bar.style.width = pct.toFixed(2) + "%";
    const missing = PIXELS - filledCount;
    const missLine = document.getElementById("missing-line");
    const missCount = document.getElementById("missing-count");
    if (missLine && missCount) {
        // The "missing pixels" warning only matters when the project requires
        // a fully-painted mask. Loose projects keep it hidden — the badge
        // would otherwise nag the operator about a non-condition.
        const showMissing = maskCompleteRequired && missing > 0;
        missLine.classList.toggle("hidden", !showMissing);
        missCount.textContent = missing.toLocaleString("pt-BR");
    }
    updateSubmitButton(missing);
}

// Single point that paints the footer submit button's state. Both the raster
// path (driven by pixel counts) and the non-raster path (driven by validator
// errors) end here so UX tweaks (label tone, classnames) only need editing
// once.
function _paintSubmit({ incomplete, label, title, mode }) {
    const btn = document.getElementById("btn-submit");
    const labelEl = document.getElementById("submit-label");
    if (!btn || !labelEl) return;
    btn.classList.toggle("incomplete", incomplete);
    btn.classList.toggle("is-review", mode === "review");
    labelEl.textContent = label;
    btn.title = title;
}

function _baseSubmitLabel() {
    return currentTile && currentTile.status === "in_review" ? "Aprovar revisão" : "Submeter";
}

function updateSubmitButton(missing) {
    _paintSubmit(rasterSubmitState({
        missing, maskCompleteRequired,
        isReview: currentTile?.status === "in_review",
    }));
}

// Classification drives their own state and don't go through updateProgress.
// They call window.tcRefreshSubmit?.() after each mutation so the footer's
// submit button mirrors the validity of the current state.
function refreshSubmitState() {
    if (!currentTile) return;
    let issues = [];
    try {
        if (isClassificationProject()) issues = validateClassification();
        else return;  // raster goes through updateSubmitButton
    } catch { issues = []; }
    const base = _baseSubmitLabel();
    if (issues.length) {
        const first = issues[0];
        _paintSubmit({
            incomplete: true, mode: "incomplete",
            label: first.length > 32 ? first.slice(0, 30) + "…" : first,
            title: issues.join("\n"),
        });
    } else {
        _paintSubmit({
            incomplete: false, label: base, title: `${base} (Ctrl+S)`,
            mode: base === "Aprovar revisão" ? "review" : "submit",
        });
    }
}

// --- Rendering ---

// Reference-mask overlays (ref_primary / ref_secondary) hide the user mask
// while held so the operator sees the reference clean. Imagery overlays
// (secondary/tertiary) keep the mask visible because they are alternative
// satellite views, not a comparison layer.
const HIDE_MASK_OVERLAYS = new Set(["ref_primary", "ref_secondary"]);

function setOverlayHold(key, on) {
    if (!satMap) return;
    setOverlayVisible(satMap, key, on);
    if (HIDE_MASK_OVERLAYS.has(key)) {
        document.getElementById("canvas-viewport").classList.toggle(`${key}-held`, on);
    }
}

function pressOverlay(key) {
    if (!overlayCfg[key] || overlayHeld[key]) return;
    overlayHeld[key] = true;
    setOverlayHold(key, true);
}

function releaseOverlay(key) {
    if (!overlayHeld[key]) return;
    overlayHeld[key] = false;
    setOverlayHold(key, false);
}

let _mapErrorCount = 0;
function renderSatellite(t) {
    const tileBbox = [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north];
    const bbox = expandBbox(tileBbox, CONTEXT_FACTOR);
    const warnEl = document.getElementById("map-warning");
    if (warnEl) warnEl.classList.add("hidden");
    _mapErrorCount = 0;
    if (satMap && satMapKey !== currentMapKey()) {
        satMap = disposeMap(satMap);
    }
    if (!satMap) {
        satMap = createLockedMap("map-satellite", tileserverUrl, bbox, tileserverMaxZoom, overlayCfg);
        satMapKey = currentMapKey();
        satMap.on("error", (e) => {
            // Multiple consecutive tile errors → surface a banner so the
            // operator knows the satellite background is missing (pure
            // black/blank is otherwise indistinguishable from water).
            if (e && e.error) {
                _mapErrorCount++;
                if (_mapErrorCount >= 2 && warnEl) warnEl.classList.remove("hidden");
            }
        });
    } else {
        setMapBbox(satMap, bbox);
    }
    requestAnimationFrame(() => satMap && satMap.resize());
}

function renderMaskFull() {
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
}

function writeMaskPixels(x0, y0, x1, y1) {
    for (let y = y0; y < y1; y++) {
        const row = y * TILE;
        for (let x = x0; x < x1; x++) {
            const i = row + x;
            maskData32[i] = colorLut32[mask[i]];
        }
    }
}

function blitMask() {
    offCtx.putImageData(maskImageData, 0, 0);
    ctxMask.clearRect(0, 0, DISPLAY, DISPLAY);
    if (maskHidden) return;
    if (outlineOnly) {
        drawOutlines(false);
    } else {
        ctxMask.globalAlpha = maskOpacity;
        ctxMask.imageSmoothingEnabled = false;
        ctxMask.drawImage(offCanvas, 0, 0, DISPLAY, DISPLAY);
        ctxMask.globalAlpha = 1;
    }
    if (missingHighlight) drawMissingOverlay();
}

function drawMissingOverlay() {
    ctxMask.fillStyle = palette.missing;
    ctxMask.globalAlpha = 0.85;
    for (let y = 0; y < TILE; y++) {
        const row = y * TILE;
        for (let x = 0; x < TILE; x++) {
            if (mask[row + x] === 255) ctxMask.fillRect(x*SCALE, y*SCALE, SCALE, SCALE);
        }
    }
    ctxMask.globalAlpha = 1;
}

function drawOutlines(overlay) {
    // overlay=true: draw on top of filled mask with darker color
    // overlay=false: fill-mode is off; draw outlines in class color
    const lut = overlay ? darkLut : colorLut;
    ctxMask.globalAlpha = overlay ? Math.min(1, maskOpacity + 0.3) : 1;
    for (let y = 0; y < TILE; y++) {
        for (let x = 0; x < TILE; x++) {
            const v = mask[y*TILE + x];
            if (v === 255) continue;
            const right = x + 1 < TILE ? mask[y*TILE + x+1] : 255;
            const bottom = y + 1 < TILE ? mask[(y+1)*TILE + x] : 255;
            if (right !== v) {
                ctxMask.fillStyle = `rgb(${lut[v*4]},${lut[v*4+1]},${lut[v*4+2]})`;
                ctxMask.fillRect((x+1)*SCALE - 1, y*SCALE, 1, SCALE);
            }
            if (bottom !== v) {
                ctxMask.fillStyle = `rgb(${lut[v*4]},${lut[v*4+1]},${lut[v*4+2]})`;
                ctxMask.fillRect(x*SCALE, (y+1)*SCALE - 1, SCALE, 1);
            }
        }
    }
    ctxMask.globalAlpha = 1;
}

// --- Painting ---

function screenToLogical(ev) {
    return coreScreenToLogical(canvasCursor.getBoundingClientRect(), ev.clientX, ev.clientY, TILE);
}

function brushRadius() { return Math.floor(BRUSH_SIZES[brushSizeIdx] / 2); }

function paintAt(cx, cy) {
    const r = brushRadius();
    const value = tool === "eraser" ? 255 : activeClass;
    const res = corePaintAt(mask, cx, cy, value, r, gestureTouched, TILE);
    filledCount += res.deltaFilled;
    const [x0, y0, x1, y1] = res.region;
    writeMaskPixels(x0, y0, x1, y1);
    blitMask();
}

function paintLine(x0, y0, x1, y1) {
    const value = tool === "eraser" ? 255 : activeClass;
    const res = corePaintLine(mask, x0, y0, x1, y1, value, brushRadius(), TILE);
    filledCount += res.deltaFilled;
    // Merge touched into the gesture map
    for (const [i, v] of res.touched) if (!gestureTouched.has(i)) gestureTouched.set(i, v);
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
}

function floodFill(cx, cy) {
    const replacement = activeClass;
    const res = coreFloodFill(mask, cx, cy, replacement, TILE);
    if (res.positions.length === 0) return;
    filledCount += res.deltaFilled;
    pushUndoEntry({ positions: res.positions, prevValues: res.prevValues });
    renderMaskFull();
    updateProgress();
    saveBackup();
}

function pushUndo(touchedMap) {
    pushUndoEntry(toUndoEntry(touchedMap));
}

function pushUndoEntry(entry) {
    undoStack.push(entry);
    if (undoStack.length > MAX_UNDO) undoStack.shift();
    redoStack.length = 0;
    updateUndoRedoButtons();
}

function updateUndoRedoButtons() {
    const u = document.getElementById("btn-undo");
    const r = document.getElementById("btn-redo");
    if (u) u.disabled = undoStack.length === 0;
    if (r) r.disabled = redoStack.length === 0;
}

function applyPatch(entry) {
    const res = coreApplyPatch(mask, entry);
    filledCount += res.deltaFilled;
    return res.inverse;
}

function undo() {
    if (!undoStack.length) return;
    const entry = undoStack.pop();
    redoStack.push(applyPatch(entry));
    renderMaskFull();
    updateProgress();
    saveBackup();
    updateUndoRedoButtons();
}

function redo() {
    if (!redoStack.length) return;
    const entry = redoStack.pop();
    undoStack.push(applyPatch(entry));
    renderMaskFull();
    updateProgress();
    saveBackup();
    updateUndoRedoButtons();
}

// --- Events ---

function attachEvents() {
    canvasCursor.addEventListener("mousedown", onMouseDown);
    canvasCursor.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    canvasCursor.addEventListener("mouseleave", () => {
        ctxCursor.clearRect(0, 0, DISPLAY, DISPLAY);
        clearHoverClass();
    });
    const viewport = document.getElementById("canvas-viewport");
    viewport.addEventListener("wheel", onWheel, { passive: false });
    viewport.addEventListener("mousedown", onViewportMouseDown);
    window.addEventListener("mousemove", onViewportMouseMove);
    window.addEventListener("mouseup", onViewportMouseUp);
    viewport.addEventListener("contextmenu", (e) => e.preventDefault());

    document.getElementById("btn-zoom-in").addEventListener("click", () => zoomBy(1.5));
    document.getElementById("btn-zoom-out").addEventListener("click", () => zoomBy(1 / 1.5));
    document.getElementById("btn-zoom-reset").addEventListener("click", resetView);

    const ntRefresh = document.getElementById("no-tiles-refresh");
    if (ntRefresh) ntRefresh.addEventListener("click", loadNext);
    const ntLogout = document.getElementById("no-tiles-logout");
    if (ntLogout) ntLogout.addEventListener("click", exitEditorOrLogout);

    document.getElementById("tool-brush").addEventListener("click", () => setTool("brush"));
    document.getElementById("tool-eraser").addEventListener("click", () => setTool("eraser"));
    document.getElementById("tool-fill").addEventListener("click", () => setTool("fill"));

    const bs = document.getElementById("brush-size");
    bs.min = 0; bs.max = String(BRUSH_SIZES.length - 1); bs.value = String(brushSizeIdx);
    bs.addEventListener("input", () => {
        brushSizeIdx = Number(bs.value);
        document.getElementById("brush-size-label").textContent = BRUSH_SIZES[brushSizeIdx];
        syncRangeFill(bs);
    });
    document.getElementById("brush-size-label").textContent = BRUSH_SIZES[brushSizeIdx];
    syncRangeFill(bs);

    const op = document.getElementById("mask-opacity");
    op.addEventListener("input", () => {
        maskOpacity = Number(op.value) / 100;
        document.getElementById("opacity-label").textContent = `${op.value}%`;
        syncRangeFill(op);
        blitMask();
    });
    syncRangeFill(op);

    document.getElementById("btn-undo").addEventListener("click", undo);
    document.getElementById("btn-redo").addEventListener("click", redo);
    document.getElementById("btn-submit").addEventListener("click", submit);
    document.getElementById("btn-problem").addEventListener("click", openProblemModal);
    const btnRC = document.getElementById("btn-request-changes");
    if (btnRC) btnRC.addEventListener("click", openRequestChangesModal);
    const btnPause = document.getElementById("btn-pause");
    if (btnPause) btnPause.addEventListener("click", pauseTile);
    document.getElementById("btn-next-missing")?.addEventListener("click", jumpToNextMissing);
    document.getElementById("btn-shortcuts")?.addEventListener("click", openShortcutsModal);
    document.getElementById("shortcuts-close")?.addEventListener("click", closeShortcutsModal);
    // Clicking the dimmed backdrop of an editor modal cancels it.
    for (const id of ["modal-problem", "modal-request-changes", "modal-shortcuts"]) {
        const m = document.getElementById(id);
        m?.addEventListener("mousedown", (ev) => {
            if (ev.target === m) modalCancelControl(m)?.click();
        });
    }
    const btnGmaps = document.getElementById("btn-gmaps");
    if (btnGmaps) btnGmaps.addEventListener("click", openInGoogleMaps);
    const btnGearth = document.getElementById("btn-gearth");
    if (btnGearth) btnGearth.addEventListener("click", openInGoogleEarth);

    const resumeContinue = document.getElementById("paused-resume-continue");
    if (resumeContinue) resumeContinue.addEventListener("click", continuePausedTile);
    const resumeLogout = document.getElementById("paused-resume-logout");
    if (resumeLogout) resumeLogout.addEventListener("click", exitEditorOrLogout);

    document.getElementById("idle-start").addEventListener("click", () => {
        hideIdleScreen();
        loadNext();
    });
    document.getElementById("idle-logout").addEventListener("click", exitEditorOrLogout);

    document.getElementById("problem-cancel").addEventListener("click", closeProblemModal);
    document.getElementById("problem-confirm").addEventListener("click", confirmProblem);
    const rcCancel = document.getElementById("request-changes-cancel");
    if (rcCancel) rcCancel.addEventListener("click", closeRequestChangesModal);
    const rcConfirm = document.getElementById("request-changes-confirm");
    if (rcConfirm) rcConfirm.addEventListener("click", confirmRequestChanges);
    const rcNote = document.getElementById("request-changes-note");
    if (rcNote) rcNote.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) confirmRequestChanges();
    });


    document.getElementById("btn-logout").addEventListener("click", async () => {
        await apiLogout();
        location.reload();
    });
    const btnSwitch = document.getElementById("btn-switch-project");
    if (btnSwitch) btnSwitch.addEventListener("click", openSwitchProjectModal);

    const note = document.getElementById("problem-note");
    const noteCount = document.getElementById("problem-note-count");
    if (note) {
        const updateCount = () => {
            if (noteCount) noteCount.textContent = `${note.value.length}/2000`;
        };
        note.addEventListener("input", updateCount);
        note.addEventListener("keydown", (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); confirmProblem(); }
        });
        updateCount();
    }

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    // Losing focus mid-chord (e.g. Alt+Tab during Ctrl+Z) drops the keyup,
    // leaving zSuppressed stuck. Reset on blur so the next session is clean.
    // Same for Space: its keyup never arrives, so undo everything the
    // keydown did (hidden mask + grab cursor), not just the flag.
    window.addEventListener("blur", () => {
        zSuppressed = false;
        releaseSpace();
        for (const key of Object.keys(overlayHeld)) releaseOverlay(key);
    });
}

function isTextFocused() {
    const el = document.activeElement;
    return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA");
}
// Any visible .modal (problem, request-changes, project switcher shell,
// action confirm…) blocks editor shortcuts.
function isModalOpen() {
    return isAnyModalOpen(document);
}

// Escape dismisses the topmost modal through its own cancel control so
// promise-based modals (openModal/confirmAction) resolve and clean up.
function closeTopmostModal() {
    const modal = topmostOpenModal(document);
    if (!modal) return false;
    const cancel = modalCancelControl(modal);
    if (cancel) cancel.click();
    else modal.classList.add("hidden");
    return true;
}

function confirmAction({
    title = "Confirmar", message, okLabel = "Confirmar", cancelLabel = "Cancelar",
    iconName = "send", tone = "accent",
}) {
    const modal = document.getElementById("modal-action-confirm");
    const iconWrap = document.getElementById("action-confirm-icon");
    iconWrap.className = `modal-icon tone-${tone}`;
    document.getElementById("action-confirm-icon-use").setAttribute("href", `${ICON_SPRITE}#i-${iconName}`);
    document.getElementById("action-confirm-title").textContent = title;
    document.getElementById("action-confirm-message").textContent = message;
    const okBtn = document.getElementById("action-confirm-ok");
    const cancelBtn = document.getElementById("action-confirm-cancel");
    okBtn.textContent = okLabel;
    cancelBtn.textContent = cancelLabel;
    modal.classList.remove("hidden");
    okBtn.focus();
    return new Promise((resolve) => {
        const finish = (value) => {
            modal.classList.add("hidden");
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            window.removeEventListener("keydown", onKey, true);
            resolve(value);
        };
        const onOk = () => finish(true);
        const onCancel = () => finish(false);
        const onKey = (ev) => {
            if (ev.key === "Escape") { ev.stopPropagation(); finish(false); }
            else if (ev.key === "Enter") { ev.stopPropagation(); finish(true); }
        };
        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
        window.addEventListener("keydown", onKey, true);
    });
}

function onMouseDown(ev) {
    if (!currentTile) return;
    if (ev.button === 2) return; // right-click is pan — handled on viewport
    const [x, y] = screenToLogical(ev);
    if (tool === "fill") { floodFill(x, y); return; }
    drawing = true;
    gestureTouched = new Map();
    lastPx = [x, y];
    paintAt(x, y);
    drawCursor(x, y);
}

function onMouseMove(ev) {
    if (!currentTile) return;
    const [x, y] = screenToLogical(ev);
    drawCursor(x, y);
    updateHoverClass(x, y);
    if (drawing) {
        if (lastPx) paintLine(lastPx[0], lastPx[1], x, y);
        else paintAt(x, y);
        lastPx = [x, y];
    }
}

function updateHoverClass(x, y) {
    const v = mask[y * TILE + x];
    const el = document.getElementById("hover-class");
    const sw = document.getElementById("hover-swatch");
    const cls = classesById[v];
    if (v === 255) el.textContent = "(não preenchido)";
    else el.textContent = cls?.name || `classe ${v}`;
    if (sw) {
        sw.classList.toggle("hidden", v === 255);
        sw.style.backgroundColor = cls?.color || "";
    }
}

function clearHoverClass() {
    document.getElementById("hover-class").textContent = "—";
    document.getElementById("hover-swatch")?.classList.add("hidden");
}

function onMouseUp() {
    if (!drawing) return;
    drawing = false;
    if (gestureTouched && gestureTouched.size > 0) {
        pushUndo(gestureTouched);
        saveBackup();
    }
    gestureTouched = null;
    lastPx = null;
    updateProgress();
    blitMask(); // final blit with outline overlay now that gesture ended
}

function onWheel(ev) {
    ev.preventDefault();
    if (ev.ctrlKey) {
        // Ctrl+wheel adjusts mask opacity (preserved from previous UX).
        const delta = ev.deltaY < 0 ? 0.05 : -0.05;
        adjustOpacity(delta);
        return;
    }
    // Plain wheel → zoom, centered on the cursor position.
    const factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
    zoomAt(factor, ev.clientX, ev.clientY);
}

function onViewportMouseDown(ev) {
    // Pan only via right mouse button (any tool). Space is reserved for
    // hiding the mask while held, per UX spec.
    if (ev.button !== 2) return;
    ev.preventDefault();
    panning = true;
    panStart = { clientX: ev.clientX, clientY: ev.clientY, panX, panY };
    document.getElementById("canvas-viewport").classList.add("panning");
    document.getElementById("canvas-stack").classList.add("panning");
}
function onViewportMouseMove(ev) {
    if (!panning || !panStart) return;
    panX = panStart.panX + (ev.clientX - panStart.clientX);
    panY = panStart.panY + (ev.clientY - panStart.clientY);
    applyTransform();
}
function onViewportMouseUp() {
    if (!panning) return;
    panning = false;
    panStart = null;
    document.getElementById("canvas-viewport").classList.remove("panning");
    document.getElementById("canvas-stack").classList.remove("panning");
}

function applyTransform() {
    const stack = document.getElementById("canvas-stack");
    stack.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    const label = document.getElementById("zoom-label");
    if (label) label.textContent = `${Math.round(zoom * 100)}%`;
}

function zoomBy(factor) {
    const viewport = document.getElementById("canvas-viewport");
    const r = viewport.getBoundingClientRect();
    zoomAt(factor, r.left + r.width / 2, r.top + r.height / 2);
}

function zoomAt(factor, clientX, clientY) {
    const prev = zoom;
    const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom * factor));
    if (next === prev) return;
    // Keep the point under the cursor stationary: compute current offset of
    // that client point relative to the stack center, then scale it.
    const stack = document.getElementById("canvas-stack");
    const r = stack.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const ratio = next / prev;
    // Keep (clientX, clientY) fixed: pan' = pan + (client - r.center) * (1 - ratio).
    panX = panX + (clientX - cx) * (1 - ratio);
    panY = panY + (clientY - cy) * (1 - ratio);
    zoom = next;
    applyTransform();
    drawGrid();
}

// Center the viewport on a specific logical pixel (px, py) at targetZoom,
// recomputing pan so that pixel lands at viewport center. Used by "jump to
// next missing pixel" so the operator doesn't have to hunt for stragglers.
function focusPixel(px, py, targetZoom) {
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, targetZoom));
    const localX = (px + 0.5) * SCALE;
    const localY = (py + 0.5) * SCALE;
    // At pan=0 the stack is centered in its viewport; offset from stack center
    // is (local - DISPLAY/2). Multiplied by zoom gives the on-screen offset;
    // negate to pull that point to center.
    panX = -(localX - DISPLAY / 2) * zoom;
    panY = -(localY - DISPLAY / 2) * zoom;
    applyTransform();
    drawGrid();
}

function jumpToNextMissing() {
    if (filledCount === PIXELS) {
        showToast("Máscara completa.", "success");
        return;
    }
    let i = nextMissingCursor % PIXELS;
    for (let k = 0; k < PIXELS; k++) {
        if (mask[i] === 255) break;
        i = (i + 1) % PIXELS;
    }
    nextMissingCursor = (i + 1) % PIXELS;
    const px = i % TILE, py = (i / TILE) | 0;
    focusPixel(px, py, Math.max(zoom, 8));
    if (!missingHighlight) {
        missingHighlight = true;
        blitMask();
    }
}

// Zoom applied by the last resetView(); the resize handler refits only while
// the operator is still on that fitted view.
let _fittedZoom = 1;
function resetView() {
    const vp = document.getElementById("canvas-viewport");
    // padY leaves the bottom HUD / zoom cluster (12 px inset + 38 px) off the
    // paint area; the paint area stays centered, so the top gets the same room.
    _fittedZoom = fitZoom(vp.clientWidth, vp.clientHeight, DISPLAY, { padX: 16, padY: 56 });
    zoom = _fittedZoom; panX = 0; panY = 0;
    applyTransform();
    drawGrid();
}

// --- 256×256 grid overlay ---
// The grid canvas lives inside the transformed stack at DISPLAY resolution
// (768×768). Lines are drawn with width scaled by 1/zoom so they remain 1 CSS
// pixel thick on screen regardless of zoom level.
function drawGrid() {
    ctxGrid.clearRect(0, 0, DISPLAY, DISPLAY);
    // offsetWidth is the untransformed CSS size; zoom is applied on top.
    const g = gridGeometry({
        tilePx: TILE, metersPerPixel: METERS_PER_PIXEL, display: DISPLAY,
        cssSize: canvasGrid.offsetWidth || DISPLAY, zoom,
    });
    // Below ~3 CSS px per mask pixel the lines would just cover the imagery.
    if (!g.visible) return;
    ctxGrid.save();
    ctxGrid.lineWidth = g.lineWidth;
    ctxGrid.strokeStyle = palette.ink;
    ctxGrid.globalAlpha = 0.6;
    ctxGrid.beginPath();
    for (let i = 0; i <= TILE; i++) {
        const p = i * g.cell;
        ctxGrid.moveTo(p, 0); ctxGrid.lineTo(p, DISPLAY);
        ctxGrid.moveTo(0, p); ctxGrid.lineTo(DISPLAY, p);
    }
    ctxGrid.stroke();
    ctxGrid.restore();
}

function drawCursor(x, y) {
    ctxCursor.clearRect(0, 0, DISPLAY, DISPLAY);
    const r = brushRadius();
    const sx = (x - r) * SCALE;
    const sy = (y - r) * SCALE;
    const sz = (2 * r + 1) * SCALE;
    // Stroke scaled by 1/zoom so the cursor box stays exactly 1 CSS pixel
    // thick on screen regardless of zoom — otherwise at high zoom a 1.5px
    // line bleeds into adjacent grid cells and hides which pixel will paint.
    const lw = 1 / zoom;
    const inset = lw / 2;
    // Cursor shows the active class color sandwiched between dark/light strokes
    // so it stays visible over any background AND tells the operator at a
    // glance which class is about to paint. Eraser falls back to the original
    // black/white to signal "no class".
    const cls = tool === "eraser" ? null : classesById[activeClass];
    const cssRgb = cls ? `rgb(${colorLut[cls.id*4]},${colorLut[cls.id*4+1]},${colorLut[cls.id*4+2]})` : null;
    ctxCursor.globalAlpha = 0.85;
    ctxCursor.strokeStyle = palette.ink;
    ctxCursor.lineWidth = lw;
    ctxCursor.strokeRect(sx + inset, sy + inset, sz - lw, sz - lw);
    if (cssRgb) {
        ctxCursor.globalAlpha = 1;
        ctxCursor.strokeStyle = cssRgb;
        ctxCursor.lineWidth = lw;
        ctxCursor.strokeRect(sx - inset, sy - inset, sz + lw, sz + lw);
        // Outer light halo keeps the class color readable over light imagery.
        ctxCursor.globalAlpha = 0.85;
        ctxCursor.strokeStyle = palette.halo;
        ctxCursor.lineWidth = lw;
        ctxCursor.strokeRect(sx - inset - lw, sy - inset - lw, sz + 3*lw, sz + 3*lw);
    } else {
        ctxCursor.globalAlpha = 0.95;
        ctxCursor.strokeStyle = palette.halo;
        ctxCursor.lineWidth = lw;
        ctxCursor.strokeRect(sx - inset, sy - inset, sz + lw, sz + lw);
    }
    ctxCursor.globalAlpha = 1;
}

function setTool(t) {
    tool = t;
    for (const id of ["tool-brush","tool-eraser","tool-fill"]) {
        document.getElementById(id).classList.toggle("active", id === `tool-${t}`);
    }
}

function onKeyDown(ev) {
    // Listeners stay attached when admin toggles to the panel — gate so editor
    // shortcuts (e.g. Z/X opacity) don't fire against a hidden canvas.
    if (document.getElementById("view-editor").classList.contains("hidden")) return;
    const sc = resolveShortcut(ev);
    // Ctrl+S never opens the browser's "save page" dialog inside the editor.
    if (sc?.action === "submit") ev.preventDefault();
    // Ctrl+P opens the project switcher regardless of focus / kind — single
    // shortcut for all project types. Skip when a modal is already open so
    // it doesn't stack dialogs.
    if (sc?.action === "switch-project" && !isModalOpen()) {
        const btn = document.getElementById("btn-switch-project");
        if (btn && !btn.disabled) {
            ev.preventDefault();
            btn.click();
            return;
        }
    }
    // "?" toggles the shortcut reference (any kind, never over another modal).
    if (sc?.action === "help" && !isTextFocused()) {
        const open = !document.getElementById("modal-shortcuts").classList.contains("hidden");
        if (open) { ev.preventDefault(); closeShortcutsModal(); return; }
        if (!isModalOpen()) { ev.preventDefault(); openShortcutsModal(); return; }
    }
    if (isModalOpen()) {
        if (ev.key === "Escape") {
            ev.preventDefault();
            closeTopmostModal();
        }
        return;
    }
    if (isTextFocused()) return;
    if (sc?.action === "submit") {
        if (!ev.repeat) submit();
        return;
    }
    // Raster shortcuts (1–6, Q/W/E/A/S, Z/X, Space, Ctrl+Z/Y…) only apply to the
    // canvas editor. Classification has its own (digits 1–9 pick a class);
    // running the raster ones too would fire against a hidden canvas. Chords
    // (Ctrl/Meta/Alt) are never letter/digit shortcuts in either editor.
    if (isClassificationProject()) {
        if (!ev.ctrlKey && !ev.metaKey && !ev.altKey) classificationKeyDown(ev);
        return;
    }
    if (!sc) return;
    switch (sc.action) {
        case "undo":
            ev.preventDefault();
            zSuppressed = true;
            if (!ev.repeat) undo();
            return;
        case "redo":
            ev.preventDefault();
            if (ev.key === "z" || ev.key === "Z") zSuppressed = true;
            if (!ev.repeat) redo();
            return;
        case "class":
            if (classes[sc.arg]) setActiveClass(classes[sc.arg].id);
            return;
        case "tool": setTool(sc.arg); return;
        case "brush": adjustBrushSize(sc.arg); return;
        case "opacity":
            // Ignore Z still held from a prior Ctrl+Z chord — otherwise the
            // autorepeat that fires after Ctrl is released would shift opacity.
            if (sc.arg < 0 && zSuppressed) return;
            adjustOpacity(sc.arg);
            return;
        case "next-missing": jumpToNextMissing(); return;
        case "overlay": pressOverlay(sc.arg); return;
        case "toggle-missing":
            missingHighlight = !missingHighlight;
            blitMask();
            return;
        case "hide-mask":
            ev.preventDefault();
            if (!spaceHeld) {
                spaceHeld = true;
                document.getElementById("canvas-viewport").classList.add("space-held");
            }
            if (!maskHidden) { maskHidden = true; blitMask(); }
            return;
    }
}

// Undo the Space hold: flag, grab cursor and hidden mask. Shared by keyup
// and window blur (where the keyup never arrives).
function releaseSpace() {
    const wasHidden = maskHidden;
    spaceHeld = false;
    document.getElementById("canvas-viewport").classList.remove("space-held");
    maskHidden = false;
    if (wasHidden) blitMask();
}

function onKeyUp(ev) {
    if (document.getElementById("view-editor").classList.contains("hidden")) return;
    if (ev.key === " ") releaseSpace();
    const upLow = ev.key.toLowerCase();
    if (KEY_TO_OVERLAY[upLow]) releaseOverlay(KEY_TO_OVERLAY[upLow]);
    if (ev.key === "z" || ev.key === "Z") zSuppressed = false;
}

function adjustBrushSize(delta) {
    brushSizeIdx = Math.max(0, Math.min(BRUSH_SIZES.length - 1, brushSizeIdx + delta));
    const bs = document.getElementById("brush-size");
    bs.value = String(brushSizeIdx);
    document.getElementById("brush-size-label").textContent = BRUSH_SIZES[brushSizeIdx];
    syncRangeFill(bs);
}

// Filled portion of a styled range track (CSS reads --fill).
function syncRangeFill(input) {
    const min = Number(input.min) || 0, max = Number(input.max) || 100;
    const pct = max > min ? (100 * (Number(input.value) - min)) / (max - min) : 0;
    input.style.setProperty("--fill", `${pct}%`);
}

function adjustOpacity(delta) {
    maskOpacity = Math.max(0, Math.min(1, maskOpacity + delta));
    const op = document.getElementById("mask-opacity");
    op.value = String(Math.round(maskOpacity * 100));
    document.getElementById("opacity-label").textContent = `${op.value}%`;
    syncRangeFill(op);
    blitMask();
}

// --- Submit / problem ---

let _submitting = false;
async function submit() {
    if (!currentTile || _submitting) return;
    if (isClassificationProject()) {
        const errs = validateClassification();
        if (errs.length) {
            showToast(errs[0], "error");
            return;
        }
    } else if (maskCompleteRequired && filledCount < PIXELS) {
        // Loose projects (mask_complete_required=false) accept any mask — the
        // backend mirrors this rule.
        const missing = PIXELS - filledCount;
        showToast(`Faltam ${missing} pixels.`, "error");
        flashMissing();
        return;
    }
    // Claim the guard BEFORE the (async) confirm modal — otherwise a double
    // click opens two modals and confirming both fires two POSTs.
    _submitting = true;
    const isReview = currentTile.status === "in_review";
    const ok = await confirmAction({
        title: isReview ? "Aprovar revisão?" : "Submeter classificação?",
        message: isReview
            ? "A revisão será aprovada e o tile marcado como revisado."
            : "A classificação será enviada e o tile passará para a fila de revisão.",
        okLabel: isReview ? "Aprovar" : "Submeter",
        iconName: isReview ? "circle-check" : "send",
        tone: isReview ? "ok" : "accent",
    });
    if (!ok) { _submitting = false; return; }
    const btn = document.getElementById("btn-submit");
    const label = document.getElementById("submit-label");
    const prevLabel = label?.textContent;
    if (btn) btn.disabled = true;
    if (label) label.textContent = "Enviando...";
    try {
        const version = currentTile.version != null ? String(currentTile.version) : "";
        const versionHeaders = version ? { "X-Tile-Version": version } : {};
        if (isClassificationProject()) {
            await apiPostJson(
                `/api/tiles/${currentTile.id}/classify`, JSON.parse(getClassificationBody()),
                versionHeaders,
            );
        } else {
            await apiPostBytes(
                `/api/tiles/${currentTile.id}/classify`, mask, versionHeaders,
            );
        }
        clearBackup();
        todayCount++;
        document.getElementById("today-count").textContent = todayCount;
        loadQueueStats();
        // Clear the canvas immediately instead of waiting out the flash — the
        // operator shouldn't see the previous tile's painted mask lingering
        // while they decide whether to pull the next one.
        if (isClassificationProject()) exitClassificationTile();
        flashSuccess();
        showIdleScreen("Tile enviado ✓", "Verificando próximo tile...", { previewNext: true, variant: "done" });
    } catch (e) {
        const err = e.body?.detail?.error;
        if (err === "unfilled_pixels") {
            showToast(`Faltam ${e.body.detail.missing} pixels (servidor).`, "error");
        } else if (err === "tile_modified") {
            showToast("Este tile foi modificado por um admin. Seu progresso está salvo localmente.",
                "error", 8000);
        } else {
            showToast(e.message, "error");
        }
    } finally {
        _submitting = false;
        if (btn) btn.disabled = false;
        if (label && prevLabel) label.textContent = prevLabel;
        updateProgress();
    }
}

function flashMissing() {
    let visible = true;
    const start = Date.now();
    const step = () => {
        ctxCursor.clearRect(0, 0, DISPLAY, DISPLAY);
        if (visible) {
            ctxCursor.fillStyle = palette.error;
            ctxCursor.globalAlpha = 0.7;
            for (let y = 0; y < TILE; y++) {
                for (let x = 0; x < TILE; x++) {
                    if (mask[y*TILE+x] === 255) ctxCursor.fillRect(x*SCALE, y*SCALE, SCALE, SCALE);
                }
            }
            ctxCursor.globalAlpha = 1;
        }
        visible = !visible;
        if (Date.now() - start < 2000) setTimeout(step, 200);
        else ctxCursor.clearRect(0, 0, DISPLAY, DISPLAY);
    };
    step();
}

// Idle card icon per situation (shown until/unless a preview thumbnail loads).
const IDLE_ICONS = {
    start: { name: "play", tone: "accent" },
    done: { name: "circle-check", tone: "ok" },
    paused: { name: "circle-pause", tone: "paused" },
};

function showIdleScreen(title, message, { previewNext = false, variant = "start" } = {}) {
    hidePausedResumeScreen();
    const ic = IDLE_ICONS[variant] || IDLE_ICONS.start;
    document.getElementById("idle-icon").className = `no-tiles-icon tone-${ic.tone}`;
    document.getElementById("idle-icon-use").setAttribute("href", `${ICON_SPRITE}#i-${ic.name}`);
    document.getElementById("idle-screen").dataset.variant = variant;
    setTileOpen(false);
    stopHeartbeat();
    document.title = "TileClass";
    _previewToken++;
    resetPreview("idle-preview", "idle-icon");
    document.getElementById("idle-title").textContent = title;
    const msgEl = document.getElementById("idle-message");
    msgEl.textContent = message;
    // Drop any hint-pill styling from a previous idle showing while the new
    // preview (if any) is being fetched.
    msgEl.classList.remove("idle-hint-classify", "idle-hint-review");
    document.getElementById("idle-screen").classList.remove("hidden");
    setTimeout(() => document.getElementById("idle-start").focus(), 0);
    currentTile = null;
    renderTileLabel(null);
    mask.fill(255);
    filledCount = 0;
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
    updateProgress();
    // Tell the operator up front whether the next tile will be a
    // classification or a review — they should know what they're about
    // to commit to before clicking.
    if (previewNext) updateIdleNextHint();
}

// Each idle/paused show bumps a token — async preview fetches that started
// before the screen was dismissed (or replaced) check the token before
// touching the DOM, so a slow load never paints onto a screen the user has
// already left.
let _previewToken = 0;

function resetPreview(imgId, iconId) {
    const img = document.getElementById(imgId);
    const icon = iconId ? document.getElementById(iconId) : null;
    if (img) {
        // Revoke any previous blob URL so we don't leak object URLs across
        // back-to-back idle/paused transitions.
        const prev = img.getAttribute("src");
        if (prev && prev.startsWith("blob:")) URL.revokeObjectURL(prev);
        img.classList.add("hidden");
        img.removeAttribute("src");
        img.closest(".state-visual")?.classList.remove("has-preview");
    }
    if (icon) icon.classList.remove("hidden");
}

async function loadTilePreview(tileId, imgId, iconId, token) {
    const img = document.getElementById(imgId);
    const icon = iconId ? document.getElementById(iconId) : null;
    if (!img || !tileId) return;
    let blob;
    try {
        // Auth-protected endpoint — must go through apiGetBlob to attach the
        // Bearer token (a plain <img src=...> would 401).
        blob = await apiGetBlob(`/api/tiles/${tileId}/satellite-thumbnail?size=320`);
    } catch {
        // Satellite source unavailable (no MBTiles, network) — keep the
        // fallback icon, no toast since the prompt itself still works.
        return;
    }
    if (token !== _previewToken) return;
    const url = URL.createObjectURL(blob);
    img.src = url;
    img.classList.remove("hidden");
    // The state icon stays, re-styled by CSS as a badge over the thumbnail.
    img.closest(".state-visual")?.classList.add("has-preview");
}

function describeNextPreview(t) {
    if (!t) return { label: "Fila vazia", kind: null };
    // status='classified' → /next will promote to 'in_review' for this user.
    // status='pending'    → will become 'in_progress'.
    // status='in_progress'/'in_review' → user's own resume (bulk-assign queue).
    let kind, verb;
    if (t.status === "classified") { kind = "review"; verb = "Revisar"; }
    else if (t.status === "in_review") { kind = "review"; verb = "Continuar revisão"; }
    else if (t.status === "in_progress") { kind = "classify"; verb = "Continuar classificação"; }
    else { kind = "classify"; verb = "Classificar"; }
    return { label: `Próximo: ${verb} · ${truncateName(t.name)} (#${t.id})`, kind };
}

async function updateIdleNextHint() {
    const msg = document.getElementById("idle-message");
    const idle = document.getElementById("idle-screen");
    if (!msg || !idle) return;
    const token = _previewToken;
    try {
        const next = await apiGet(withProjectParam("/api/tiles/next-preview", activeProjectId));
        // Avoid overwriting if the user already left the idle screen while the
        // preview was in flight (e.g. clicked "Iniciar tile" quickly).
        if (idle.classList.contains("hidden") || token !== _previewToken) return;
        const { label, kind } = describeNextPreview(next);
        msg.textContent = label;
        msg.classList.remove("idle-hint-classify", "idle-hint-review");
        if (kind === "review") msg.classList.add("idle-hint-review");
        else if (kind === "classify") msg.classList.add("idle-hint-classify");
        if (next?.id) loadTilePreview(next.id, "idle-preview", "idle-icon", token);
    } catch {}
}

function hideIdleScreen() {
    document.getElementById("idle-screen").classList.add("hidden");
    _previewToken++;
    resetPreview("idle-preview", "idle-icon");
}

function showNoTilesScreen() {
    const el = document.getElementById("no-tiles-screen");
    if (el) el.classList.remove("hidden");
    setTileOpen(false);
    currentTile = null;
    renderTileLabel(null);
    // Wipe the canvas so the operator can't keep painting on a stale tile.
    mask.fill(255);
    filledCount = 0;
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
    updateProgress();
}

function hideNoTilesScreen() {
    const el = document.getElementById("no-tiles-screen");
    if (el) el.classList.add("hidden");
}

function flashSuccess() {
    const stack = document.getElementById("canvas-stack");
    stack.classList.add("canvas-flash-ok");
    setTimeout(() => stack.classList.remove("canvas-flash-ok"), 220);
    // The idle-screen overlay (rgba 0.88) shows immediately after submit and
    // would otherwise mask the canvas flash. Animate the idle box itself once
    // it appears so the operator sees a clear success signal on top.
    setTimeout(() => {
        const box = document.querySelector("#idle-screen .no-tiles-box");
        if (!box) return;
        box.classList.remove("idle-flash-ok");
        // Force reflow so the animation restarts even on back-to-back submits.
        void box.offsetWidth;
        box.classList.add("idle-flash-ok");
        setTimeout(() => box.classList.remove("idle-flash-ok"), 700);
    }, 50);
}

function clearCanvasFlash() {
    document.getElementById("canvas-stack").classList.remove("canvas-flash-ok");
}

function openShortcutsModal() {
    document.getElementById("modal-shortcuts").classList.remove("hidden");
    document.getElementById("shortcuts-close").focus();
}
function closeShortcutsModal() {
    document.getElementById("modal-shortcuts").classList.add("hidden");
}

function openProblemModal() {
    const note = document.getElementById("problem-note");
    note.value = "";
    note.dispatchEvent(new Event("input"));
    document.getElementById("modal-problem").classList.remove("hidden");
    note.focus();
}
function closeProblemModal() {
    document.getElementById("modal-problem").classList.add("hidden");
}
async function confirmProblem() {
    const note = document.getElementById("problem-note").value.trim();
    if (!note) { showToast("Descreva o problema.", "error"); return; }
    try {
        await apiPostJson(`/api/tiles/${currentTile.id}/report-problem`, { note });
        clearBackup();
        clearPreload();
        closeProblemModal();
        showToast("Problema reportado.", "success");
        await loadNext();
    } catch (e) {
        showToast(`Erro: ${e.message}`, "error");
    }
}

function openRequestChangesModal() {
    if (!currentTile) return;
    document.getElementById("request-changes-note").value = "";
    document.getElementById("modal-request-changes").classList.remove("hidden");
    document.getElementById("request-changes-note").focus();
}
function closeRequestChangesModal() {
    document.getElementById("modal-request-changes").classList.add("hidden");
}
async function confirmRequestChanges() {
    const note = document.getElementById("request-changes-note").value.trim();
    if (!note) { showToast("Descreva o ajuste necessário.", "error"); return; }
    try {
        await apiPostJson(`/api/tiles/${currentTile.id}/request-changes`, { note });
        clearBackup();
        clearPreload();
        closeRequestChangesModal();
        showToast("Tile devolvido ao classificador.", "success");
        showIdleScreen("Ajuste solicitado ✓", "Verificando próximo tile...", { previewNext: true, variant: "done" });
    } catch (e) {
        showToast(`Erro: ${e.message}`, "error");
    }
}

function openInGoogleMaps() {
    if (!currentTile) {
        showToast("Nenhum tile aberto.", "warn");
        return;
    }
    const lat = (currentTile.bbox_south + currentTile.bbox_north) / 2;
    const lon = (currentTile.bbox_west + currentTile.bbox_east) / 2;
    // data=!3m1!1e3 forces the satellite/earth layer; zoom 18 frames a 640m tile.
    const url = `https://www.google.com/maps/@${lat},${lon},18z/data=!3m1!1e3`;
    window.open(url, "_blank", "noopener,noreferrer");
}

function openInGoogleEarth() {
    if (!currentTile) {
        showToast("Nenhum tile aberto.", "warn");
        return;
    }
    const lat = (currentTile.bbox_south + currentTile.bbox_north) / 2;
    const lon = (currentTile.bbox_west + currentTile.bbox_east) / 2;
    // 1500d = camera distance in meters (frames a ~640m tile with margin); 0t = top-down.
    const url = `https://earth.google.com/web/@${lat},${lon},0a,1500d,1y,0h,0t,0r`;
    window.open(url, "_blank", "noopener,noreferrer");
}

// --- Pause / Resume ---

async function pauseTile() {
    const btn = document.getElementById("btn-pause");
    if (btn?.disabled) return;
    if (!currentTile) {
        showToast("Nenhum tile aberto para pausar.", "warn");
        return;
    }
    if (isClassificationProject()) {
        showToast("Projetos de classification não suportam pause.", "warn");
        return;
    }
    const ok = await confirmAction({
        title: "Pausar tile?",
        message: "Seu progresso será salvo no servidor e o tempo será congelado. Você poderá retomar mais tarde.",
        okLabel: "Pausar",
        iconName: "circle-pause",
        tone: "paused",
    });
    if (!ok) return;
    if (btn) btn.disabled = true;
    try {
        const version = currentTile.version != null ? String(currentTile.version) : "";
        await apiPostBytes(
            `/api/tiles/${currentTile.id}/pause`, mask,
            version ? { "X-Tile-Version": version } : {},
        );
        clearBackup();
        // The pause bumped the tile's version and changed its mask; any
        // preload is stale (and must never stand in for this tile).
        clearPreload();
        if (isClassificationProject()) exitClassificationTile();
        showToast("Tile pausado. Suas alterações foram salvas no servidor.", "success");
        showIdleScreen("Tile pausado", "Faça login depois para continuar de onde parou.", { variant: "paused" });
    } catch (e) {
        const detail = e.body?.detail;
        const err = detail?.error;
        if (err === "tile_modified") {
            showToast("Outro dispositivo modificou este tile. Recarregando...", "warn");
            setTimeout(() => location.reload(), 1500);
        } else if (err === "unfilled_pixels") {
            // Pausing a review requires a complete mask (mask_complete_required):
            // same feedback as an incomplete submit.
            const missing = detail.missing ?? (PIXELS - filledCount);
            showToast(detail.message || `Faltam ${missing} pixels.`, "error", 5000);
            flashMissing();
        } else {
            showToast(`Erro ao pausar: ${e.message}`, "error");
        }
    } finally {
        if (btn) btn.disabled = false;
    }
}

function showPausedResumeScreen(tile) {
    setTileOpen(false);
    _previewToken++;
    resetPreview("paused-preview", "paused-icon");
    const msg = document.getElementById("paused-resume-message");
    if (msg) {
        msg.textContent = `Tile: ${truncateName(tile.name)} (#${tile.id}). Deseja continuar de onde parou?`;
    }
    const btn = document.getElementById("paused-resume-continue");
    if (btn) {
        btn.dataset.tileId = String(tile.id);
        btn.disabled = false;
    }
    const el = document.getElementById("paused-resume-screen");
    if (el) el.classList.remove("hidden");
    setTimeout(() => btn?.focus(), 0);
    loadTilePreview(tile.id, "paused-preview", "paused-icon", _previewToken);
}

function hidePausedResumeScreen() {
    const el = document.getElementById("paused-resume-screen");
    if (el) el.classList.add("hidden");
    const btn = document.getElementById("paused-resume-continue");
    if (btn) delete btn.dataset.tileId;
    _previewToken++;
    resetPreview("paused-preview", "paused-icon");
}

async function continuePausedTile() {
    const btn = document.getElementById("paused-resume-continue");
    const id = Number(btn?.dataset.tileId);
    if (!id) return;
    btn.disabled = true;
    try {
        const { tile: refreshed, maskBytes } = await resumeWithMask(id);
        hidePausedResumeScreen();
        await loadTile(refreshed, maskBytes);
        preloadNext();
    } catch (e) {
        showToast(`Erro ao retomar: ${e.message}`, "error");
        btn.disabled = false;
    }
}
