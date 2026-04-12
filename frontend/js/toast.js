// Toast messages.
let timer = null;
export function showToast(msg, type = "info", ms = 2500) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = `toast ${type}`;
    el.classList.remove("hidden");
    clearTimeout(timer);
    timer = setTimeout(() => el.classList.add("hidden"), ms);
}
