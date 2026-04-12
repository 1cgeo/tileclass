// API wrapper: attaches Authorization header, refreshes on 401.
const TOKENS_KEY = "tileclass_tokens";

export function getTokens() {
    try { return JSON.parse(localStorage.getItem(TOKENS_KEY)) || null; }
    catch { return null; }
}

export function setTokens(t) {
    if (t) localStorage.setItem(TOKENS_KEY, JSON.stringify(t));
    else localStorage.removeItem(TOKENS_KEY);
    scheduleProactiveRefresh();
}

function decodeJwtExp(token) {
    try {
        const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
        return payload.exp ? payload.exp * 1000 : null;
    } catch { return null; }
}

let _refreshTimer = null;
// Refresh 60s before expiry so a long idle session auto-renews.
function scheduleProactiveRefresh() {
    clearTimeout(_refreshTimer);
    const t = getTokens();
    if (!t?.access_token) return;
    const expMs = decodeJwtExp(t.access_token);
    if (!expMs) return;
    const fireIn = Math.max(5_000, expMs - Date.now() - 60_000);
    _refreshTimer = setTimeout(() => {
        refreshAccess().catch(() => {}); // setTokens inside will reschedule
    }, fireIn);
}

let _refreshing = null;

async function refreshAccess() {
    const t = getTokens();
    if (!t?.refresh_token) throw new Error("no refresh token");
    const res = await fetch("/api/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: t.refresh_token }),
    });
    if (!res.ok) { setTokens(null); throw new Error("refresh failed"); }
    const data = await res.json();
    setTokens(data);
    return data.access_token;
}

async function _fetch(url, opts = {}, retry = true) {
    const tokens = getTokens();
    const headers = new Headers(opts.headers || {});
    if (tokens?.access_token) headers.set("Authorization", `Bearer ${tokens.access_token}`);
    const res = await fetch(url, { ...opts, headers });
    if (res.status === 401 && retry && tokens?.refresh_token) {
        if (!_refreshing) _refreshing = refreshAccess().finally(() => { _refreshing = null; });
        try { await _refreshing; }
        catch { location.reload(); throw new Error("auth"); }
        return _fetch(url, opts, false);
    }
    return res;
}

export async function apiJson(url, opts = {}) {
    const res = await _fetch(url, opts);
    if (!res.ok) {
        let body;
        try { body = await res.json(); } catch { body = { detail: res.statusText }; }
        const err = new Error(body.detail?.error || body.detail || res.statusText);
        err.status = res.status;
        err.body = body;
        throw err;
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
}

export async function apiGet(url) {
    return apiJson(url, { method: "GET" });
}

export async function apiPostJson(url, body) {
    return apiJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}

export async function apiPostBytes(url, bytes) {
    return apiJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: bytes,
    });
}

export async function apiGetBlob(url) {
    const res = await _fetch(url);
    if (!res.ok) throw new Error(res.statusText);
    return res.blob();
}

export async function login(username, password) {
    const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: "erro" }));
        throw new Error(body.detail || "falha no login");
    }
    const data = await res.json();
    setTokens(data);
    return data;
}
