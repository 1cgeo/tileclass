// Canvas editor: MapLibre renders the XYZ satellite underlay; two overlay canvases
// (mask + cursor) sit georeferenced on top of the tile bbox.
import { apiGet, apiGetBlob, apiPostBytes, apiPostJson } from "./api.js";
import { showToast } from "./toast.js";
import { renderMinimap } from "./minimap.js";
import { createLockedMap, setMapBbox } from "./maplib.js";
import { hexToRgb, blobToImage } from "./utils.js";

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
let mask = new Uint8Array(PIXELS);
let filledCount = 0;
let activeClass = 1;
let tool = "brush";
let brushSizeIdx = 1;
let maskOpacity = 0.5;
let outlineOnly = false;
let maskHidden = false;
let tileserverUrl = "";
let todayCount = 0;

// Preload cache for the "next" tile while user paints
let preloadedNext = null;

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
let minimapMap = null;

export async function initEditor(user) {
    currentUser = user;
    document.getElementById("user-label").textContent = user.username;
    const [cfg, cls] = await Promise.all([
        apiGet("/api/config/tileserver"),
        apiGet("/api/config/classes"),
    ]);
    tileserverUrl = cfg.url_template;
    classes = cls;
    classesById = Object.fromEntries(classes.map(c => [c.id, c]));
    buildClassPanel();
    buildColorLut();
    attachEvents();
    await Promise.all([loadTodayCount(), loadQueueStats()]);
    await loadNext();
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
                showToast("Todos os tiles foram processados!", "success", 5000);
                document.getElementById("tile-name-label").textContent = "";
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
                showToast("Todos os tiles foram processados!", "success", 5000);
                document.getElementById("tile-name-label").textContent = "";
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
        const blob = await apiGetBlob(`/api/tiles/${peek.id}/image`);
        const img = await blobToImage(blob);
        const tmp = document.createElement("canvas");
        tmp.width = TILE; tmp.height = TILE;
        tmp.getContext("2d").drawImage(img, 0, 0, TILE, TILE);
        const data = tmp.getContext("2d").getImageData(0, 0, TILE, TILE).data;
        const m = new Uint8Array(PIXELS);
        for (let i = 0; i < PIXELS; i++) m[i] = data[i*4];
        preloadedNext = { tile: peek, maskBytes: m };
    } catch { preloadedNext = null; }
}

async function loadTile(t, preloadedMask = null) {
    currentTile = t;
    undoStack.length = 0; redoStack.length = 0;
    document.getElementById("tile-name-label").textContent = `Tile: ${t.name} (#${t.id})`;
    const reviewBanner = document.getElementById("review-banner");
    if (t.status === "in_review") {
        reviewBanner.textContent = `MODO REVISÃO — Classificado por ${t.classified_by_username || "?"}`;
        reviewBanner.classList.remove("hidden");
    } else {
        reviewBanner.classList.add("hidden");
    }
    if (preloadedMask) mask = preloadedMask;
    else await loadMaskFromServer(t.id);
    tryRestoreBackup();
    recountFilled();
    renderMaskFull();
    renderSatellite(t);
    renderMinimapContext(t);
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
    try {
        const raw = localStorage.getItem(LS_BACKUP_KEY);
        if (!raw) return;
        const b = JSON.parse(raw);
        if (b.tileId !== currentTile.id || !b.mask) return;
        const restored = Uint8Array.from(atob(b.mask), c => c.charCodeAt(0));
        if (restored.length !== PIXELS) return;
        if (confirm("Foi encontrado um backup local deste tile. Restaurar?")) {
            mask = restored;
        }
    } catch {}
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
}

// --- Rendering ---

function renderSatellite(t) {
    const bbox = [t.bbox_west, t.bbox_south, t.bbox_east, t.bbox_north];
    if (!satMap) {
        satMap = createLockedMap("map-satellite", tileserverUrl, bbox);
        satMap.on("error", (e) => {
            // Swallow tile errors (frequent on restricted networks)
            if (e && e.error) console.warn("map tile error", e.error.message);
        });
    } else {
        setMapBbox(satMap, bbox);
    }
    // Give MapLibre a moment to finish layout, then resize in case parent changed
    requestAnimationFrame(() => satMap && satMap.resize());
}

