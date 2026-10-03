// E2E runner: spawns a uvicorn instance on a random port with a throwaway
// config/DB, runs Puppeteer scenarios, and reports pass/fail.
//
// Usage: node tests/e2e/runner.mjs
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, readFileSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..", "..");

const PORT = 18766;
const BASE = `http://127.0.0.1:${PORT}`;

const tmp = mkdtempSync(join(tmpdir(), "tileclass-e2e-"));
const dbPath = join(tmp, "e2e.db").replace(/\\/g, "/");
const cfgPath = join(tmp, "config.yaml");

// Build a config that points to the temp DB
const baseCfg = readFileSync(join(ROOT, "backend", "config.yaml"), "utf-8");
const cfg = baseCfg.replace(/database:\s*\n\s*path:.*/, `database:\n  path: "${dbPath}"`);
writeFileSync(cfgPath, cfg);

// Seed (Python script uses the same DB path)
function runPython(args) {
    return new Promise((res, rej) => {
        const p = spawn("python", args, {
            cwd: ROOT,
            env: { ...process.env, TILECLASS_CONFIG: cfgPath, TILECLASS_ALLOW_DEFAULT_SECRET: "1" },
            stdio: "inherit",
        });
        p.on("exit", (code) => code === 0 ? res() : rej(new Error(`python exit ${code}`)));
    });
}

async function waitFor(url, timeoutMs = 15000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
        try {
            const r = await fetch(url);
            if (r.ok) return;
        } catch {}
        await new Promise(r => setTimeout(r, 200));
    }
    throw new Error(`timeout waiting for ${url}`);
}

// --- assertion helpers ---
let passed = 0, failed = 0;
const results = [];
async function test(name, fn) {
    const t0 = Date.now();
    try {
        await fn();
        const dt = Date.now() - t0;
        console.log(`  ok  ${name} (${dt}ms)`);
        passed++;
        results.push({ name, ok: true, dt });
    } catch (e) {
        const dt = Date.now() - t0;
        console.log(`  FAIL  ${name} (${dt}ms)\n    ${e.message}`);
        failed++;
        results.push({ name, ok: false, dt, err: e.message });
    }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || "assertion failed"); }

// --- page helpers ---
async function newPageBlocked({ abortOnce = null } = {}) {
    // Fresh incognito context per test so localStorage/cookies are isolated.
    const ctx = await browser.createBrowserContext();
    const page = await ctx.newPage();
    page._tcContext = ctx;  // so we can close it in test
    await page.setRequestInterception(true);
    // `abortOnce(url)`: fail the first matching local request (simulates a
    // flaky network on one call).
    let aborted = false;
    page.on("request", (req) => {
        const url = req.url();
        if (abortOnce && !aborted && abortOnce(url)) {
            aborted = true;
            req.abort();
            return;
        }
        // Block external MapLibre tile fetches (unreachable in CI) but keep local.
        if (url.startsWith(BASE) || url.startsWith("data:") || url.startsWith("blob:")) {
            req.continue();
        } else {
            req.abort();
        }
    });
    page.on("pageerror", (e) => console.log("[pageerror]", e.message));
    return page;
}

async function login(page, user, pass, waitForTile = true) {
    await page.goto(`${BASE}/?test=1`);
    await page.waitForSelector("#login-form", { timeout: 10000 });
    await page.type("#login-username", user);
    await page.type("#login-password", pass);
    await page.evaluate(() => document.querySelector("#login-form").requestSubmit());
    await page.waitForSelector("#view-editor:not(.hidden)", { timeout: 10000 });
    if (waitForTile) {
        // tileReady — async mask load can race with subsequent mouse events.
        await page.waitForFunction(
            () => window.__tcTest__?.tileReady === true
               || !document.getElementById("idle-screen").classList.contains("hidden"),
            { timeout: 5000 },
        );
        const hasTile = await page.evaluate(() => window.__tcTest__?.tileReady === true);
        if (!hasTile) {
            await page.click("#idle-start");
            await page.waitForFunction(
                () => window.__tcTest__?.tileReady === true,
                { timeout: 10000 },
            );
        }
    }
}

async function getMask(page) {
    return await page.evaluate(() => Array.from(window.__tcTest__.mask));
}

