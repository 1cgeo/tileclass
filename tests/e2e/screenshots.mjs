// Screenshot tour of every screen, for UI review (not part of the test run).
// Spins up a throwaway backend (seeded temp DB, port 18767) and saves PNGs in
// light and dark color schemes.
//
// Usage:
//   node tests/e2e/screenshots.mjs <out_dir> [--themes light,dark] [--demo]
//
//   --demo   seed with tests/e2e/seed_demo.py (realistic team, ~30 days of
//            history, synthetic imagery .mbtiles) instead of the E2E seed.
//            Generated files live in a temp dir that is removed at the end.
//
// Also importable: `import { runScreenshots } from "./screenshots.mjs"`.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, readFileSync, mkdirSync, existsSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer from "puppeteer";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..", "..");
const PORT = 18767;
const BASE = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Prefer the project venv so the tool works without activating it.
function pythonExe() {
    const venv = process.platform === "win32"
        ? join(ROOT, ".venv", "Scripts", "python.exe")
        : join(ROOT, ".venv", "bin", "python");
    return existsSync(venv) ? venv : "python";
}

// Logins per seed. The demo seed mirrors tests/e2e/seed_demo.py DEMO_USERS.
const ACCOUNTS = {
    default: { admin: ["admin", "admin123"], raster: ["op1", "secret123"], classification: ["cls1", "secret123"],
               project: "default" },
    demo: { admin: ["admin", "admin123"], raster: ["diego.rocha", "secret123"], classification: null,
            project: "Cobertura do solo — Demo" },
};

