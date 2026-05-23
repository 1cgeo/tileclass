// Tests for promptForm in admin/modals.js. Renders the actual modal chrome
// (#modal-form) into jsdom and drives it via the same DOM the real admin
// uses, so the renderer + validator stay covered.
import { describe, it, expect, beforeEach } from "vitest";
import { promptForm } from "../../frontend/js/admin/modals.js";

function installModalChrome() {
    document.body.innerHTML = `
        <div id="modal-form" class="modal hidden">
            <div class="modal-box">
                <h2 id="form-title"></h2>
                <p id="form-description" class="hint"></p>
                <div id="form-fields"></div>
                <div class="modal-actions">
                    <button id="form-cancel">Cancelar</button>
                    <button id="form-ok" class="primary">Salvar</button>
                </div>
            </div>
        </div>`;
}

describe("promptForm", () => {
    beforeEach(() => installModalChrome());

    it("renders one control per field with the right type and ids", async () => {
        promptForm({
            title: "T", fields: [
                { key: "u", label: "Usuário", type: "text" },
                { key: "p", label: "Senha", type: "password" },
                { key: "r", label: "Papel", type: "select",
                  options: [{ value: "a", label: "A" }, { value: "b", label: "B" }] },
                { key: "n", label: "Notas", type: "textarea" },
            ],
        });
        expect(document.getElementById("form-field-u").type).toBe("text");
        expect(document.getElementById("form-field-p").type).toBe("password");
        const sel = document.getElementById("form-field-r");
        expect(sel.tagName).toBe("SELECT");
        expect(sel.options.length).toBe(2);
        expect(document.getElementById("form-field-n").tagName).toBe("TEXTAREA");
    });

    it("appends ' *' to required field labels", () => {
        promptForm({
            title: "T", fields: [
                { key: "u", label: "Usuário", type: "text", required: true },
                { key: "n", label: "Notas", type: "text" },
            ],
        });
        const labels = document.querySelectorAll(".modal-form-label");
        expect(labels[0].textContent).toBe("Usuário *");
        expect(labels[1].textContent).toBe("Notas");
    });

    it("OK button is disabled until required fields are filled", () => {
        promptForm({
            title: "T", fields: [
                { key: "u", label: "Usuário", type: "text", required: true },
            ],
        });
        const ok = document.getElementById("form-ok");
        expect(ok.disabled).toBe(true);
        const input = document.getElementById("form-field-u");
        input.value = "joao";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        expect(ok.disabled).toBe(false);
    });

    it("enforces minLength before enabling OK", () => {
        promptForm({
            title: "T", fields: [
                { key: "p", label: "Senha", type: "password",
                  required: true, minLength: 6 },
            ],
        });
        const ok = document.getElementById("form-ok");
        const input = document.getElementById("form-field-p");
        input.value = "abc";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        expect(ok.disabled).toBe(true);
        input.value = "abc123";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        expect(ok.disabled).toBe(false);
    });

    it("resolves {confirmed:true, values} on OK click", async () => {
        const p = promptForm({
            title: "T", fields: [
                { key: "u", label: "Usuário", type: "text", defaultValue: "joao" },
                { key: "r", label: "Papel", type: "select",
                  defaultValue: "admin",
                  options: [{ value: "operator", label: "Op" },
                            { value: "admin", label: "Adm" }] },
            ],
        });
        document.getElementById("form-ok").click();
        const r = await p;
        expect(r).toEqual({ confirmed: true, values: { u: "joao", r: "admin" } });
    });

    it("resolves {confirmed:false} on Cancel click", async () => {
        const p = promptForm({
            title: "T", fields: [{ key: "u", label: "U", type: "text" }],
        });
        document.getElementById("form-cancel").click();
        const r = await p;
        expect(r).toEqual({ confirmed: false });
    });

    it("hides the modal on resolve so reopen is clean", async () => {
        const modal = document.getElementById("modal-form");
        const p = promptForm({
            title: "T", fields: [{ key: "u", label: "U", type: "text" }],
        });
        expect(modal.classList.contains("hidden")).toBe(false);
        document.getElementById("form-cancel").click();
        await p;
        expect(modal.classList.contains("hidden")).toBe(true);
    });

    it("Enter in a single-line input submits", async () => {
        const p = promptForm({
            title: "T", fields: [
                { key: "u", label: "U", type: "text", defaultValue: "joao" },
            ],
        });
        const input = document.getElementById("form-field-u");
        input.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter", bubbles: true,
        }));
        const r = await p;
        expect(r.confirmed).toBe(true);
        expect(r.values.u).toBe("joao");
    });

    it("Enter in a textarea is treated as newline, not submit", () => {
        promptForm({
            title: "T", fields: [
                { key: "n", label: "N", type: "textarea", defaultValue: "hi" },
            ],
        });
        const ta = document.getElementById("form-field-n");
        const modal = document.getElementById("modal-form");
        ta.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter", bubbles: true,
        }));
        // Modal must still be open — submit shouldn't have fired.
        expect(modal.classList.contains("hidden")).toBe(false);
    });

    it("Ctrl+Enter in a textarea submits", async () => {
        const p = promptForm({
            title: "T", fields: [
                { key: "n", label: "N", type: "textarea", defaultValue: "hi" },
            ],
        });
        const ta = document.getElementById("form-field-n");
        ta.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter", ctrlKey: true, bubbles: true,
        }));
        const r = await p;
        expect(r).toEqual({ confirmed: true, values: { n: "hi" } });
    });

    it("OK click while disabled does not resolve", async () => {
        // Two events race: a click on a still-disabled button shouldn't fire
        // because handlers check validate(). We assert by clicking, then
        // filling, then clicking again — only the second should resolve.
        const p = promptForm({
            title: "T", fields: [
                { key: "u", label: "U", type: "text", required: true },
            ],
        });
        let resolved = false;
        p.then(() => { resolved = true; });
        const ok = document.getElementById("form-ok");
        ok.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        await Promise.resolve();
        expect(resolved).toBe(false);
        const input = document.getElementById("form-field-u");
        input.value = "ok";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        ok.click();
        const r = await p;
        expect(r.confirmed).toBe(true);
    });
});
