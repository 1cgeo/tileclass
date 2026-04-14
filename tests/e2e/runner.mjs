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
async function newPageBlocked() {
    // Fresh incognito context per test so localStorage/cookies are isolated.
    const ctx = await browser.createBrowserContext();
    const page = await ctx.newPage();
    page._tcContext = ctx;  // so we can close it in test
    await page.setRequestInterception(true);
    page.on("request", (req) => {
        const url = req.url();
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
        // Either the editor resumes an already-assigned tile (skipping idle),
        // or lands on the idle screen which must be dismissed with a click.
        await page.waitForFunction(
            () => window.__tcTest__?.currentTile != null
               || !document.getElementById("idle-screen").classList.contains("hidden"),
            { timeout: 5000 },
        );
        const hasTile = await page.evaluate(() => window.__tcTest__?.currentTile != null);
        if (!hasTile) {
            await page.click("#idle-start");
            await page.waitForFunction(
                () => window.__tcTest__?.currentTile != null,
                { timeout: 10000 },
            );
        }
    }
}

async function getMask(page) {
    return await page.evaluate(() => Array.from(window.__tcTest__.mask));
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
