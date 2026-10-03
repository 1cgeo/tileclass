// Theme runtime: applies the resolved theme to <html data-theme>, follows the
// OS while the preference is "system", and wires every [data-theme-toggle]
// button. Dispatches `tc-themechange` on window ({detail: {theme}}) so code
// that paints with theme colors outside CSS (canvas, MapLibre) can repaint.
import {
    THEME_STORAGE_KEY, normalizePref, resolveTheme, toggledPref, toggleAffordance,
} from "./theme-core.js";
import { ICON_SPRITE } from "./utils.js";

const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function readPref() {
    try { return normalizePref(localStorage.getItem(THEME_STORAGE_KEY)); }
    catch { return "system"; }
}

function writePref(pref) {
    try {
        if (pref === "system") localStorage.removeItem(THEME_STORAGE_KEY);
        else localStorage.setItem(THEME_STORAGE_KEY, pref);
    } catch { /* storage blocked: theme still applies for this session */ }
}

export function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function apply(pref) {
    const theme = resolveTheme(pref, !!media?.matches);
    const changed = theme !== currentTheme();
    document.documentElement.setAttribute("data-theme", theme);
    const { icon, label } = toggleAffordance(theme);
    for (const btn of document.querySelectorAll("[data-theme-toggle]")) {
        btn.title = label;
        btn.setAttribute("aria-label", label);
        btn.querySelector("use")?.setAttribute("href", `${ICON_SPRITE}#i-${icon}`);
    }
    if (changed) window.dispatchEvent(new CustomEvent("tc-themechange", { detail: { theme } }));
}

let _initialized = false;
export function initTheme() {
    if (_initialized) return;
    _initialized = true;
    apply(readPref());
    media?.addEventListener?.("change", () => {
        if (readPref() === "system") apply("system");
    });
    // Delegated so buttons rendered later (admin re-renders) work too.
    document.addEventListener("click", (ev) => {
        const btn = ev.target.closest?.("[data-theme-toggle]");
        if (!btn) return;
        const pref = toggledPref(readPref(), !!media?.matches);
        writePref(pref);
        apply(pref);
    });
}
