// Confirm + assign modals shared across admin tabs (tiles, users, map, viewer).
// Pure DOM — no admin state, so the rest of admin/ can import without cycles.

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
        const preview = ids.slice(0, 20);
        list.textContent = `IDs: ${preview.join(", ")}${ids.length > 20 ? ` … (+${ids.length - 20})` : ""}`;
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
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        reason.addEventListener("input", updateOkState);
        modal.classList.remove("hidden");
        setTimeout(() => reason.focus(), 50);
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
            const tags = [];
            if (u.role === "admin") tags.push("admin");
            else if (u.can_review) tags.push("revisor");
            opt.textContent = u.username + (tags.length ? ` (${tags.join(", ")})` : "");
            sel.appendChild(opt);
        }
        const reason = document.getElementById("assign-reason"); reason.value = "";
        const ok = document.getElementById("assign-ok");
        const cancel = document.getElementById("assign-cancel");
        const cleanup = (result) => {
            modal.classList.add("hidden");
            ok.removeEventListener("click", onOk);
            cancel.removeEventListener("click", onCancel);
            resolve(result);
        };
        const onOk = () => cleanup({ confirmed: true, user_id: Number(sel.value), reason: reason.value.trim() });
        const onCancel = () => cleanup({ confirmed: false });
        ok.addEventListener("click", onOk);
        cancel.addEventListener("click", onCancel);
        modal.classList.remove("hidden");
        setTimeout(() => sel.focus(), 50);
    });
}