function renderMinimapContext(t) {
    minimapMap = renderMinimap(minimapMap, t, tileserverUrl);
}

function renderMaskFull() {
    writeMaskPixels(0, 0, TILE, TILE);
    blitMask();
}

function writeMaskPixels(x0, y0, x1, y1) {
    const data = maskImageData.data;
    for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
            const i = y * TILE + x;
            const v = mask[i];
            const lut = v * 4;
            const di = i * 4;
            data[di]     = colorLut[lut];
            data[di + 1] = colorLut[lut + 1];
            data[di + 2] = colorLut[lut + 2];
            data[di + 3] = colorLut[lut + 3];
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
        // Outline overlay iterates all 65k pixels; skip during gesture and
        // redraw once on mouseup to keep painting under 16ms/frame.
        if (!drawing) drawOutlines(true);
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
    const rect = canvasCursor.getBoundingClientRect();
    const sx = (ev.clientX - rect.left) / rect.width;
    const sy = (ev.clientY - rect.top) / rect.height;
    const x = Math.floor(sx * TILE);
    const y = Math.floor(sy * TILE);
    return [Math.max(0, Math.min(TILE-1, x)), Math.max(0, Math.min(TILE-1, y))];
}

function brushRadius() { return Math.floor(BRUSH_SIZES[brushSizeIdx] / 2); }

function paintAt(cx, cy) {
    const r = brushRadius();
    const value = tool === "eraser" ? 255 : activeClass;
    const x0 = Math.max(0, cx - r), x1 = Math.min(TILE, cx + r + 1);
    const y0 = Math.max(0, cy - r), y1 = Math.min(TILE, cy + r + 1);
    for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
            const i = y * TILE + x;
            const prev = mask[i];
            if (prev === value) continue;
            if (!gestureTouched.has(i)) gestureTouched.set(i, prev);
            if (prev === 255 && value !== 255) filledCount++;
            else if (prev !== 255 && value === 255) filledCount--;
            mask[i] = value;
        }
    }
    // Update dirty region into imageData and fully blit (cheap — 65k pixels).
    writeMaskPixels(x0, y0, x1, y1);
    blitMask();
}

function paintLine(x0, y0, x1, y1) {
    const dx = Math.abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    const dy = -Math.abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
    let err = dx + dy;
    let x = x0, y = y0;
    while (true) {
        paintAt(x, y);
        if (x === x1 && y === y1) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; x += sx; }
        if (e2 <= dx) { err += dx; y += sy; }
    }
}

function floodFill(cx, cy) {
    const target = mask[cy * TILE + cx];
    const replacement = activeClass;
    if (target === replacement) return;
    const touched = new Map();
    const stack = [[cx, cy]];
    while (stack.length) {
        const [x, y] = stack.pop();
        if (x < 0 || x >= TILE || y < 0 || y >= TILE) continue;
        const i = y * TILE + x;
        if (mask[i] !== target) continue;
        touched.set(i, target);
        if (target === 255) filledCount++;
        mask[i] = replacement;
        stack.push([x+1, y], [x-1, y], [x, y+1], [x, y-1]);
    }
    if (touched.size === 0) return;
    pushUndo(touched);
    renderMaskFull();
    updateProgress();
    saveBackup();
}

function pushUndo(touchedMap) {
    const n = touchedMap.size;
    const positions = new Uint32Array(n);
    const prevValues = new Uint8Array(n);
    let i = 0;
    for (const [pos, val] of touchedMap) { positions[i] = pos; prevValues[i] = val; i++; }
    undoStack.push({ positions, prevValues });
    if (undoStack.length > MAX_UNDO) undoStack.shift();
    redoStack.length = 0;
}

function applyPatch(entry) {
    const { positions, prevValues } = entry;
    const newPrev = new Uint8Array(prevValues.length);
    for (let i = 0; i < positions.length; i++) {
        const p = positions[i];
        newPrev[i] = mask[p];
        const v = prevValues[i];
        if (mask[p] === 255 && v !== 255) filledCount++;
        else if (mask[p] !== 255 && v === 255) filledCount--;
        mask[p] = v;
    }
    return { positions, prevValues: newPrev };
}