export async function runScreenshots({ outDir, themes = ["light", "dark"], demo = false } = {}) {
    outDir = resolve(outDir || "screenshots");
    mkdirSync(outDir, { recursive: true });
    const acc = demo ? ACCOUNTS.demo : ACCOUNTS.default;
    const tmp = mkdtempSync(join(tmpdir(), "tileclass-shots-"));
    const dbPath = join(tmp, "shots.db").replace(/\\/g, "/");
    const cachePath = join(tmp, "overlay_cache.mbtiles").replace(/\\/g, "/");
    const cfgPath = join(tmp, "config.yaml");
    const baseCfg = readFileSync(join(ROOT, "backend", "config.yaml"), "utf-8");
    writeFileSync(cfgPath, baseCfg
        .replace(/database:\s*\n\s*path:.*/, `database:\n  path: "${dbPath}"`)
        // Keep the admin map overlay cache out of the repo.
        .replace(/cache_path:.*/, `cache_path: "${cachePath}"`));
    const env = { ...process.env, TILECLASS_CONFIG: cfgPath, TILECLASS_ALLOW_DEFAULT_SECRET: "1",
                  TILECLASS_DISABLE_RATE_LIMIT: "1", PYTHONUNBUFFERED: "1" };
    const py = pythonExe();
    const run = (args) => new Promise((res, rej) => {
        const p = spawn(py, args, { cwd: ROOT, env, stdio: "inherit" });
        p.on("exit", (c) => c === 0 ? res() : rej(new Error(`python exit ${c}`)));
    });
    const waitFor = async (url) => {
        for (let i = 0; i < 100; i++) {
            try { if ((await fetch(url)).ok) return; } catch {}
            await sleep(200);
        }
        throw new Error("backend did not start");
    };

    if (demo) await run(["-m", "tests.e2e.seed_demo", dbPath, tmp]);
    else await run(["-m", "tests.e2e.seed", dbPath]);
    const server = spawn(py, ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                              "--port", String(PORT), "--log-level", "warning"],
                         { cwd: ROOT, env, stdio: ["ignore", "inherit", "inherit"] });
    let browser;
    const saved = [];
    try {
        await waitFor(`${BASE}/api/health`);
        browser = await puppeteer.launch({ headless: true, args: ["--no-sandbox"],
                                           defaultViewport: { width: 1440, height: 900 } });

        const newPage = async (theme) => {
            const ctx = await browser.createBrowserContext();
            const p = await ctx.newPage();
            await p.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
            await p.setRequestInterception(true);
            p.on("request", (r) => (r.url().startsWith(BASE) || r.url().startsWith("data:")
                                    || r.url().startsWith("blob:")) ? r.continue() : r.abort());
            p.on("pageerror", (e) => console.log("[pageerror]", e.message));
            p._ctx = ctx;
            return p;
        };
        const close = async (p) => { await p.close(); await p._ctx.close(); };
        const login = async (p, [user, pass]) => {
            await p.goto(`${BASE}/`);
            await p.waitForSelector("#login-form");
            await p.type("#login-username", user);
            await p.type("#login-password", pass);
            await p.evaluate(() => document.querySelector("#login-form").requestSubmit());
        };
        // The idle screen's start button exists in the DOM even when hidden.
        const startTileIfIdle = async (p) => {
            await sleep(500);
            const clicked = await p.evaluate(() => {
                const b = document.getElementById("idle-start");
                if (!b || b.offsetParent === null) return false;
                b.click();
                return true;
            });
            if (clicked) await sleep(1200);
        };
        const shot = async (p, name, { wait = 700, full = false } = {}) => {
            await sleep(wait);
            const file = join(outDir, `${name}.png`);
            if (full) {
                // The admin content scrolls inside #admin-content: grow the
                // viewport to its scroll height for a full-page capture.
                const h = await p.evaluate(() => {
                    const c = document.getElementById("admin-content");
                    return c ? c.scrollHeight + c.getBoundingClientRect().top : document.body.scrollHeight;
                });
                await p.setViewport({ width: 1440, height: Math.min(Math.max(900, Math.ceil(h)), 4000) });
                await sleep(500);
                await p.screenshot({ path: file });
                await p.setViewport({ width: 1440, height: 900 });
            } else {
                await p.screenshot({ path: file });
            }
            saved.push(file);
            console.log("saved", name);
        };
        const tab = async (p, name) => {
            await p.click(`.admin-nav button[data-tab="${name}"]`);
            await p.waitForFunction(() => !document.querySelector("#admin-content > .tab-view > .loading-text"), { timeout: 15000 });
        };
        const selectScope = async (p, tabName, label) => {
            await p.evaluate((tabName, label) => {
                const sel = document.getElementById(`${tabName}-project-filter`);
                const opt = [...sel.options].find(o => (label === null ? o.value === "" : o.textContent === label));
                if (opt && sel.value !== opt.value) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event("change"));
                }
            }, tabName, label);
            await sleep(400);
            await p.waitForFunction(() => !document.querySelector("#admin-content > .tab-view > .loading-text"), { timeout: 15000 });
        };

        for (const theme of themes) {
            const t = (n) => `${theme}-${n}`;
            // Login + operator editors
            let p = await newPage(theme);
            await p.goto(`${BASE}/`);
            await p.waitForSelector("#login-form");
            await shot(p, t("01-login"));
            await login(p, acc.raster);
            await p.waitForSelector("#view-editor:not(.hidden)");
            await shot(p, t("02-editor-idle"));
            await startTileIfIdle(p);
            await shot(p, t("03-editor-raster"), { wait: 1500 });
            await close(p);
            if (acc.classification) {
                p = await newPage(theme);
                await login(p, acc.classification);
                await p.waitForSelector("#view-editor:not(.hidden)");
                await startTileIfIdle(p);
                await shot(p, t("04-editor-classification"));
                await close(p);
            }

            // Admin
            p = await newPage(theme);
            await p.evaluateOnNewDocument(() => {
                try { localStorage.setItem("tc_admin_map_sat_on", "1"); } catch {}
            });
            await login(p, acc.admin);
            await p.waitForSelector("#view-admin:not(.hidden)");
            await p.waitForFunction(() => !document.querySelector("#admin-content > .tab-view > .loading-text"), { timeout: 15000 });
            await shot(p, t("10-admin-dashboard"), { wait: 1000 });
            await shot(p, t("10b-admin-dashboard-full"), { full: true });
            await selectScope(p, "dashboard", null);
            await shot(p, t("11-admin-dashboard-all"), { full: true });
            await selectScope(p, "dashboard", acc.project);

            await tab(p, "projects");
            await shot(p, t("12-admin-projects"));
            await p.click(`tr[aria-label="Abrir projeto ${acc.project}"]`);
            await p.waitForSelector("#export-remap", { timeout: 10000 });
            await shot(p, t("13-admin-project-detail"), { full: true });

            await tab(p, "tiles");
            await shot(p, t("14-admin-tiles"));
            // Select a few rows → floating bulk bar; open a row menu.
            await p.evaluate(() => {
                [...document.querySelectorAll("#tiles-list tbody input[type=checkbox]")].slice(1, 4)
                    .forEach(cb => cb.click());
            });
            await p.evaluate(() => document.querySelector("#tiles-list tbody tr:nth-child(6) .menu-trigger")?.click());
            await shot(p, t("15-admin-tiles-bulk-menu"));
            await p.keyboard.press("Escape");
            await p.click("#bulk-clear");
            await p.click("#view-grid");
            await sleep(1500);
            await shot(p, t("16-admin-tiles-grid"), { wait: 1200 });
            await p.click("#view-table");
            await sleep(600);
            // Viewer on a reviewed tile (has mask + history)
            await p.select("#filter-status", "reviewed");
            await sleep(800);
            // Fall back to any tile when the seed has no reviewed ones.
            if (!(await p.$("#tiles-list tbody tr"))) {
                await p.select("#filter-status", "");
                await sleep(800);
            }
            await p.evaluate(() => {
                const rows = document.querySelectorAll("#tiles-list tbody tr");
                const row = rows[Math.min(2, rows.length - 1)];
                [...row.querySelectorAll("button")].find(b => b.textContent.trim() === "Ver").click();
            });
            await p.waitForSelector("#view-tile-body .tile-info:not(.is-loading)", { timeout: 10000 });
            await shot(p, t("17-admin-viewer"), { wait: 1800 });
            await p.click("#view-tile-close");

            await tab(p, "map");
            await shot(p, t("18-admin-map"), { wait: 2500 });
            // Open a tile in the side panel via the public click path.
            await p.evaluate(() => {
                const c = document.querySelector("#admin-map canvas");
                if (!c) return;
                const r = c.getBoundingClientRect();
                const ev = (type) => new MouseEvent(type, { bubbles: true, clientX: r.left + r.width * 0.42, clientY: r.top + r.height * 0.5 });
                c.dispatchEvent(ev("mousedown")); c.dispatchEvent(ev("mouseup")); c.dispatchEvent(ev("click"));
            });
            await shot(p, t("19-admin-map-panel"), { wait: 2500 });

            await tab(p, "problems");
            await selectScope(p, "problems", null);
            await shot(p, t("20-admin-problems"));
            await tab(p, "users");
            await shot(p, t("21-admin-users"));
            await tab(p, "maintenance");
            await shot(p, t("22-admin-maintenance"));
            await close(p);
        }
    } finally {
        if (browser) await browser.close().catch(() => {});
        server.kill("SIGINT");
        await sleep(500);
        try { server.kill("SIGKILL"); } catch {}
        await sleep(300);
        try { rmSync(tmp, { recursive: true, force: true }); } catch {}
    }
    return saved;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
    const args = process.argv.slice(2);
    const themesIdx = args.indexOf("--themes");
    const themes = themesIdx >= 0 ? args[themesIdx + 1].split(",") : ["light", "dark"];
    const outDir = args.find((a, i) => !a.startsWith("--") && args[i - 1] !== "--themes") || "screenshots";
    await runScreenshots({ outDir, themes, demo: args.includes("--demo") });
}
