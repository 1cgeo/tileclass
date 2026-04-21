// Canvas editor: MapLibre renders the XYZ satellite underlay; two overlay canvases
// (mask + cursor) sit georeferenced on top of the tile bbox.
import { apiGet, apiGetBlob, apiPostBytes, apiPostJson, onSessionWarning, logout as apiLogout } from "./api.js";
import { showToast } from "./toast.js";
import { createLockedMap, setMapBbox, updateMapSource } from "./maplib.js";

// Context view shows 3x3 tiles around the paintable center.
const CONTEXT_FACTOR = 3;
function expandBbox(bbox, factor) {
    const [w, s, e, n] = bbox;
    const cx = (w + e) / 2, cy = (s + n) / 2;
    const hw = (e - w) * factor / 2, hh = (n - s) * factor / 2;
    return [cx - hw, cy - hh, cx + hw, cy + hh];
}
import { hexToRgb, blobToImage } from "./utils.js";
import {
    paintAt as corePaintAt,
    paintLine as corePaintLine,
    floodFill as coreFloodFill,
    toUndoEntry,
    applyPatch as coreApplyPatch,
    screenToLogical as coreScreenToLogical,
} from "./mask-core.js";

const TILE = 256;
const DISPLAY = 768;
const SCALE = DISPLAY / TILE;
const PIXELS = TILE * TILE;
const BRUSH_SIZES = [1, 3, 5, 7, 11];
const LS_BACKUP_KEY = "tileclass_backup";

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
let tileserverUrlSecondary = null;
let tileserverMaxZoom = 22;
let tileserverMaxZoomSecondary = 22;
let activeSource = "primary"; // "primary" | "secondary"
let todayCount = 0;

// Preload cache for the "next" tile while user paints
let preloadedNext = null;
let _pendingBackup = null;

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

// Offscreen 256x256 for mask imageData
const maskImageData = ctxMask.createImageData(TILE, TILE);
const offCanvas = document.createElement("canvas");
offCanvas.width = TILE; offCanvas.height = TILE;
const offCtx = offCanvas.getContext("2d");

let satMap = null;

// Zoom/pan state for the canvas-stack. zoom=1 fits the paintable area in the
// viewport; higher values zoom into the 256x256 tile. panX/panY are in CSS
// pixels at the current zoom (applied AFTER scale via translate).
let zoom = 1;
let panX = 0, panY = 0;
const MIN_ZOOM = 1;
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
// Paint-pixel grid: one line every 2.5 m (= every paint pixel). Line thickness
// is 0.5 m on the ground so each cell has a visible frame and the interior is
// what gets colored. Purely visual — painting is still 256×256.
const GRID_LINE_METERS = 0.5;
const GRID_CELL_METERS = 2.5;
const GRID_LINE_DISPLAY = SCALE * (GRID_LINE_METERS / GRID_CELL_METERS); // in DISPLAY units

