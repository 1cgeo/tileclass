// Toast messages.
//
// Queue: messages arriving while one is already showing line up FIFO so a
// rapid sequence (e.g. "tile salvo" then "próximo carregado") doesn't get
// truncated by the second call. Each entry keeps its own type/timeout.
// Brief gap between toasts so a SR/visual transition fires — without it,
// back-to-back messages look like one rewrite.
const HIDE_GAP_MS = 120;
let _timer = null;
const _queue = [];
let _showing = false;

export function showToast(msg, type = "info", ms = 2500) {
    _queue.push({ msg, type, ms });
    if (!_showing) _next();
}

function _next() {
    const el = document.getElementById("toast");
    if (!el) return;
    if (_queue.length === 0) {
        _showing = false;
        el.classList.add("hidden");
        return;
    }
    const { msg, type, ms } = _queue.shift();
    _showing = true;
    el.textContent = msg;
    el.className = `toast ${type}`;
    el.classList.remove("hidden");
    clearTimeout(_timer);
    _timer = setTimeout(() => {
        el.classList.add("hidden");
        setTimeout(_next, HIDE_GAP_MS);
    }, ms);
}
