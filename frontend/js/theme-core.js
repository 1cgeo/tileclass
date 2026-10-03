// Pure theme logic (testable without DOM).
//
// Preference is "system" | "light" | "dark". The toggle is two-state from the
// user's point of view (light <-> dark): when the chosen theme matches the OS
// theme we store "system" again, so the app goes back to following the OS
// instead of pinning a value the user never meant to pin.

export const THEME_STORAGE_KEY = "tileclass_theme";
export const THEME_PREFS = ["system", "light", "dark"];

export function normalizePref(pref) {
    return THEME_PREFS.includes(pref) ? pref : "system";
}

export function resolveTheme(pref, systemDark) {
    const p = normalizePref(pref);
    if (p === "light" || p === "dark") return p;
    return systemDark ? "dark" : "light";
}

export function toggledPref(pref, systemDark) {
    const next = resolveTheme(pref, systemDark) === "dark" ? "light" : "dark";
    const system = systemDark ? "dark" : "light";
    return next === system ? "system" : next;
}

// What the toggle button shows: the icon of the theme it switches TO, plus a
// pt-BR label for title/aria-label.
export function toggleAffordance(effective) {
    return effective === "dark"
        ? { icon: "sun", label: "Mudar para tema claro" }
        : { icon: "moon", label: "Mudar para tema escuro" };
}
