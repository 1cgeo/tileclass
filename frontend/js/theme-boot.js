// Classic (non-module) script loaded synchronously in <head>: sets
// <html data-theme> before first paint so there is no light->dark flash.
// Mirrors theme-core.js resolveTheme(); keep the storage key in sync.
(function () {
    var theme = "light";
    try {
        var pref = localStorage.getItem("tileclass_theme") || "system";
        var systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
        theme = pref === "dark" || (pref !== "light" && systemDark) ? "dark" : "light";
    } catch (e) { /* storage blocked: fall back to light */ }
    document.documentElement.setAttribute("data-theme", theme);
})();
