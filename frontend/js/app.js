// SPA entrypoint: login -> editor | admin based on role.
import { apiGet, login, getTokens, setTokens } from "./api.js";
import { initEditor } from "./editor.js";
import { initAdmin } from "./admin.js";
import { showToast } from "./toast.js";

function show(viewId) {
    for (const v of document.querySelectorAll(".view")) v.classList.add("hidden");
    document.getElementById(viewId).classList.remove("hidden");
}

async function start() {
    const tokens = getTokens();
    if (!tokens?.access_token) return showLogin();
    try {
        const me = await apiGet("/api/auth/me");
        route(me);
    } catch {
        setTokens(null);
        showLogin();
    }
}

function route(user) {
    if (user.role === "admin") {
        show("view-admin");
        initAdmin(user);
    } else {
        show("view-editor");
        initEditor(user);
    }
}

function showLogin() {
    show("view-login");
    const form = document.getElementById("login-form");
    const btn = form.querySelector("button[type=submit]");
    form.onsubmit = async (ev) => {
        ev.preventDefault();
        if (btn.disabled) return;
        const u = document.getElementById("login-username").value;
        const p = document.getElementById("login-password").value;
        const err = document.getElementById("login-error");
        err.textContent = "";
        btn.disabled = true;
        const originalLabel = btn.textContent;
        btn.innerHTML = '<span class="loading"></span> Entrando...';
        try {
            await login(u, p);
            const me = await apiGet("/api/auth/me");
            route(me);
        } catch (e) {
            err.textContent = e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = originalLabel;
        }
    };
}

start();
