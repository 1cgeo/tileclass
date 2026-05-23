// Confirm + assign modals shared across admin tabs (tiles, users, map, viewer).
// Pure DOM — no admin state, so the rest of admin/ can import without cycles.

// Wire a click-on-backdrop dismiss for any modal element. Returns the
// teardown fn — callers store it and call inside their cleanup() so the
// listener never outlives the modal instance.
function wireBackdropDismiss(modal, onCancel) {
    const handler = (ev) => { if (ev.target === modal) onCancel(); };
    modal.addEventListener("click", handler);
    return () => modal.removeEventListener("click", handler);
}


export function wireConfirmModal() {
    const modal = document.getElementById("modal-confirm");
    if (!modal) return;
    const reason = document.getElementById("confirm-reason");
    if (reason) reason.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            document.getElementById("confirm-ok")?.click();
        }
    });
}

export function confirmDestructive({
    title, description, ids, confirmLabel = "Confirmar", danger = true,
    reasonLabel, reasonPlaceholder, reasonRequired = false, reasonMaxLength = 500,
}) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-confirm");
        document.getElementById("confirm-title").textContent = title;
        document.getElementById("confirm-description").textContent = description;
        const list = document.getElementById("confirm-ids");
        list.innerHTML = "";
        // Bulk callers pass an ID list; single-item callers (remove member,
        // delete project) pass [singleId] or []. Hide the line entirely when
        // there's nothing useful to show — "IDs: 5" for a user id is more
        // confusing than informative.
        if (ids && ids.length > 1) {
            const preview = ids.slice(0, 20);
            list.textContent = `IDs: ${preview.join(", ")}${ids.length > 20 ? ` … (+${ids.length - 20})` : ""}`;
            list.style.display = "";
        } else {
            list.style.display = "none";
        }
        const reason = document.getElementById("confirm-reason");
        const reasonLabelEl = modal.querySelector(".confirm-reason-label");
        // Remember original label/placeholder/maxlength so we can restore them
        // on close — other callers share this modal and expect the defaults.
        const origLabelText = reasonLabelEl?.firstChild?.nodeValue;
        const origPlaceholder = reason.placeholder;
        const origMaxLength = reason.maxLength;
        if (reasonLabel && reasonLabelEl?.firstChild) reasonLabelEl.firstChild.nodeValue = reasonLabel;
        if (reasonPlaceholder !== undefined) reason.placeholder = reasonPlaceholder;
        if (reasonMaxLength) reason.maxLength = reasonMaxLength;
        reason.value = "";
        const ok = document.getElementById("confirm-ok");
        ok.textContent = confirmLabel;
        ok.classList.toggle("danger", !!danger);
        const cancel = document.getElementById("confirm-cancel");
        const updateOkState = () => {
            ok.disabled = reasonRequired && !reason.value.trim();
        };
        updateOkState();

        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            reason.removeEventListener("input", updateOkState);
            unwireBackdrop();
            ok.disabled = false;
            if (origLabelText && reasonLabelEl?.firstChild) reasonLabelEl.firstChild.nodeValue = origLabelText;
            reason.placeholder = origPlaceholder;
            reason.maxLength = origMaxLength;
            resolve(result);
        };
        const onOk = () => {
            if (reasonRequired && !reason.value.trim()) return;
            cleanup({ confirmed: true, reason: reason.value.trim() });
        };
        const onCancel = () => cleanup({ confirmed: false });
        const unwireBackdrop = wireBackdropDismiss(modal, onCancel);
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        reason.addEventListener("input", updateOkState);
        modal.classList.remove("hidden");
        setTimeout(() => reason.focus(), 50);
    });
}

