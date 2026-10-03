// API wrapper: attaches Authorization header, refreshes on 401.
const TOKENS_KEY = "tileclass_tokens";

// Default timeout for all requests. Kept generous for slow tileservers but
// bounded so the UI never hangs forever on a broken network.
const DEFAULT_TIMEOUT_MS = 15_000;

export function getTokens() {
    try { return JSON.parse(localStorage.getItem(TOKENS_KEY)) || null; }
    catch { return null; }
}

export function authHeader() {
    const tok = getTokens()?.access_token;
    return tok ? { Authorization: `Bearer ${tok}` } : {};
}

export function setTokens(t) {
    if (t) localStorage.setItem(TOKENS_KEY, JSON.stringify(t));
    else localStorage.removeItem(TOKENS_KEY);
    scheduleProactiveRefresh();
    scheduleExpiryWarning();
}

function decodeJwtExp(token) {
    try {
        const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
        return payload.exp ? payload.exp * 1000 : null;
    } catch { return null; }
}

let _refreshTimer = null;
function scheduleProactiveRefresh() {
    clearTimeout(_refreshTimer);
    const t = getTokens();
    if (!t?.access_token) return;
    const expMs = decodeJwtExp(t.access_token);
    if (!expMs) return;
    const fireIn = Math.max(5_000, expMs - Date.now() - 60_000);
    _refreshTimer = setTimeout(() => {
        refreshAccess().catch(() => {});
    }, fireIn);
}

// --- Session expiry warning (5 min before refresh fails) ---
let _warnTimer = null;
let _expiryListeners = [];
export function onSessionWarning(fn) { _expiryListeners.push(fn); }
function scheduleExpiryWarning() {
    clearTimeout(_warnTimer);
    const t = getTokens();
    if (!t?.refresh_token) return;
    const expMs = decodeJwtExp(t.refresh_token);
    if (!expMs) return;
    const fireIn = expMs - Date.now() - 5 * 60_000;
    if (fireIn <= 0) return;
    _warnTimer = setTimeout(() => {
        _expiryListeners.forEach(fn => { try { fn(); } catch {} });
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

function _timeoutSignal(ms) {
    if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
        return AbortSignal.timeout(ms);
    }
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), ms);
    return ctrl.signal;
}

async function _fetch(url, opts = {}, retry = true) {
    const tokens = getTokens();
    const headers = new Headers(opts.headers || {});
    if (tokens?.access_token) headers.set("Authorization", `Bearer ${tokens.access_token}`);
    const signal = opts.signal || _timeoutSignal(opts.timeout ?? DEFAULT_TIMEOUT_MS);
    let res;
    try {
        res = await fetch(url, { ...opts, headers, signal });
    } catch (e) {
        if (e.name === "AbortError" || e.name === "TimeoutError") {
            const err = new Error("Tempo esgotado. Verifique sua conexão.");
            err.timeout = true;
            throw err;
        }
        throw e;
    }
    if (res.status === 401 && retry && tokens?.refresh_token) {
        if (!_refreshing) _refreshing = refreshAccess().finally(() => { _refreshing = null; });
        try { await _refreshing; }
        catch { location.reload(); throw new Error("auth"); }
        return _fetch(url, opts, false);
    }
    return res;
}

function _message(body, fallback) {
    const d = body?.detail;
    if (!d) return fallback;
    if (typeof d === "string") return d;
    return d.message || d.error || fallback;
}

export async function apiJson(url, opts = {}) {
    const res = await _fetch(url, opts);
    if (!res.ok) {
        let body;
        try { body = await res.json(); } catch { body = { detail: res.statusText }; }
        const err = new Error(_message(body, res.statusText));
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

export async function apiGetWithHeaders(url) {
    const res = await _fetch(url);
    if (!res.ok) {
        let body; try { body = await res.json(); } catch { body = { detail: res.statusText }; }
        const err = new Error(_message(body, res.statusText));
        err.status = res.status; err.body = body; throw err;
    }
    return { json: await res.json(), headers: res.headers };
}

export async function apiPostJson(url, body, extraHeaders = {}) {
    return apiJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...extraHeaders },
        body: JSON.stringify(body),
    });
}

export async function apiPatchJson(url, body) {
    return apiJson(url, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}

export async function apiPutJson(url, body) {
    return apiJson(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}

export async function apiDelete(url) {
    return apiJson(url, { method: "DELETE" });
}

export async function apiPostBytes(url, bytes, extraHeaders = {}) {
    return apiJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", ...extraHeaders },
        body: bytes,
    });
}

export async function apiGetBlob(url, { timeout = DEFAULT_TIMEOUT_MS, retries = 0 } = {}) {
    let lastErr;
    for (let i = 0; i <= retries; i++) {
        try {
            const res = await _fetch(url, { timeout });
            if (!res.ok) {
                let msg = res.statusText;
                try { const b = await res.json(); msg = _message(b, msg); } catch {}
                const err = new Error(msg);
                err.status = res.status;
                throw err;
            }
            return res.blob();
        } catch (e) {
            lastErr = e;
            // Retry only transient failures (timeout or 5xx). Hard client
            // errors (4xx) are deterministic and never worth retrying.
            if (!e.timeout && e.status && e.status < 500) throw e;
        }
    }
    throw lastErr;
}

export async function login(username, password) {
    const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
        signal: _timeoutSignal(10_000),
    });
    if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: "erro" }));
        throw new Error(_message(body, "Falha no login"));
    }
    const data = await res.json();
    setTokens(data);
    return data;
}

export async function logout() {
    try { await apiPostJson("/api/auth/logout", {}); }
    catch {} // ignore — revoking locally is sufficient fallback
    setTokens(null);
}
