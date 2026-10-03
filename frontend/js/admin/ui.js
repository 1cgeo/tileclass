// Small DOM building blocks shared by the admin tabs: a hyperscript helper,
// cards, KPI tiles, avatars, status chips, empty states and an overflow menu.
// Everything sets text through textContent — never innerHTML with data.
import { icon, statusLabel } from "../utils.js";
import { initials, avatarTone } from "../admin-core.js";

// h("div", {class: "x", title: "t", onclick: fn, dataset: {id: 1}}, child, "text")
export function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k in el && typeof v !== "string") el[k] = v;
        else el.setAttribute(k, v === true ? "" : String(v));
    }
    appendChildren(el, children);
    return el;
}

function appendChildren(el, children) {
    for (const c of children.flat(Infinity)) {
        if (c == null || c === false) continue;
        el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
}

export { icon };

// Button with optional leading icon. `variant` is a base.css button class
// list ("primary", "ghost btn-sm", …).
export function button(label, { icon: ic, variant = "", title, onClick, id, type = "button", disabled } = {}) {
    const b = h("button", { type, class: variant || null, title, id, disabled });
    if (ic) b.appendChild(icon(ic));
    if (label) b.appendChild(document.createTextNode(label));
    if (onClick) b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(ev); });
    return b;
}

export function iconButton(ic, label, { onClick, variant = "ghost", sm = true, id } = {}) {
    const b = h("button", {
        type: "button", class: `${variant} icon-btn${sm ? " btn-sm" : ""}`,
        title: label, "aria-label": label, id,
    }, icon(ic));
    if (onClick) b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(ev); });
    return b;
}

export function avatar(name, { size = "" } = {}) {
    return h("span", {
        class: `avatar avatar-tone-${avatarTone(name)}${size ? ` avatar-${size}` : ""}`,
        "aria-hidden": "true", text: initials(name),
    });
}

// Avatar + name, or a muted dash when there's no user.
export function userCell(name, { hint } = {}) {
    if (!name) return h("span", { class: "dim", text: "—" });
    return h("span", { class: "user-cell" }, avatar(name, { size: "xs" }),
        h("span", { class: "user-cell-name", text: name }),
        hint ? h("span", { class: "user-cell-hint", text: hint }) : null);
}

export function statusChip(status, { paused = false, title } = {}) {
    return h("span", {
        class: `chip ${paused ? "paused" : status}`,
        text: statusLabel(status, paused), title,
    });
}

// Card with optional header (title, subtitle, actions) and body children.
export function card({ title, subtitle, icon: ic, actions = [], cls = "", bodyCls = "" } = {}, ...body) {
    const el = h("section", { class: `card admin-card ${cls}`.trim() });
    if (title) {
        const head = h("header", { class: "card-header" },
            h("div", { class: "card-heading" },
                ic ? h("span", { class: "card-heading-icon" }, icon(ic)) : null,
                h("div", {},
                    h("h3", { class: "card-title", text: title }),
                    subtitle ? h("p", { class: "card-subtitle", text: subtitle }) : null)),
            actions.length ? h("div", { class: "card-actions" }, actions) : null);
        el.appendChild(head);
    }
    const b = h("div", { class: `card-body ${bodyCls}`.trim() });
    appendChildren(b, body);
    el.appendChild(b);
    return el;
}

// KPI tile: icon chip + label, big tabular value, secondary line.
export function kpi({ icon: ic, label, value, sub, tone = "accent", title, badge }) {
    return h("div", { class: `kpi kpi-${tone}`, title },
        h("div", { class: "kpi-head" },
            h("span", { class: "kpi-icon" }, icon(ic)),
            h("span", { class: "kpi-label", text: label }),
            badge ? h("span", { class: "kpi-badge", text: badge }) : null),
        h("div", { class: "kpi-value tabular", text: String(value) }),
        sub ? h("div", { class: "kpi-sub", text: sub }) : null);
}

export function emptyState({ icon: ic = "inbox", title, text, action } = {}) {
    return h("div", { class: "empty-state admin-empty" },
        h("span", { class: "admin-empty-icon" }, icon(ic)),
        title ? h("strong", { text: title }) : null,
        text ? h("p", { class: "muted", text }) : null,
        action || null);
}