export async function initEditor(user) {
    currentUser = user;
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
        };
    }
    document.getElementById("user-label").textContent = user.username;
    onSessionWarning(() => {
        showToast("Sua sessão expira em breve. Submeta seu trabalho e faça login novamente.", "warn", 30_000);
    });
    const [cfg, cls] = await Promise.all([
        apiGet("/api/config/tileserver"),
        apiGet("/api/config/classes"),
    ]);
    tileserverUrl = cfg.url_template;
    tileserverUrlSecondary = cfg.secondary_url_template || null;
    tileserverMaxZoom = cfg.max_zoom ?? 22;
    tileserverMaxZoomSecondary = cfg.secondary_max_zoom ?? 22;
    classes = cls;
    classesById = Object.fromEntries(classes.map(c => [c.id, c]));
    buildClassPanel();
    buildColorLut();
    attachEvents();
    // Paused tiles need an explicit confirmation before /resume restarts the timer.
    let resume = null;
    try {
        [, , resume] = await Promise.all([
            loadTodayCount(),
            loadQueueStats(),
            apiGet("/api/tiles/assigned").catch(() => null),
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
    showIdleScreen("Pronto para começar", "Clique para receber um tile.");
}

async function loadTodayCount() {
    try {
        const r = await apiGet("/api/me/stats-today");
        todayCount = r.count || 0;
        document.getElementById("today-count").textContent = todayCount;
    } catch {}
}

async function loadQueueStats() {
    try {
        const q = await apiGet("/api/tiles/queue-stats");
        const el = document.getElementById("queue-progress");
        el.textContent = `${q.reviewed}/${q.total} revisados · ${q.classified}/${q.total} classificados`;
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
    ul.innerHTML = "";
    classes.forEach((c, i) => {
        const li = document.createElement("li");
        li.dataset.id = c.id;
        const sw = document.createElement("span");
        sw.className = "swatch"; sw.style.backgroundColor = c.color;
        const name = document.createElement("span"); name.textContent = c.name;
        const key = document.createElement("span"); key.className = "key"; key.textContent = String(i+1);
        li.append(sw, name, key);
        li.addEventListener("click", () => setActiveClass(c.id));
        ul.appendChild(li);
    });
    setActiveClass(classes[0].id);
}

function setActiveClass(id) {
    activeClass = id;
    document.querySelectorAll("#class-list li").forEach(li => {
        li.classList.toggle("active", Number(li.dataset.id) === id);
    });
    // feedback: brief border flash on canvas stack
    const stack = document.getElementById("canvas-stack");
    stack.style.transition = "box-shadow 80ms";
    stack.style.boxShadow = `0 0 0 3px ${classesById[id]?.color || "#fff"} inset`;
    setTimeout(() => { stack.style.boxShadow = ""; }, 120);
}

// --- Loading / saving ---
async function loadNext() {
    clearCanvasFlash();
    try {
        let t;
        if (preloadedNext) {
            t = preloadedNext.tile;
            // We still must call /next to actually assign. Preloaded PNG may differ.
            const real = await apiGet("/api/tiles/next");
            if (!real) {
                preloadedNext = null;
                showNoTilesScreen();
                return;
            }
            if (real.id === t.id) {
                await loadTile(real, preloadedNext.maskBytes);
            } else {
                await loadTile(real);
            }
            preloadedNext = null;
        } else {
            t = await apiGet("/api/tiles/next");
            if (!t) {
                showNoTilesScreen();
                return;
            }
            await loadTile(t);
        }
        // Fire and forget preload of the next candidate
        preloadNext();
    } catch (e) {
        showToast(`Erro ao carregar: ${e.message}`, "error");
    }
}

async function preloadNext() {
    try {
        const peek = await apiGet("/api/tiles/next-preview");
        if (!peek) { preloadedNext = null; return; }
        const blob = await apiGetBlob(`/api/tiles/${peek.id}/image`, { retries: 2, timeout: 8_000 });
        const img = await blobToImage(blob);
        const tmp = document.createElement("canvas");
        tmp.width = TILE; tmp.height = TILE;
        tmp.getContext("2d").drawImage(img, 0, 0, TILE, TILE);
        const data = tmp.getContext("2d").getImageData(0, 0, TILE, TILE).data;
        const m = new Uint8Array(PIXELS);
        for (let i = 0; i < PIXELS; i++) m[i] = data[i*4];
        preloadedNext = { tile: peek, maskBytes: m };
    } catch (e) {
        preloadedNext = null;
        if (e?.timeout) showToast("Não foi possível pré-carregar o próximo tile.", "warn", 4000);
    }
}

async function loadTile(t, preloadedMask = null) {
    _tileReady = false;
    currentTile = t;
    undoStack.length = 0; redoStack.length = 0;
    updateUndoRedoButtons();
    hideNoTilesScreen();
    hideIdleScreen();
    hidePausedResumeScreen();
    document.getElementById("tile-name-label").textContent = `Tile: ${t.name} (#${t.id})`;
    const reviewBanner = document.getElementById("review-banner");
    const modePill = document.getElementById("mode-pill");
    if (t.status === "in_review") {
        reviewBanner.textContent = `Classificado por ${t.classified_by_username || "?"}`;
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
    _tileReady = true;
}

async function loadMaskFromServer(tileId) {
    const blob = await apiGetBlob(`/api/tiles/${tileId}/image`);
    const img = await blobToImage(blob);
    const tmp = document.createElement("canvas");
    tmp.width = TILE; tmp.height = TILE;
    const tctx = tmp.getContext("2d");
    tctx.drawImage(img, 0, 0, TILE, TILE);
    const data = tctx.getImageData(0, 0, TILE, TILE).data;
    mask = new Uint8Array(PIXELS);
    for (let i = 0; i < PIXELS; i++) mask[i] = data[i * 4];
}

function tryRestoreBackup() {
    hideBackupBanner();
    try {
        const raw = localStorage.getItem(LS_BACKUP_KEY);
        if (!raw) return;
        const b = JSON.parse(raw);
        if (b.tileId !== currentTile.id || !b.mask) return;
        const restored = Uint8Array.from(atob(b.mask), c => c.charCodeAt(0));
        if (restored.length !== PIXELS) return;
        // Compare — if identical to current, nothing to offer.
        let differs = restored.length !== mask.length;
        if (!differs) {
            for (let i = 0; i < PIXELS; i++) { if (restored[i] !== mask[i]) { differs = true; break; } }
        }
        if (!differs) return;
        _pendingBackup = restored;
        showBackupBanner();
    } catch {}
}

function showBackupBanner() {
    const el = document.getElementById("backup-banner");
    if (!el) return;
    el.classList.remove("hidden");
}
function hideBackupBanner() {
    const el = document.getElementById("backup-banner");
    if (el) el.classList.add("hidden");
    _pendingBackup = null;
}
function applyPendingBackup() {
    if (!_pendingBackup) return;
    mask = _pendingBackup;
    _pendingBackup = null;
    hideBackupBanner();
    recountFilled();
    renderMaskFull();
    saveBackup();
    showToast("Trabalho local restaurado.", "success");
}
function discardPendingBackup() {
    _pendingBackup = null;
    hideBackupBanner();
    clearBackup();
}

function uint8ToBase64(arr) {
    let s = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < arr.length; i += CHUNK) {
        s += String.fromCharCode.apply(null, arr.subarray(i, i + CHUNK));
    }
    return btoa(s);
}

function saveBackup() {
    if (!currentTile) return;
    try {
        const b64 = uint8ToBase64(mask);
        localStorage.setItem(LS_BACKUP_KEY, JSON.stringify({ tileId: currentTile.id, mask: b64 }));
    } catch {}
}

function clearBackup() { localStorage.removeItem(LS_BACKUP_KEY); }

function recountFilled() {
    let c = 0;
    for (let i = 0; i < PIXELS; i++) if (mask[i] !== 255) c++;
    filledCount = c;
    updateProgress();
}

function updateProgress() {
    document.getElementById("pixel-count").textContent = filledCount;
    const pct = (100 * filledCount / PIXELS).toFixed(1);
    document.getElementById("pixel-pct").textContent = pct;
    const line = document.getElementById("progress-line");
    line.classList.toggle("complete", filledCount === PIXELS);
    const bar = document.getElementById("progress-bar-fill");
    if (bar) bar.style.width = pct + "%";
    const missing = PIXELS - filledCount;
    const missLine = document.getElementById("missing-line");
    const missCount = document.getElementById("missing-count");
    if (missLine && missCount) {
        missLine.classList.toggle("hidden", missing === 0);
        missCount.textContent = missing.toLocaleString("pt-BR");
    }
    updateSubmitButton(missing);
}

function updateSubmitButton(missing) {
    const btn = document.getElementById("btn-submit");
    const label = document.getElementById("submit-label");
    if (!btn || !label) return;
    const isReview = currentTile && currentTile.status === "in_review";
    const baseLabel = isReview ? "Aprovar revisão" : "Submeter";
    if (missing > 0) {
        btn.classList.add("incomplete");
        label.textContent = `Faltam ${missing.toLocaleString("pt-BR")} px`;
        btn.title = `Complete a máscara — faltam ${missing} pixels`;
    } else {
        btn.classList.remove("incomplete");
        label.textContent = baseLabel;
        btn.title = isReview ? "Aprovar esta revisão (Ctrl+S)" : "Submeter classificação (Ctrl+S)";
    }
}

// --- Rendering ---

function toggleSecondarySource() {
    if (!tileserverUrlSecondary) {
        showToast("Imagem secundária não configurada.", "warn", 2500);
        return;
    }
    if (!satMap) return;
    activeSource = activeSource === "primary" ? "secondary" : "primary";
    const url = activeSource === "primary" ? tileserverUrl : tileserverUrlSecondary;
    const mz = activeSource === "primary" ? tileserverMaxZoom : tileserverMaxZoomSecondary;
    _mapErrorCount = 0;
    document.getElementById("map-warning")?.classList.add("hidden");
    updateMapSource(satMap, url, mz);
    showToast(`Imagem: ${activeSource === "primary" ? "principal" : "secundária"}`, "info", 1200);
}

let _mapErrorCount = 0;
function renderSatellite(t) {
    const tileBbox = [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north];
    const bbox = expandBbox(tileBbox, CONTEXT_FACTOR);
    const warnEl = document.getElementById("map-warning");
    if (warnEl) warnEl.classList.add("hidden");
    _mapErrorCount = 0;
    if (!satMap) {
        satMap = createLockedMap("map-satellite", tileserverUrl, bbox, tileserverMaxZoom);
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

const maskData32 = new Uint32Array(maskImageData.data.buffer);
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
    ctxMask.fillStyle = "rgba(255, 0, 255, 0.85)";
    for (let y = 0; y < TILE; y++) {
        const row = y * TILE;
        for (let x = 0; x < TILE; x++) {
            if (mask[row + x] === 255) ctxMask.fillRect(x*SCALE, y*SCALE, SCALE, SCALE);
        }
    }
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
    return coreScreenToLogical(canvasCursor.getBoundingClientRect(), ev.clientX, ev.clientY);
}

function brushRadius() { return Math.floor(BRUSH_SIZES[brushSizeIdx] / 2); }

function paintAt(cx, cy) {
    const r = brushRadius();
    const value = tool === "eraser" ? 255 : activeClass;
    const res = corePaintAt(mask, cx, cy, value, r, gestureTouched);
    filledCount += res.deltaFilled;
    const [x0, y0, x1, y1] = res.region;
    writeMaskPixels(x0, y0, x1, y1);
    blitMask();
}

function paintLine(x0, y0, x1, y1) {
    const value = tool === "eraser" ? 255 : activeClass;
    const res = corePaintLine(mask, x0, y0, x1, y1, value, brushRadius());
    filledCount += res.deltaFilled;
    // Merge touched into the gesture map
    for (const [i, v] of res.touched) if (!gestureTouched.has(i)) gestureTouched.set(i, v);
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
}

function floodFill(cx, cy) {
    const replacement = activeClass;
    const res = coreFloodFill(mask, cx, cy, replacement);
    if (res.touched.size === 0) return;
    filledCount += res.deltaFilled;
    pushUndo(res.touched);
    renderMaskFull();
    updateProgress();
    saveBackup();
}

function pushUndo(touchedMap) {
    undoStack.push(toUndoEntry(touchedMap));
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
        document.getElementById("hover-class").textContent = "—";
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
    if (ntLogout) ntLogout.addEventListener("click", async () => { await apiLogout(); location.reload(); });

    document.getElementById("tool-brush").addEventListener("click", () => setTool("brush"));
    document.getElementById("tool-eraser").addEventListener("click", () => setTool("eraser"));
    document.getElementById("tool-fill").addEventListener("click", () => setTool("fill"));

    const bs = document.getElementById("brush-size");
    bs.min = 0; bs.max = String(BRUSH_SIZES.length - 1); bs.value = String(brushSizeIdx);
    bs.addEventListener("input", () => {
        brushSizeIdx = Number(bs.value);
        document.getElementById("brush-size-label").textContent = BRUSH_SIZES[brushSizeIdx];
    });
    document.getElementById("brush-size-label").textContent = BRUSH_SIZES[brushSizeIdx];

    const op = document.getElementById("mask-opacity");
    op.addEventListener("input", () => {
        maskOpacity = Number(op.value) / 100;
        document.getElementById("opacity-label").textContent = `${op.value}%`;
        blitMask();
    });

    document.getElementById("btn-undo").addEventListener("click", undo);
    document.getElementById("btn-redo").addEventListener("click", redo);
    document.getElementById("btn-submit").addEventListener("click", submit);
    document.getElementById("btn-problem").addEventListener("click", openProblemModal);
    const btnPause = document.getElementById("btn-pause");
    if (btnPause) btnPause.addEventListener("click", pauseTile);

    const resumeContinue = document.getElementById("paused-resume-continue");
    if (resumeContinue) resumeContinue.addEventListener("click", continuePausedTile);
    const resumeLogout = document.getElementById("paused-resume-logout");
    if (resumeLogout) resumeLogout.addEventListener("click", () => {
        document.getElementById("btn-logout").click();
    });

    document.getElementById("idle-start").addEventListener("click", () => {
        hideIdleScreen();
        loadNext();
    });
    document.getElementById("idle-logout").addEventListener("click", () => {
        document.getElementById("btn-logout").click();
    });

    document.getElementById("problem-cancel").addEventListener("click", closeProblemModal);
    document.getElementById("problem-confirm").addEventListener("click", confirmProblem);

    document.getElementById("btn-help").addEventListener("click", () => document.getElementById("modal-help").classList.remove("hidden"));
    const btnHelpInline = document.getElementById("btn-help-inline");
    if (btnHelpInline) btnHelpInline.addEventListener("click", () => document.getElementById("modal-help").classList.remove("hidden"));
    document.getElementById("help-close").addEventListener("click", () => document.getElementById("modal-help").classList.add("hidden"));

    document.getElementById("btn-logout").addEventListener("click", async () => {
        await apiLogout();
        location.reload();
    });

    const backupApply = document.getElementById("backup-apply");
    const backupDiscard = document.getElementById("backup-discard");
    if (backupApply) backupApply.addEventListener("click", applyPendingBackup);
    if (backupDiscard) backupDiscard.addEventListener("click", discardPendingBackup);

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
    window.addEventListener("blur", () => { zSuppressed = false; spaceHeld = false; });
}

function isTextFocused() {
    const el = document.activeElement;
    return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA");
}
function isModalOpen() {
    return !document.getElementById("modal-problem").classList.contains("hidden") ||
           !document.getElementById("modal-help").classList.contains("hidden");
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
    if (v === 255) el.textContent = "(não preenchido)";
    else el.textContent = classesById[v]?.name || `classe ${v}`;
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

function resetView() {
    zoom = 1; panX = 0; panY = 0;
    applyTransform();
    drawGrid();
}

// --- 256×256 grid overlay ---
// The grid canvas lives inside the transformed stack at DISPLAY resolution
// (768×768). Lines are drawn with width scaled by 1/zoom so they remain 1 CSS
// pixel thick on screen regardless of zoom level.
function drawGrid() {
    ctxGrid.clearRect(0, 0, DISPLAY, DISPLAY);
    ctxGrid.save();
    // 2.5 m cell grid with 0.5 m thick borders — always visible.
    ctxGrid.lineWidth = GRID_LINE_DISPLAY;
    ctxGrid.strokeStyle = "rgba(0, 0, 0, 0.85)";
    ctxGrid.beginPath();
    for (let i = 0; i <= TILE; i++) {
        const p = i * SCALE;
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
    ctxCursor.strokeStyle = "rgba(0,0,0,0.8)";
    ctxCursor.lineWidth = lw;
    ctxCursor.strokeRect(sx + inset, sy + inset, sz - lw, sz - lw);
    ctxCursor.strokeStyle = "rgba(255,255,255,0.95)";
    ctxCursor.lineWidth = lw;
    ctxCursor.strokeRect(sx - inset, sy - inset, sz + lw, sz + lw);
}

function setTool(t) {
    tool = t;
    for (const id of ["tool-brush","tool-eraser","tool-fill"]) {
        document.getElementById(id).classList.toggle("active", id === `tool-${t}`);
    }
}

function onKeyDown(ev) {
    if (isTextFocused() || isModalOpen()) {
        if (ev.key === "Escape") {
            document.getElementById("modal-problem").classList.add("hidden");
            document.getElementById("modal-help").classList.add("hidden");
        }
        return;
    }
    const k = ev.key;
    if (ev.ctrlKey && (k === "z" || k === "Z")) {
        ev.preventDefault();
        zSuppressed = true;
        if (!ev.repeat) {
            if (ev.shiftKey) redo(); else undo();
        }
        return;
    }
    if (ev.ctrlKey && (k === "y" || k === "Y")) {
        ev.preventDefault();
        if (!ev.repeat) redo();
        return;
    }
    if (k >= "1" && k <= "6") {
        const idx = Number(k) - 1;
        if (classes[idx]) setActiveClass(classes[idx].id);
        return;
    }
    // Left-hand ergonomic layout on QWE / ASD / ZXC.
    const low = k.toLowerCase();
    if (low === "q") { setTool("brush"); return; }
    if (low === "w") { setTool("eraser"); return; }
    if (low === "e") { setTool("fill"); return; }
    if (low === "a") { adjustBrushSize(-1); return; }
    if (low === "s") { adjustBrushSize(1); return; }
    if (low === "z") {
        // Ignore if Z is still held down from a prior Ctrl+Z chord — otherwise
        // the autorepeat that fires after Ctrl is released would shift opacity.
        if (zSuppressed) return;
        adjustOpacity(0.1);
        return;
    }
    if (low === "x") { adjustOpacity(-0.1); return; }
    if (low === "c") { jumpToNextMissing(); return; }
    if (low === "d") { toggleSecondarySource(); return; }
    if (low === "f") { missingHighlight = !missingHighlight; blitMask(); return; }
    if (k === " ") {
        ev.preventDefault();
        if (!spaceHeld) {
            spaceHeld = true;
            document.getElementById("canvas-viewport").classList.add("space-held");
        }
        if (!maskHidden) { maskHidden = true; blitMask(); }
        return;
    }
    if (k === "?") { document.getElementById("modal-help").classList.remove("hidden"); return; }
}

function onKeyUp(ev) {
    if (ev.key === " ") {
        spaceHeld = false;
        document.getElementById("canvas-viewport").classList.remove("space-held");
        maskHidden = false;
        blitMask();
    }
    if (ev.key === "z" || ev.key === "Z") zSuppressed = false;
}

function adjustBrushSize(delta) {
    brushSizeIdx = Math.max(0, Math.min(BRUSH_SIZES.length - 1, brushSizeIdx + delta));
    const bs = document.getElementById("brush-size");
    bs.value = String(brushSizeIdx);
    document.getElementById("brush-size-label").textContent = BRUSH_SIZES[brushSizeIdx];
}

function adjustOpacity(delta) {
    maskOpacity = Math.max(0, Math.min(1, maskOpacity + delta));
    const op = document.getElementById("mask-opacity");
    op.value = String(Math.round(maskOpacity * 100));
    document.getElementById("opacity-label").textContent = `${op.value}%`;
    blitMask();
}

// --- Submit / problem ---

let _submitting = false;
async function submit() {
    if (!currentTile || _submitting) return;
    if (filledCount < PIXELS) {
        const missing = PIXELS - filledCount;
        showToast(`Faltam ${missing} pixels.`, "error");
        flashMissing();
        return;
    }
    _submitting = true;
    const btn = document.getElementById("btn-submit");
    const label = document.getElementById("submit-label");
    const prevLabel = label?.textContent;
    if (btn) btn.disabled = true;
    if (label) label.textContent = "Enviando...";
    try {
        const version = currentTile.version != null ? String(currentTile.version) : "";
        await apiPostBytes(
            `/api/tiles/${currentTile.id}/classify`, mask,
            version ? { "X-Tile-Version": version } : {},
        );
        clearBackup();
        todayCount++;
        document.getElementById("today-count").textContent = todayCount;
        loadQueueStats();
        flashSuccess();
        setTimeout(() => {
            showIdleScreen("Tile enviado ✓", "Clique para iniciar o próximo tile.");
        }, 220);
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
            ctxCursor.fillStyle = "rgba(255,0,0,0.7)";
            for (let y = 0; y < TILE; y++) {
                for (let x = 0; x < TILE; x++) {
                    if (mask[y*TILE+x] === 255) ctxCursor.fillRect(x*SCALE, y*SCALE, SCALE, SCALE);
                }
            }
        }
        visible = !visible;
        if (Date.now() - start < 2000) setTimeout(step, 200);
        else ctxCursor.clearRect(0, 0, DISPLAY, DISPLAY);
    };
    step();
}

function showIdleScreen(title, message) {
    hidePausedResumeScreen();
    document.getElementById("idle-title").textContent = title;
    document.getElementById("idle-message").textContent = message;
    document.getElementById("idle-screen").classList.remove("hidden");
    setTimeout(() => document.getElementById("idle-start").focus(), 0);
    currentTile = null;
    document.getElementById("tile-name-label").textContent = "";
    mask.fill(255);
    filledCount = 0;
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
    updateProgress();
}

function hideIdleScreen() {
    document.getElementById("idle-screen").classList.add("hidden");
}

function showNoTilesScreen() {
    const el = document.getElementById("no-tiles-screen");
    if (el) el.classList.remove("hidden");
    currentTile = null;
    document.getElementById("tile-name-label").textContent = "";
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
}

function clearCanvasFlash() {
    document.getElementById("canvas-stack").classList.remove("canvas-flash-ok");
}

function openProblemModal() {
    document.getElementById("problem-note").value = "";
    document.getElementById("modal-problem").classList.remove("hidden");
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
        closeProblemModal();
        showToast("Problema reportado.", "success");
        await loadNext();
    } catch (e) {
        showToast(`Erro: ${e.message}`, "error");
    }
}

// --- Pause / Resume ---

async function pauseTile() {
    const btn = document.getElementById("btn-pause");
    if (btn?.disabled) return;
    if (!currentTile) {
        showToast("Nenhum tile aberto para pausar.", "warn");
        return;
    }
    if (btn) btn.disabled = true;
    try {
        const version = currentTile.version != null ? String(currentTile.version) : "";
        await apiPostBytes(
            `/api/tiles/${currentTile.id}/pause`, mask,
            version ? { "X-Tile-Version": version } : {},
        );
        clearBackup();
        showToast("Tile pausado. Suas alterações foram salvas no servidor.", "success");
        showIdleScreen("Tile pausado ⏸", "Faça login depois para continuar de onde parou.");
    } catch (e) {
        const err = e.body?.detail?.error;
        if (err === "tile_modified") {
            showToast("Outro dispositivo modificou este tile. Recarregando...", "warn");
            setTimeout(() => location.reload(), 1500);
        } else {
            showToast(`Erro ao pausar: ${e.message}`, "error");
        }
    } finally {
        if (btn) btn.disabled = false;
    }
}

function showPausedResumeScreen(tile) {
    const msg = document.getElementById("paused-resume-message");
    if (msg) {
        msg.textContent = `Tile: ${tile.name} (#${tile.id}). Deseja continuar de onde parou?`;
    }
    const btn = document.getElementById("paused-resume-continue");
    if (btn) btn.dataset.tileId = String(tile.id);
    const el = document.getElementById("paused-resume-screen");
    if (el) el.classList.remove("hidden");
    setTimeout(() => btn?.focus(), 0);
}

function hidePausedResumeScreen() {
    const el = document.getElementById("paused-resume-screen");
    if (el) el.classList.add("hidden");
    const btn = document.getElementById("paused-resume-continue");
    if (btn) delete btn.dataset.tileId;
}

async function continuePausedTile() {
    const btn = document.getElementById("paused-resume-continue");
    const id = Number(btn?.dataset.tileId);
    if (!id) return;
    btn.disabled = true;
    try {
        // Fetch the saved mask in parallel with /resume to halve resume latency.
        const [refreshed, maskBlob] = await Promise.all([
            apiPostJson(`/api/tiles/${id}/resume`, {}),
            apiGetBlob(`/api/tiles/${id}/image`, { retries: 1 }),
        ]);
        const img = await blobToImage(maskBlob);
        const tmp = document.createElement("canvas");
        tmp.width = TILE; tmp.height = TILE;
        tmp.getContext("2d").drawImage(img, 0, 0, TILE, TILE);
        const data = tmp.getContext("2d").getImageData(0, 0, TILE, TILE).data;
        const preloaded = new Uint8Array(PIXELS);
        for (let i = 0; i < PIXELS; i++) preloaded[i] = data[i * 4];
        hidePausedResumeScreen();
        await loadTile(refreshed, preloaded);
        preloadNext();
    } catch (e) {
        showToast(`Erro ao retomar: ${e.message}`, "error");
        btn.disabled = false;
    }
}