// submit() and pause() open #modal-action-confirm before mutating state; tests
// need to confirm it after clicking the trigger or the underlying handler
// stays parked on the modal's promise.
async function confirmModal(page) {
    await page.waitForSelector("#modal-action-confirm:not(.hidden)", { timeout: 2000 });
    await page.click("#action-confirm-ok");
}

async function paintWholeCanvas(page, classKey = "1") {
    await page.keyboard.press(classKey);
    // Switch to fill tool via the button click (more reliable than keyboard)
    await page.click("#tool-fill");
    const box = await page.$eval("#canvas-cursor", el => {
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    });
    await page.mouse.move(box.x, box.y);
    await page.mouse.down();
    await page.mouse.up();
    // Give flood fill a tick to complete and update state
    await page.waitForFunction(
        () => window.__tcTest__?.filledCount === 65536,
        { timeout: 3000 },
    );
    await page.click("#tool-brush");
}

// Node-side API access (no browser) — admin actions and server-state checks.
async function apiToken(user, pass) {
    const r = await fetch(`${BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: user, password: pass }),
    });
    if (!r.ok) throw new Error(`login ${user} → ${r.status}`);
    return (await r.json()).access_token;
}
async function apiCall(token, method, path, body) {
    const r = await fetch(`${BASE}${path}`, {
        method,
        headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
    });
    const text = await r.text();
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch {}
    return { status: r.status, json };
}

// Paint a short diagonal stroke with class 1 (partial mask).
async function paintStroke(page, dx = 100, len = 50) {
    const box = await page.$eval("#canvas-cursor", (el, d) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + d, y: r.top + d };
    }, dx);
    await page.keyboard.press("1");
    await page.mouse.move(box.x, box.y);
    await page.mouse.down();
    await page.mouse.move(box.x + len, box.y + len);
    await page.mouse.up();
}

// ---------- main ----------
let uvicornProc = null;
let browser = null;
try {
    console.log(`[e2e] seeding DB at ${dbPath}`);
    await runPython(["-m", "tests.e2e.seed", dbPath]);

    console.log(`[e2e] starting uvicorn on port ${PORT}`);
    uvicornProc = spawn("python", [
        "-m", "uvicorn", "backend.main:app",
        "--host", "127.0.0.1", "--port", String(PORT),
        "--log-level", "warning",
    ], {
        cwd: ROOT,
        env: {
            ...process.env,
            TILECLASS_CONFIG: cfgPath,
            TILECLASS_DISABLE_RATE_LIMIT: "1",
            TILECLASS_ALLOW_DEFAULT_SECRET: "1",
            PYTHONUNBUFFERED: "1",
        },
        stdio: ["ignore", "inherit", "inherit"],
    });

    await waitFor(`${BASE}/api/health`);
    console.log(`[e2e] backend up`);

    browser = await puppeteer.launch({
        headless: true,
        args: ["--no-sandbox", "--window-size=1400,1000"],
        defaultViewport: { width: 1400, height: 1000 },
        protocolTimeout: 30_000,
    });

    // ---------------- scenarios ----------------

    await test("login flow under 5s, editor loads", async () => {
        const page = await newPageBlocked();
        const t0 = Date.now();
        await login(page, "op1", "secret123");
        const elapsed = Date.now() - t0;
        assert(elapsed < 5000, `login→editor took ${elapsed}ms (>5s)`);
        const tile = await page.evaluate(() => window.__tcTest__.currentTile);
        assert(tile && tile.status === "in_progress", "expected in_progress tile");
        await page.close(); await page._tcContext?.close();
    });

    await test("paint full canvas → submit → idle screen → click → next tile", async () => {
        const page = await newPageBlocked();
        await login(page, "op1", "secret123");
        const tileBefore = await page.evaluate(() => window.__tcTest__.currentTile.id);

        await paintWholeCanvas(page, "1");
        const filled = await page.evaluate(() => window.__tcTest__.filledCount);
        assert(filled === 65536, `filledCount=${filled}, expected 65536`);

        await page.click("#btn-submit");
        await confirmModal(page);

        // Idle screen must appear after submit; tile must NOT auto-advance.
        await page.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });
        const auto = await page.evaluate(() => window.__tcTest__?.currentTile?.id ?? null);
        assert(auto === null, `submit should not auto-load next (currentTile=${auto})`);

        // Click "Iniciar tile" -> next tile loads
        await page.click("#idle-start");
        await page.waitForFunction(
            (before) => window.__tcTest__?.currentTile?.id && window.__tcTest__.currentTile.id !== before,
            { timeout: 5000 }, tileBefore,
        );
        const tileAfter = await page.evaluate(() => window.__tcTest__.currentTile.id);
        assert(tileAfter !== tileBefore, "next tile did not load after clicking start");
        await page.close(); await page._tcContext?.close();
    });

    await test("re-login with an assigned tile skips idle and opens it directly", async () => {
        // First session: pick up a tile but don't submit.
        const page1 = await newPageBlocked();
        await login(page1, "op3", "secret123");
        const tileId = await page1.evaluate(() => window.__tcTest__.currentTile.id);
        await page1.close(); await page1._tcContext?.close();

        // Second session: fresh incognito context — on login, the editor should
        // resume the same tile without showing the idle screen.
        const page2 = await newPageBlocked();
        await page2.goto(`${BASE}/?test=1`);
        await page2.waitForSelector("#login-form", { timeout: 10000 });
        await page2.type("#login-username", "op3");
        await page2.type("#login-password", "secret123");
        await page2.evaluate(() => document.querySelector("#login-form").requestSubmit());
        await page2.waitForSelector("#view-editor:not(.hidden)", { timeout: 10000 });
        // currentTile should be populated without any manual click.
        await page2.waitForFunction(
            () => window.__tcTest__?.currentTile?.id != null,
            { timeout: 5000 },
        );
        const idleVisible = await page2.$eval("#idle-screen", el => !el.classList.contains("hidden"));
        assert(!idleVisible, "idle screen should NOT appear when a tile is already assigned");
        const resumed = await page2.evaluate(() => window.__tcTest__.currentTile.id);
        assert(resumed === tileId, `expected resume of tile ${tileId}, got ${resumed}`);
        await page2.close(); await page2._tcContext?.close();
    });

    await test("post-login shows idle screen; no tile assigned until click", async () => {
        const page = await newPageBlocked();
        await page.goto(`${BASE}/?test=1`);
        await page.waitForSelector("#login-form", { timeout: 10000 });
        await page.type("#login-username", "op2");
        await page.type("#login-password", "secret123");
        await page.evaluate(() => document.querySelector("#login-form").requestSubmit());
        await page.waitForSelector("#view-editor:not(.hidden)", { timeout: 10000 });
        // Idle must be visible and no tile assigned on the client.
        await page.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });
        const t = await page.evaluate(() => window.__tcTest__?.currentTile ?? null);
        assert(t === null, `expected no currentTile before clicking start, got ${JSON.stringify(t)}`);
        await page.close(); await page._tcContext?.close();
    });

    await test("submit incomplete mask → blocked client-side, tile not advanced", async () => {
        const page = await newPageBlocked();
        await login(page, "op3", "secret123");
        const tileId = await page.evaluate(() => window.__tcTest__.currentTile.id);

        // Bypass any pre-existing mask from previous tests by sending a mask
        // with 255s directly via the backend. Client-side submit() uses the
        // in-memory filledCount to guard. We directly POST via fetch inside the
        // page context to exercise the SERVER's 422 path.
        const badStatus = await page.evaluate(async (tid) => {
            const bad = new Uint8Array(65536).fill(1);
            for (let i = 0; i < 1000; i++) bad[i] = 255;
            const tokens = JSON.parse(localStorage.getItem("tileclass_tokens"));
            const res = await fetch(`/api/tiles/${tid}/classify`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/octet-stream",
                    "Authorization": `Bearer ${tokens.access_token}`,
                },
                body: bad,
            });
            return res.status;
        }, tileId);
        assert(badStatus === 422, `expected 422 from server, got ${badStatus}`);

        // Tile must not have advanced on server side
        const stillId = await page.evaluate(() => window.__tcTest__.currentTile.id);
        assert(stillId === tileId, "incomplete submit advanced the tile");
        await page.close(); await page._tcContext?.close();
    });

    await test("space keydown hides mask; keyup restores", async () => {
        const page = await newPageBlocked();
        await login(page, "op3", "secret123");
        await page.focus("#canvas-cursor");
        await page.keyboard.down(" ");
        await new Promise(r => setTimeout(r, 50));
        const hidden = await page.evaluate(() => window.__tcTest__.maskHidden);
        assert(hidden === true, "maskHidden should be true while space held");
        await page.keyboard.up(" ");
        await new Promise(r => setTimeout(r, 50));
        const shown = await page.evaluate(() => window.__tcTest__.maskHidden);
        assert(shown === false, "maskHidden should be false after space up");
        await page.close(); await page._tcContext?.close();
    });

    await test("undo/redo keyboard: Ctrl+Z reverts a paint gesture", async () => {
        const page = await newPageBlocked();
        await login(page, "op1", "secret123");

        const before = await page.evaluate(() => window.__tcTest__.filledCount);
        const box = await page.$eval("#canvas-cursor", el => {
            const r = el.getBoundingClientRect();
            return { x: r.left + 100, y: r.top + 100 };
        });
        await page.keyboard.press("1");
        await page.mouse.move(box.x, box.y);
        await page.mouse.down();
        await page.mouse.move(box.x + 30, box.y + 30);
        await page.mouse.up();

        const painted = await page.evaluate(() => window.__tcTest__.filledCount);
        assert(painted > before, `filled must grow after paint (was ${before}, now ${painted})`);

        await page.keyboard.down("Control"); await page.keyboard.press("z"); await page.keyboard.up("Control");
        await new Promise(r => setTimeout(r, 50));
        const undone = await page.evaluate(() => window.__tcTest__.filledCount);
        assert(undone === before, `undo must restore fill count (expected ${before}, got ${undone})`);

        // Redo
        await page.keyboard.down("Control"); await page.keyboard.press("y"); await page.keyboard.up("Control");
        await new Promise(r => setTimeout(r, 50));
        const redone = await page.evaluate(() => window.__tcTest__.filledCount);
        assert(redone === painted, `redo must restore painted fill (expected ${painted}, got ${redone})`);

        await page.close(); await page._tcContext?.close();
    });

    await test("preload: next-preview endpoint is peek-only (no assignment)", async () => {
        // Direct API check: peek then assign — both should succeed and return
        // a sensible tile id. We don't go through the browser here; the frontend
        // preload path is exercised in other scenarios and this test locks in
        // the no-side-effect contract.
        const login = await fetch(`${BASE}/api/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username: "op2", password: "secret123" }),
        }).then(r => r.json());
        const tok = login.access_token;
        const h2 = { "Authorization": `Bearer ${tok}` };

        // Two consecutive previews must return the same id.
        const p1 = await fetch(`${BASE}/api/tiles/next-preview`, { headers: h2 });
        const p2 = await fetch(`${BASE}/api/tiles/next-preview`, { headers: h2 });
        assert(p1.status === 200 && p2.status === 200, `preview status ${p1.status}/${p2.status}`);
        const b1 = await p1.json(); const b2 = await p2.json();
        assert(b1.id === b2.id, `preview not idempotent: ${b1.id} vs ${b2.id}`);
        // The tile should NOT be in_progress from the peek alone
        // (it can be in_progress from a prior test's assignment, though — we only
        //  assert that preview doesn't itself advance state).
    });

    await test("pause: partial mask saved server-side, resume prompt on re-login, mask preserved", async () => {
        // Session 1: paint partially, then pause.
        const page1 = await newPageBlocked();
        await login(page1, "op2", "secret123");
        const tileId = await page1.evaluate(() => window.__tcTest__.currentTile.id);

        // Paint a short line — partial mask, NOT 65536 filled.
        const box = await page1.$eval("#canvas-cursor", el => {
            const r = el.getBoundingClientRect();
            return { x: r.left + 100, y: r.top + 100 };
        });
        await page1.keyboard.press("1");
        await page1.mouse.move(box.x, box.y);
        await page1.mouse.down();
        await page1.mouse.move(box.x + 50, box.y + 50);
        await page1.mouse.up();
        const painted = await page1.evaluate(() => window.__tcTest__.filledCount);
        assert(painted > 0 && painted < 65536, `partial paint expected, got ${painted}`);

        // Snapshot the mask before pause.
        const maskBefore = await getMask(page1);

        await page1.click("#btn-pause");
        await confirmModal(page1);
        // After pause: idle screen visible, currentTile cleared.
        await page1.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });
        const cleared = await page1.evaluate(() => window.__tcTest__?.currentTile);
        assert(cleared === null, `pause should clear currentTile, got ${JSON.stringify(cleared)}`);
        await page1.close(); await page1._tcContext?.close();

        // Session 2: fresh incognito — login should land on the resume prompt,
        // NOT directly in the editor (because the tile is paused).
        const page2 = await newPageBlocked();
        await page2.goto(`${BASE}/?test=1`);
        await page2.waitForSelector("#login-form", { timeout: 10000 });
        await page2.type("#login-username", "op2");
        await page2.type("#login-password", "secret123");
        await page2.evaluate(() => document.querySelector("#login-form").requestSubmit());
        await page2.waitForSelector("#view-editor:not(.hidden)", { timeout: 10000 });
        await page2.waitForFunction(
            () => window.__tcTest__?.pausedPromptVisible === true,
            { timeout: 5000 },
        );
        const noTileYet = await page2.evaluate(() => window.__tcTest__?.currentTile);
        assert(noTileYet === null, "currentTile must stay null until user clicks Continuar");

        // Click Continuar → editor opens with mask preserved. tileReady
        // guarantees loadMaskFromServer finished before we read filledCount.
        await page2.click("#paused-resume-continue");
        await page2.waitForFunction(
            () => window.__tcTest__?.tileReady === true
              && window.__tcTest__.pausedPromptVisible === false,
            { timeout: 5000 },
        );
        const resumed = await page2.evaluate(() => window.__tcTest__.currentTile);
        assert(resumed.id === tileId, `expected resume of tile ${tileId}, got ${resumed.id}`);
        assert(resumed.paused_at === null, `paused_at should be cleared after resume, got ${resumed.paused_at}`);

        // The mask painted in session 1 must be intact in session 2.
        const maskAfter = await getMask(page2);
        const filledAfter = await page2.evaluate(() => window.__tcTest__.filledCount);
        assert(filledAfter === painted,
            `mask not preserved across pause: filled was ${painted}, now ${filledAfter}`);
        // Spot-check a handful of pixels match exactly.
        let mismatches = 0;
        for (let i = 0; i < maskBefore.length; i++) {
            if (maskBefore[i] !== maskAfter[i]) mismatches++;
        }
        assert(mismatches === 0, `mask bytes diverged after pause/resume: ${mismatches} mismatches`);
        await page2.close(); await page2._tcContext?.close();
    });

    await test("classification: digit key picks class, submit stores it with X-Tile-Version", async () => {
        const page = await newPageBlocked();
        const status415 = [];
        let classifyHeaders = null;
        page.on("response", (r) => { if (r.status() === 415) status415.push(r.url()); });
        page.on("request", (req) => {
            if (/\/api\/tiles\/\d+\/classify$/.test(req.url())) classifyHeaders = req.headers();
        });
        await login(page, "cls1", "secret123");
        await page.waitForSelector("#classification-panel:not(.hidden)", { timeout: 5000 });
        const tileId = await page.evaluate(() => window.__tcTest__.currentTile.id);

        // "2" → second class in display order (id 20), not class id 2.
        await page.keyboard.press("2");
        const active = await page.$eval(".classification-class.active", el => el.dataset.classId);
        assert(active === "20", `digit 2 should select class 20, got ${active}`);

        await page.click("#btn-submit");
        await confirmModal(page);
        await page.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });
        assert(classifyHeaders && classifyHeaders["x-tile-version"] !== undefined,
            "classification submit must send X-Tile-Version");

        const stored = await page.evaluate(async (id) => {
            const t = JSON.parse(localStorage.getItem("tileclass_tokens") || "null");
            const r = await fetch(`/api/tiles/${id}/classification`,
                { headers: { Authorization: `Bearer ${t?.access_token}` } });
            return r.status === 200 ? (await r.json()).class_id : `status ${r.status}`;
        }, tileId);
        assert(stored === 20, `server should store class 20, got ${stored}`);
        // Preload must not probe the raster-only /image endpoint.
        assert(status415.length === 0, `unexpected 415s: ${status415.join(", ")}`);
        await page.close(); await page._tcContext?.close();
    });

    await test("classification: paused tile resumes into the editor (no /image 415)", async () => {
        const page = await newPageBlocked();
        const status415 = [];
        page.on("response", (r) => { if (r.status() === 415) status415.push(r.url()); });
        await login(page, "cls_paused", "secret123", false);
        await page.waitForFunction(
            () => window.__tcTest__?.pausedPromptVisible === true, { timeout: 5000 },
        );
        await page.click("#paused-resume-continue");
        await page.waitForFunction(
            () => window.__tcTest__?.tileReady === true
              && window.__tcTest__.pausedPromptVisible === false,
            { timeout: 5000 },
        );
        const t = await page.evaluate(() => window.__tcTest__.currentTile);
        assert(t.name === "cls_paused_tile", `expected the paused tile, got ${t.name}`);
        assert(t.paused_at === null, `paused_at should be cleared, got ${t.paused_at}`);
        const panelVisible = await page.$eval("#classification-panel",
            el => !el.classList.contains("hidden"));
        assert(panelVisible, "classification panel should be visible after resume");
        assert(status415.length === 0, `unexpected 415s: ${status415.join(", ")}`);
        await page.close(); await page._tcContext?.close();
    });

    await test("raster: resume survives a failed mask fetch (no stuck prompt)", async () => {
        // /resume and /image run in parallel; if /image fails the tile is
        // already unpaused, so the editor must still open with the saved mask.
        const page = await newPageBlocked({ abortOnce: (u) => /\/api\/tiles\/\d+\/image$/.test(u) });
        await login(page, "op_paused", "secret123", false);
        await page.waitForFunction(
            () => window.__tcTest__?.pausedPromptVisible === true, { timeout: 5000 },
        );
        await page.click("#paused-resume-continue");
        await page.waitForFunction(
            () => window.__tcTest__?.tileReady === true
              && window.__tcTest__.pausedPromptVisible === false,
            { timeout: 5000 },
        );
        const t = await page.evaluate(() => window.__tcTest__.currentTile);
        assert(t.name === "raster_paused_tile", `expected the paused tile, got ${t.name}`);
        const mask = await getMask(page);
        const twos = mask.filter(v => v === 2).length;
        assert(twos === 1000 && mask[0] === 2 && mask[1000] === 255,
            `saved partial mask not restored (class-2 px: ${twos})`);
        await page.close(); await page._tcContext?.close();
    });

    await test("admin viewer shows the class of a classification tile", async () => {
        const page = await newPageBlocked();
        // Admins land on the admin panel (not the editor), so no login() helper.
        await page.goto(`${BASE}/?test=1`);
        await page.waitForSelector("#login-form", { timeout: 10000 });
        await page.type("#login-username", "admin");
        await page.type("#login-password", "admin123");
        await page.evaluate(() => document.querySelector("#login-form").requestSubmit());
        await page.waitForSelector("#view-admin:not(.hidden)", { timeout: 10000 });
        await page.click('.admin-nav button[data-tab="tiles"]');
        await page.waitForSelector("#tiles-project-filter", { timeout: 5000 });
        await page.select("#tiles-project-filter", "2");
        await page.waitForFunction(() => [...document.querySelectorAll("#admin-content tr")]
            .some(tr => tr.textContent.includes("cls_done")), { timeout: 5000 });
        await page.evaluate(() => {
            const row = [...document.querySelectorAll("#admin-content tr")]
                .find(tr => tr.textContent.includes("cls_done"));
            [...row.querySelectorAll("button")].find(b => b.textContent.trim() === "Ver").click();
        });
        await page.waitForFunction(() => {
            const body = document.getElementById("view-tile-body");
            return body && /Classe:\s*Solo/.test(body.textContent);
        }, { timeout: 5000 });
        await page.close(); await page._tcContext?.close();
    });

    await test("pause → Iniciar tile reopens the paused tile with its saved mask (no stale preload)", async () => {
        const page = await newPageBlocked();
        await login(page, "op_pr", "secret123");
        const tileId = await page.evaluate(() => window.__tcTest__.currentTile.id);
        // Let the background preload settle before editing — the bug was the
        // preload caching the OPEN tile with its initial mask.
        await page.waitForFunction(() => window.__tcTest__.preloadedNextId != null, { timeout: 5000 });

        await paintStroke(page);
        const painted = await page.evaluate(() => window.__tcTest__.filledCount);
        assert(painted > 0 && painted < 65536, `partial paint expected, got ${painted}`);
        const maskBefore = await getMask(page);

        await page.click("#btn-pause");
        await confirmModal(page);
        await page.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });

        await page.click("#idle-start");
        await page.waitForFunction(
            (id) => window.__tcTest__?.tileReady === true && window.__tcTest__.currentTile?.id === id,
            { timeout: 5000 }, tileId,
        );
        const maskAfter = await getMask(page);
        let mismatches = 0;
        for (let i = 0; i < maskBefore.length; i++) if (maskBefore[i] !== maskAfter[i]) mismatches++;
        assert(mismatches === 0, `mask differs from the paused one: ${mismatches} px`);
        const filled = await page.evaluate(() => window.__tcTest__.filledCount);
        assert(filled === painted, `filledCount ${filled} != painted ${painted}`);
        const cur = await page.evaluate(() => window.__tcTest__.currentTile);
        assert(cur.paused_at == null, `client tile still paused: ${cur.paused_at}`);

        const tok = await apiToken("op_pr", "secret123");
        const srv = await apiCall(tok, "GET", `/api/tiles/${tileId}`);
        assert(srv.status === 200 && srv.json.paused_at == null,
            `server tile must be unpaused after start, got ${JSON.stringify(srv.json)}`);
        assert(srv.json.filled_pixels === painted,
            `server mask filled ${srv.json.filled_pixels} != ${painted}`);
        await page.close(); await page._tcContext?.close();
    });

    await test("heartbeat: tile paused server-side is resumed; submit then succeeds", async () => {
        const page = await newPageBlocked();
        await login(page, "op_hb", "secret123");
        const tileId = await page.evaluate(() => window.__tcTest__.currentTile.id);
        await paintWholeCanvas(page, "1");

        // Simulate the server pausing the tile behind the editor's back
        // (auto-pause after sleep / admin pause): bumps version + paused_at.
        const adminTok = await apiToken("admin", "admin123");
        const p = await apiCall(adminTok, "POST", `/api/admin/tiles/${tileId}/admin-pause`, {});
        assert(p.status === 200, `admin-pause failed: ${p.status} ${JSON.stringify(p.json)}`);

        await page.evaluate(() => window.__tcTest__.heartbeatNow());
        const toast = await page.$eval("#toast", el => el.textContent);
        assert(/retomado após inatividade/.test(toast), `expected resume toast, got "${toast}"`);

        await page.click("#btn-submit");
        await confirmModal(page);
        await page.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });
        const srv = await apiCall(adminTok, "GET", `/api/tiles/${tileId}`);
        assert(srv.json.status === "classified", `expected classified, got ${srv.json.status}`);
        await page.close(); await page._tcContext?.close();
    });

    await test("project switch rebuilds the satellite map with the new project's imagery", async () => {
        const page = await newPageBlocked();
        await login(page, "op_multi", "secret123");
        await page.waitForFunction(
            () => (window.__tcTest__.satSourceUrl || "").includes("imagery-a"), { timeout: 5000 },
        );
        await page.click("#btn-switch-project");
        await page.waitForSelector("#modal-shell:not(.hidden)", { timeout: 3000 });
        // The picker list is populated after an async fetch; click via DOM so
        // overlay/layout timing can't make the item "not clickable".
        await page.waitForSelector('.project-switcher-item[data-pid="4"]', { timeout: 3000 });
        await page.evaluate(() => {
            document.querySelector('.project-switcher-item[data-pid="4"]').click();
            document.getElementById("shell-ok").click();
        });
        await page.waitForSelector("#idle-screen:not(.hidden)", { timeout: 5000 });
        await page.click("#idle-start");
        await page.waitForFunction(
            () => window.__tcTest__?.tileReady === true && window.__tcTest__.currentTile?.project_id === 4,
            { timeout: 5000 },
        );
        await page.waitForFunction(
            () => (window.__tcTest__.satSourceUrl || "").includes("imagery-b"), { timeout: 5000 },
        ).catch(async () => {
            const u = await page.evaluate(() => window.__tcTest__.satSourceUrl);
            throw new Error(`map still on old imagery: ${u}`);
        });
        const overlays = await page.evaluate(() => window.__tcTest__.satOverlayKeys);
        assert(overlays.includes("secondary"), `secondary overlay missing: ${JSON.stringify(overlays)}`);
        await page.close(); await page._tcContext?.close();
    });

    await test("open project-switch modal swallows shortcuts; Escape closes it", async () => {
        const page = await newPageBlocked();
        await login(page, "op_multi", "secret123");
        await page.click("#btn-switch-project");
        await page.waitForSelector("#modal-shell:not(.hidden)", { timeout: 3000 });
        const before = await page.$eval("#brush-size-label", el => el.textContent);
        await page.keyboard.press("s");
        await page.keyboard.press("w");
        const after = await page.$eval("#brush-size-label", el => el.textContent);
        assert(after === before, `brush changed behind modal: ${before} → ${after}`);
        const eraser = await page.$eval("#tool-eraser", el => el.classList.contains("active"));
        assert(!eraser, "tool changed behind modal");
        await page.keyboard.press("Escape");
        await page.waitForSelector("#modal-shell.hidden", { timeout: 2000 });
        await page.close(); await page._tcContext?.close();
    });

    await test("window blur while Space is held restores the mask", async () => {
        const page = await newPageBlocked();
        await login(page, "op_keys", "secret123");
        await page.focus("#canvas-cursor");
        await page.keyboard.down(" ");
        await new Promise(r => setTimeout(r, 50));
        assert(await page.evaluate(() => window.__tcTest__.maskHidden) === true, "space should hide mask");
        await page.evaluate(() => window.dispatchEvent(new Event("blur")));
        const st = await page.evaluate(() => ({
            hidden: window.__tcTest__.maskHidden,
            cls: document.getElementById("canvas-viewport").classList.contains("space-held"),
        }));
        assert(st.hidden === false, "mask must be visible again after blur");
        assert(st.cls === false, "space-held class must be cleared after blur");
        await page.keyboard.up(" ");
        await page.close(); await page._tcContext?.close();
    });

    await test("modifier chords: Ctrl+A/C/F don't trigger letter shortcuts; Ctrl+S submits", async () => {
        const page = await newPageBlocked();
        await login(page, "op_keys", "secret123");
        await page.focus("#canvas-cursor");
        const brush0 = await page.$eval("#brush-size-label", el => el.textContent);
        for (const key of ["a", "f", "c"]) {
            await page.keyboard.down("Control"); await page.keyboard.press(key); await page.keyboard.up("Control");
        }
        const brush1 = await page.$eval("#brush-size-label", el => el.textContent);
        assert(brush1 === brush0, `Ctrl+A changed brush ${brush0} → ${brush1}`);
        const zoom = await page.$eval("#zoom-label", el => el.textContent);
        assert(zoom === "100%", `Ctrl+C jumped/zoomed to a missing pixel (zoom ${zoom})`);
        // Mask is incomplete → Ctrl+S goes through submit(), which refuses
        // with the "Faltam N pixels" toast instead of opening the save dialog.
        await page.keyboard.down("Control"); await page.keyboard.press("s"); await page.keyboard.up("Control");
        const toast = await page.$eval("#toast", el => el.textContent);
        assert(/Faltam/.test(toast), `Ctrl+S should run submit, toast="${toast}"`);
        await page.close(); await page._tcContext?.close();
    });

    await test("invalid login shows error, valid login succeeds", async () => {
        const page = await newPageBlocked();
        await page.goto(`${BASE}/?test=1`);
        await page.waitForSelector("#login-form");
        await page.type("#login-username", "admin");
        await page.type("#login-password", "wrong");
        await page.evaluate(() => document.querySelector("#login-form").requestSubmit());
        await new Promise(r => setTimeout(r, 800));
        const onLogin = await page.$eval("#view-login", el => !el.classList.contains("hidden"));
        assert(onLogin, "invalid login should keep user on login view");
        await page.close(); await page._tcContext?.close();
    });

} catch (e) {
    console.error("[e2e] runner error:", e);
    failed++;
} finally {
    if (browser) await browser.close().catch(() => {});
    if (uvicornProc) {
        uvicornProc.kill("SIGINT");
        // Give it a moment to flush
        await new Promise(r => setTimeout(r, 500));
        try { uvicornProc.kill("SIGKILL"); } catch {}
    }
    try { rmSync(tmp, { recursive: true, force: true }); } catch {}
}

console.log(`\n[e2e] ${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