export function progressBar(pct, { label } = {}) {
    const v = Math.max(0, Math.min(100, Number(pct) || 0));
    const fill = h("span", { class: "progress-fill" });
    fill.style.width = `${v}%`;
    return h("span", {
        class: "progress", role: "progressbar", "aria-valuemin": "0",
        "aria-valuemax": "100", "aria-valuenow": String(Math.round(v)), "aria-label": label,
    }, fill);
}

// Read a design token (e.g. "--st-reviewed") as a concrete color string.
export function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---- Overflow menu -----------------------------------------------------------
// One menu open at a time; closes on outside click, Escape, scroll or resize.
let _openMenu = null;

export function closeMenu() {
    if (!_openMenu) return;
    const { el, anchor, cleanup } = _openMenu;
    _openMenu = null;
    cleanup();
    el.remove();
    anchor.setAttribute("aria-expanded", "false");
    anchor.classList.remove("is-open");
}

// items: [{label, icon, danger, disabled, title, onClick}] | "sep"
export function openMenu(anchor, items) {
    const wasThis = _openMenu?.anchor === anchor;
    closeMenu();
    if (wasThis) return;
    const el = h("div", { class: "menu", role: "menu" });
    for (const it of items) {
        if (it === "sep") { el.appendChild(h("div", { class: "menu-sep", role: "separator" })); continue; }
        const b = h("button", {
            type: "button", role: "menuitem", class: `menu-item${it.danger ? " danger" : ""}`,
            disabled: !!it.disabled, title: it.title,
        });
        if (it.icon) b.appendChild(icon(it.icon));
        b.appendChild(document.createTextNode(it.label));
        b.addEventListener("click", (ev) => { ev.stopPropagation(); closeMenu(); it.onClick?.(); });
        el.appendChild(b);
    }
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const mw = el.offsetWidth, mh = el.offsetHeight;
    let left = r.right - mw;
    let top = r.bottom + 6;
    if (left < 8) left = 8;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    anchor.setAttribute("aria-expanded", "true");
    anchor.classList.add("is-open");
    const onDoc = (ev) => { if (!el.contains(ev.target) && ev.target !== anchor) closeMenu(); };
    const onKey = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); closeMenu(); anchor.focus(); } };
    const onScroll = () => closeMenu();
    setTimeout(() => document.addEventListener("click", onDoc), 0);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onScroll);
    document.addEventListener("scroll", onScroll, true);
    _openMenu = {
        el, anchor,
        cleanup: () => {
            document.removeEventListener("click", onDoc);
            document.removeEventListener("keydown", onKey, true);
            window.removeEventListener("resize", onScroll);
            document.removeEventListener("scroll", onScroll, true);
        },
    };
    el.querySelector(".menu-item:not(:disabled)")?.focus();
}

// Ellipsis button wired to openMenu. `getItems` is called on each open so the
// menu reflects current state.
export function menuButton(getItems, label = "Mais ações") {
    const b = iconButton("ellipsis", label);
    b.setAttribute("aria-haspopup", "menu");
    b.setAttribute("aria-expanded", "false");
    b.classList.add("menu-trigger");
    b.addEventListener("click", () => openMenu(b, getItems()));
    return b;
}

// Sortable table header cell: label + direction arrow; Enter/Space toggles.
export function sortHeader(label, { key, activeKey, dir, onSort, num = false }) {
    const active = key === activeKey;
    const th = h("th", {
        class: `sortable${num ? " num" : ""}${active ? " sorted" : ""}`,
        tabIndex: 0, "aria-sort": active ? (dir === "asc" ? "ascending" : "descending") : "none",
        title: "Clique para ordenar",
    }, h("span", { class: "th-inner" }, label,
        icon(active ? (dir === "asc" ? "chevron-up" : "chevron-down") : "chevron-down",
             `icon-sm sort-icon${active ? "" : " sort-icon-idle"}`)));
    const fire = () => onSort(key);
    th.addEventListener("click", fire);
    th.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fire(); }
    });
    return th;
}
