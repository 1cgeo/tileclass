import { describe, it, expect } from "vitest";
import { hexToRgb, escapeHtml, statusLabel } from "../../frontend/js/utils.js";

describe("hexToRgb", () => {
    it("parses 6-digit hex with leading #", () => {
        expect(hexToRgb("#FF3B30")).toEqual([255, 59, 48]);
    });
    it("parses 6-digit hex without #", () => {
        expect(hexToRgb("007AFF")).toEqual([0, 122, 255]);
    });
    it("parses lowercase hex", () => {
        expect(hexToRgb("#ffcc00")).toEqual([255, 204, 0]);
    });
    it("handles black and white", () => {
        expect(hexToRgb("#000000")).toEqual([0, 0, 0]);
        expect(hexToRgb("#FFFFFF")).toEqual([255, 255, 255]);
    });
});

describe("escapeHtml", () => {
    it("escapes HTML special characters", () => {
        expect(escapeHtml("<script>alert(1)</script>"))
            .toBe("&lt;script&gt;alert(1)&lt;/script&gt;");
    });
    it("escapes ampersand, quotes, apostrophe", () => {
        expect(escapeHtml(`a&b"c'd`)).toBe("a&amp;b&quot;c&#39;d");
    });
    it("returns empty string for null/undefined", () => {
        expect(escapeHtml(null)).toBe("");
        expect(escapeHtml(undefined)).toBe("");
    });
    it("stringifies non-string input", () => {
        expect(escapeHtml(42)).toBe("42");
        expect(escapeHtml(true)).toBe("true");
    });
    it("preserves unicode", () => {
        expect(escapeHtml("ação é tudo")).toBe("ação é tudo");
    });
});

describe("statusLabel", () => {
    it("translates every backend status to pt-BR", () => {
        // Lock the table down so a backend rename forces a frontend change.
        expect(statusLabel("pending")).toBe("Pendente");
        expect(statusLabel("in_progress")).toBe("Em andamento");
        expect(statusLabel("classified")).toBe("Classificado");
        expect(statusLabel("in_review")).toBe("Em revisão");
        expect(statusLabel("reviewed")).toBe("Revisado");
        expect(statusLabel("problem")).toBe("Problema");
        expect(statusLabel("blocked")).toBe("Bloqueado");
        expect(statusLabel("paused")).toBe("Pausado");
    });
    it("flags paused tiles distinctly, with a review variant", () => {
        // Paused is a virtual status (status=in_progress|in_review with
        // paused_at != NULL). Reviews get an explicit suffix so admins can
        // tell paused-classify from paused-review at a glance.
        expect(statusLabel("in_progress", true)).toBe("Pausado");
        expect(statusLabel("in_review", true)).toBe("Pausado (revisão)");
    });
    it("falls back to the raw value for unknown statuses", () => {
        expect(statusLabel("future_state")).toBe("future_state");
    });
});