function undo() {
    if (!undoStack.length) return;
    const entry = undoStack.pop();
    redoStack.push(applyPatch(entry));
    renderMaskFull();
    updateProgress();
    saveBackup();
}

function redo() {
    if (!redoStack.length) return;
    const entry = redoStack.pop();
    undoStack.push(applyPatch(entry));
    renderMaskFull();
    updateProgress();
    saveBackup();
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
    canvasCursor.addEventListener("wheel", onWheel, { passive: false });

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

    document.getElementById("problem-cancel").addEventListener("click", closeProblemModal);
    document.getElementById("problem-confirm").addEventListener("click", confirmProblem);

    document.getElementById("btn-help").addEventListener("click", () => document.getElementById("modal-help").classList.remove("hidden"));
    document.getElementById("help-close").addEventListener("click", () => document.getElementById("modal-help").classList.add("hidden"));

    document.getElementById("btn-logout").addEventListener("click", () => {
        localStorage.removeItem("tileclass_tokens"); location.reload();
    });

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
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
    if (!ev.ctrlKey) return;
    ev.preventDefault();
    const delta = ev.deltaY < 0 ? 0.05 : -0.05;
    adjustOpacity(delta);
}

function drawCursor(x, y) {
    ctxCursor.clearRect(0, 0, DISPLAY, DISPLAY);
    const r = brushRadius();
    const sx = (x - r) * SCALE;
    const sy = (y - r) * SCALE;
    const sz = (2 * r + 1) * SCALE;
    ctxCursor.strokeStyle = "rgba(255,255,255,0.9)";
    ctxCursor.lineWidth = 1.5;
    ctxCursor.strokeRect(sx, sy, sz, sz);
    ctxCursor.strokeStyle = "rgba(0,0,0,0.6)";
    ctxCursor.lineWidth = 1;
    ctxCursor.strokeRect(sx+1, sy+1, sz-2, sz-2);
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
        if (ev.shiftKey) redo(); else undo();
        return;
    }
    if (ev.ctrlKey && (k === "y" || k === "Y")) { ev.preventDefault(); redo(); return; }
    if (ev.ctrlKey && (k === "s" || k === "S")) { ev.preventDefault(); submit(); return; }
    if (k >= "1" && k <= "6") {
        const idx = Number(k) - 1;
        if (classes[idx]) setActiveClass(classes[idx].id);
        return;
    }
    if (k === "b" || k === "B") { setTool("brush"); return; }
    if (k === "e" || k === "E") { setTool(tool === "eraser" ? "brush" : "eraser"); return; }
    if (k === "g" || k === "G") { setTool("fill"); return; }
    if (k === "+" || k === "=") { adjustBrushSize(1); return; }
    if (k === "-" || k === "_") { adjustBrushSize(-1); return; }
    if (k === "[") { adjustOpacity(-0.1); return; }
    if (k === "]") { adjustOpacity(0.1); return; }
    if (k === "o" || k === "O") { outlineOnly = !outlineOnly; blitMask(); return; }
    if (k === " ") { ev.preventDefault(); if (!maskHidden) { maskHidden = true; blitMask(); } return; }
    if (k === "?") { document.getElementById("modal-help").classList.remove("hidden"); return; }
}

function onKeyUp(ev) {
    if (ev.key === " ") { maskHidden = false; blitMask(); }
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

async function submit() {
    if (!currentTile) return;
    if (filledCount < PIXELS) {
        const missing = PIXELS - filledCount;
        showToast(`Faltam ${missing} pixels.`, "error");
        flashMissing();
        return;
    }
    try {
        await apiPostBytes(`/api/tiles/${currentTile.id}/classify`, mask);
        clearBackup();
        todayCount++;
        document.getElementById("today-count").textContent = todayCount;
        loadQueueStats();
        flashSuccess();
        setTimeout(loadNext, 220);
    } catch (e) {
        if (e.body?.detail?.error === "unfilled_pixels") {
            showToast(`Faltam ${e.body.detail.missing} pixels (servidor).`, "error");
        } else {
            showToast(`Erro: ${e.message}`, "error");
        }
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
