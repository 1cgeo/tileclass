// SPA entrypoint: login -> editor | admin based on role.
// Admins can toggle between the admin panel and the editor (working as an
// operator). Last-chosen view is persisted in localStorage.
import { apiGet, login, getTokens, setTokens } from "./api.js";
import { initEditor, enterEditor } from "./editor.js";
import { initAdmin, enterAdmin } from "./admin.js";

const LS_ADMIN_LAST_VIEW = "tileclass_admin_last_view";

const VIEWS = {
    admin:  { id: "view-admin",  init: initAdmin,  enter: enterAdmin,  ready: false },
    editor: { id: "view-editor", init: initEditor, enter: enterEditor, ready: false },
};

function show(viewId) {
    for (const v of document.querySelectorAll(".view")) v.classList.add("hidden");
    document.getElementById(viewId).classList.remove("hidden");
}

async function switchView(target, user) {
    const v = VIEWS[target];
    show(v.id);
    if (!v.ready) { v.ready = true; await v.init(user); }
    else { await v.enter(user); }
    if (user.role === "admin") {
        try { localStorage.setItem(LS_ADMIN_LAST_VIEW, target); } catch {}
    }
}

function wireToggleButtons(user) {
    if (user.role !== "admin") return;
    const goAdmin = document.getElementById("btn-go-admin");
    const goEditor = document.getElementById("btn-go-editor");
    goAdmin.classList.remove("hidden");
    goAdmin.onclick = () => switchView("admin", user);
    goEditor.onclick = () => switchView("editor", user);
}

async function start() {
    const tokens = getTokens();
    if (!tokens?.access_token) return showLogin();
    try {
        const me = await apiGet("/api/auth/me");
        await route(me);
    } catch {
        setTokens(null);
        showLogin();
    }
}

async function route(user) {
    wireToggleButtons(user);
    let target = "editor";
    if (user.role === "admin") {
        try { target = localStorage.getItem(LS_ADMIN_LAST_VIEW) === "editor" ? "editor" : "admin"; }
        catch { target = "admin"; }
    }
    await switchView(target, user);
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
            await route(me);
        } catch (e) {
            err.textContent = e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = originalLabel;
        }
    };
}

start();
