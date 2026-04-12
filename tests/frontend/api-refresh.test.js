// Tests for api.js: JWT refresh (proactive + reactive on 401), token storage.
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";

// Build a fake JWT with {exp} seconds from now (no real crypto).
function makeJwt(expSecondsFromNow) {
    const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = btoa(JSON.stringify({
        sub: "1", exp: Math.floor(Date.now() / 1000) + expSecondsFromNow,
    }));
    return `${header}.${payload}.signature`;
}

describe("api.js — token handling & refresh", () => {
    let api;

    beforeEach(async () => {
        localStorage.clear();
        vi.resetModules();
        vi.useFakeTimers();
        // Re-import fresh module each test so internal _refreshTimer is reset.
        api = await import("../../frontend/js/api.js");
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it("setTokens persists to localStorage and getTokens reads back", () => {
        api.setTokens({ access_token: "a", refresh_token: "b" });
        const t = api.getTokens();
        expect(t.access_token).toBe("a");
        expect(t.refresh_token).toBe("b");
    });

    it("getTokens returns null on empty/corrupt storage", () => {
        expect(api.getTokens()).toBeNull();
        localStorage.setItem("tileclass_tokens", "not-json");
        expect(api.getTokens()).toBeNull();
    });

    it("setTokens(null) clears storage", () => {
        api.setTokens({ access_token: "a", refresh_token: "b" });
        api.setTokens(null);
        expect(api.getTokens()).toBeNull();
    });

    it("reactive refresh: 401 triggers /refresh and retries original request", async () => {
        api.setTokens({ access_token: makeJwt(3600), refresh_token: "r" });

        const newAccess = makeJwt(3600);
        const fetchMock = vi.fn()
            // 1st call: protected endpoint → 401
            .mockResolvedValueOnce(new Response(null, { status: 401 }))
            // 2nd call: /api/auth/refresh → 200 with new tokens
            .mockResolvedValueOnce(new Response(
                JSON.stringify({ access_token: newAccess, refresh_token: "r2" }),
                { status: 200, headers: { "content-type": "application/json" } }
            ))
            // 3rd call: retried original request → 200
            .mockResolvedValueOnce(new Response(
                JSON.stringify({ ok: true }),
                { status: 200, headers: { "content-type": "application/json" } }
            ));
        global.fetch = fetchMock;

        const result = await api.apiGet("/api/tiles/next");
        expect(result).toEqual({ ok: true });
        expect(fetchMock).toHaveBeenCalledTimes(3);
        expect(fetchMock.mock.calls[1][0]).toBe("/api/auth/refresh");
        // New tokens persisted
        expect(api.getTokens().access_token).toBe(newAccess);
    });

    it("reactive refresh: 401 → refresh fails → reloads page and throws", async () => {
        api.setTokens({ access_token: makeJwt(3600), refresh_token: "r" });
        const reload = vi.fn();
        Object.defineProperty(window, "location", {
            value: { ...window.location, reload },
            writable: true,
        });
        global.fetch = vi.fn()
            .mockResolvedValueOnce(new Response(null, { status: 401 }))
            .mockResolvedValueOnce(new Response("", { status: 401 }));

        await expect(api.apiGet("/api/tiles/next")).rejects.toThrow();
        expect(reload).toHaveBeenCalled();
    });

    it("attaches Authorization header with access_token", async () => {
        api.setTokens({ access_token: "TOKEN123", refresh_token: "r" });
        const fetchMock = vi.fn().mockResolvedValue(
            new Response("{}", { status: 200, headers: { "content-type": "application/json" } })
        );
        global.fetch = fetchMock;
        await api.apiGet("/api/auth/me");
        const headers = fetchMock.mock.calls[0][1].headers;
        expect(headers.get("Authorization")).toBe("Bearer TOKEN123");
    });

    it("no Authorization header when no token", async () => {
        const fetchMock = vi.fn().mockResolvedValue(
            new Response("{}", { status: 200, headers: { "content-type": "application/json" } })
        );
        global.fetch = fetchMock;
        await api.apiGet("/api/config/classes");
        const headers = fetchMock.mock.calls[0][1].headers;
        expect(headers.get("Authorization")).toBeNull();
    });

    it("proactive refresh schedules a timer when token is set", async () => {
        // Token expires in 120s → refresh scheduled for 60s (120 - 60)
        const fetchMock = vi.fn().mockResolvedValue(
            new Response(JSON.stringify({
                access_token: makeJwt(3600), refresh_token: "r2"
            }), { status: 200, headers: { "content-type": "application/json" } })
        );
        global.fetch = fetchMock;

        api.setTokens({ access_token: makeJwt(120), refresh_token: "r" });

        // Nothing yet
        expect(fetchMock).not.toHaveBeenCalled();

        // Advance 60s → refresh should fire
        await vi.advanceTimersByTimeAsync(60_000);
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/refresh");
    });

    it("apiPostBytes sends octet-stream with Uint8Array body", async () => {
        api.setTokens({ access_token: "T", refresh_token: "r" });
        const fetchMock = vi.fn().mockResolvedValue(
            new Response("{}", { status: 200, headers: { "content-type": "application/json" } })
        );
        global.fetch = fetchMock;

        const bytes = new Uint8Array([1, 2, 3, 4]);
        await api.apiPostBytes("/api/tiles/1/classify", bytes);
        const opts = fetchMock.mock.calls[0][1];
        expect(opts.method).toBe("POST");
        expect(opts.headers.get("Content-Type")).toBe("application/octet-stream");
        expect(opts.body).toBe(bytes);
    });

    it("apiJson throws with status and body on non-OK response", async () => {
        global.fetch = vi.fn().mockResolvedValue(
            new Response(
                JSON.stringify({ detail: { error: "unfilled_pixels", missing: 42 } }),
                { status: 422, headers: { "content-type": "application/json" } }
            )
        );
        try {
            await api.apiGet("/api/x");
            throw new Error("should have thrown");
        } catch (e) {
            expect(e.status).toBe(422);
            expect(e.body.detail.missing).toBe(42);
        }
    });

    it("apiJson returns null on 204 No Content", async () => {
        global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
        const r = await api.apiGet("/api/tiles/next");
        expect(r).toBeNull();
    });
});