// Generic form modal. Each field is `{key, label, type, required?, minLength?,
// maxLength?, placeholder?, autocomplete?, options?, defaultValue?, help?}`.
// Supported types: "text", "password", "select", "textarea". Resolves to
// `{confirmed, values}` where `values[key]` is the entered/selected string.
//
// Validation is purposely lightweight (required + minLength) — the backend is
// always the source of truth; frontend checks just prevent obvious submits.
// Caller is responsible for showing errors via showToast after the round-trip.
export function promptForm({
    title, description = "", fields, submitLabel = "Salvar",
    cancelLabel = "Cancelar", danger = false,
}) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-form");
        document.getElementById("form-title").textContent = title;
        const descEl = document.getElementById("form-description");
        descEl.textContent = description;
        descEl.classList.toggle("hidden", !description);
        const host = document.getElementById("form-fields");
        host.innerHTML = "";
        const controls = new Map();   // key → input element
        for (const f of fields) {
            const wrap = document.createElement("label");
            wrap.className = "modal-form-field";
            const labelText = document.createElement("span");
            labelText.className = "modal-form-label";
            labelText.textContent = f.label + (f.required ? " *" : "");
            wrap.appendChild(labelText);
            let ctrl;
            if (f.type === "select") {
                ctrl = document.createElement("select");
                for (const opt of f.options || []) {
                    const o = document.createElement("option");
                    o.value = String(opt.value);
                    o.textContent = opt.label;
                    ctrl.appendChild(o);
                }
                if (f.defaultValue !== undefined) ctrl.value = String(f.defaultValue);
            } else if (f.type === "textarea") {
                ctrl = document.createElement("textarea");
                ctrl.rows = f.rows || 3;
                if (f.maxLength) ctrl.maxLength = f.maxLength;
                if (f.placeholder) ctrl.placeholder = f.placeholder;
                if (f.defaultValue) ctrl.value = String(f.defaultValue);
            } else {
                ctrl = document.createElement("input");
                ctrl.type = f.type || "text";
                if (f.minLength) ctrl.minLength = f.minLength;
                if (f.maxLength) ctrl.maxLength = f.maxLength;
                if (f.placeholder) ctrl.placeholder = f.placeholder;
                if (f.autocomplete) ctrl.autocomplete = f.autocomplete;
                if (f.defaultValue) ctrl.value = String(f.defaultValue);
            }
            ctrl.id = `form-field-${f.key}`;
            ctrl.dataset.key = f.key;
            if (f.required) ctrl.required = true;
            wrap.appendChild(ctrl);
            if (f.help) {
                const help = document.createElement("span");
                help.className = "modal-form-help";
                help.textContent = f.help;
                wrap.appendChild(help);
            }
            host.appendChild(wrap);
            controls.set(f.key, ctrl);
        }

        const ok = document.getElementById("form-ok");
        ok.textContent = submitLabel;
        ok.classList.toggle("danger", !!danger);
        ok.classList.toggle("primary", !danger);
        const cancel = document.getElementById("form-cancel");
        cancel.textContent = cancelLabel;

        const validate = () => {
            for (const f of fields) {
                const v = controls.get(f.key).value;
                if (f.required && !String(v).trim()) return false;
                if (f.minLength && String(v).length < f.minLength) return false;
            }
            return true;
        };
        const updateOk = () => { ok.disabled = !validate(); };
        updateOk();

        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            host.removeEventListener("input", updateOk);
            host.removeEventListener("keydown", onKey);
            unwireBackdrop();
            ok.disabled = false;
            resolve(result);
        };
        const onOk = () => {
            if (!validate()) return;
            const values = {};
            for (const [k, el] of controls) values[k] = el.value;
            cleanup({ confirmed: true, values });
        };
        const onCancel = () => cleanup({ confirmed: false });
        const unwireBackdrop = wireBackdropDismiss(modal, onCancel);
        // Enter in a single-line input submits; textareas keep newline behavior
        // unless the user explicitly Ctrl+Enters. Same convention as the
        // confirm-reason field.
        const onKey = (e) => {
            if (e.key !== "Enter") return;
            const isTextarea = e.target.tagName === "TEXTAREA";
            if (isTextarea && !(e.ctrlKey || e.metaKey)) return;
            e.preventDefault();
            onOk();
        };
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        host.addEventListener("input", updateOk);
        host.addEventListener("keydown", onKey);
        modal.classList.remove("hidden");
        const first = fields.find(f => !f.disabled);
        if (first) setTimeout(() => controls.get(first.key).focus(), 50);
    });
}


// Generic shell modal for editors that need custom DOM (table editors, kind
// switchers, anything that promptForm can't model cleanly). Caller builds the
// body via `render(host)` and provides an `onSubmit` that returns the payload
// (or throws / returns null to keep the modal open). Resolves to
// `{confirmed, payload}` on submit, `{confirmed: false}` on cancel.
export function openModal({
    title, render, onSubmit, submitLabel = "Salvar",
    cancelLabel = "Cancelar", danger = false, size = "md",
}) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-shell");
        document.getElementById("shell-title").textContent = title;
        const host = document.getElementById("shell-body");
        host.innerHTML = "";
        const box = modal.querySelector(".modal-box");
        box.classList.remove("modal-box-md", "modal-box-lg", "modal-box-xl");
        box.classList.add(`modal-box-${size}`);
        // Caller fills the body — promptForm is for flat field lists; this
        // shell is for everything else (table editors, multi-section forms).
        render(host);

        const ok = document.getElementById("shell-ok");
        ok.textContent = submitLabel;
        ok.classList.toggle("danger", !!danger);
        ok.classList.toggle("primary", !danger);
        const cancel = document.getElementById("shell-cancel");
        cancel.textContent = cancelLabel;

        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            unwireBackdrop();
            ok.disabled = false;
            resolve(result);
        };
        const onOk = async () => {
            ok.disabled = true;
            try {
                const payload = onSubmit ? await onSubmit(host) : undefined;
                if (payload === null || payload === false) {
                    // Caller signaled validation failure — stay open.
                    ok.disabled = false;
                    return;
                }
                cleanup({ confirmed: true, payload });
            } catch (err) {
                ok.disabled = false;
                throw err;
            }
        };
        const onCancel = () => cleanup({ confirmed: false });
        const unwireBackdrop = wireBackdropDismiss(modal, onCancel);
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        modal.classList.remove("hidden");
    });
}


export function promptAssign({ title, description, users }) {
    return new Promise((resolve) => {
        const modal = document.getElementById("modal-assign");
        document.getElementById("assign-title").textContent = title;
        document.getElementById("assign-description").textContent = description;
        const sel = document.getElementById("assign-user");
        sel.innerHTML = "";
        for (const u of users) {
            const opt = document.createElement("option");
            opt.value = String(u.id);
            // Project-level role badge ("revisor" for reviewer members). Plain
            // operators get no badge — they're the default case for assigns.
            const tag = u.project_role === "reviewer" ? "revisor" : null;
            opt.textContent = u.username + (tag ? ` (${tag})` : "");
            sel.appendChild(opt);
        }
        const reason = document.getElementById("assign-reason"); reason.value = "";
        const ok = document.getElementById("assign-ok");
        const cancel = document.getElementById("assign-cancel");
        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            unwireBackdrop();
            resolve(result);
        };
        const onOk = () => cleanup({ confirmed: true, user_id: Number(sel.value), reason: reason.value.trim() });
        const onCancel = () => cleanup({ confirmed: false });
        const unwireBackdrop = wireBackdropDismiss(modal, onCancel);
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        modal.classList.remove("hidden");
        setTimeout(() => sel.focus(), 50);
    });
}
