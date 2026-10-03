import { describe, it, expect } from "vitest";
import {
    normalizePref, resolveTheme, toggledPref, toggleAffordance,
} from "../../frontend/js/theme-core.js";

describe("resolveTheme", () => {
    it("explicit preference wins over the OS", () => {
        expect(resolveTheme("light", true)).toBe("light");
        expect(resolveTheme("dark", false)).toBe("dark");
    });
    it("system follows the OS; unknown values fall back to system", () => {
        expect(resolveTheme("system", true)).toBe("dark");
        expect(resolveTheme("system", false)).toBe("light");
        expect(resolveTheme(null, true)).toBe("dark");
        expect(resolveTheme("bogus", false)).toBe("light");
        expect(normalizePref("bogus")).toBe("system");
    });
});

describe("toggledPref", () => {
    it("flips the effective theme", () => {
        expect(resolveTheme(toggledPref("system", false), false)).toBe("dark");
        expect(resolveTheme(toggledPref("system", true), true)).toBe("light");
        expect(resolveTheme(toggledPref("dark", false), false)).toBe("light");
    });
    it("returns to 'system' when the chosen theme equals the OS theme", () => {
        expect(toggledPref("dark", false)).toBe("system");   // OS light, go light
        expect(toggledPref("light", true)).toBe("system");   // OS dark, go dark
        expect(toggledPref("system", false)).toBe("dark");   // pin dark against OS
    });
});

describe("toggleAffordance", () => {
    it("shows the target theme's icon and a pt-BR label", () => {
        expect(toggleAffordance("light")).toEqual({ icon: "moon", label: "Mudar para tema escuro" });
        expect(toggleAffordance("dark")).toEqual({ icon: "sun", label: "Mudar para tema claro" });
    });
});
