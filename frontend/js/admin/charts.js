// Admin charts. No libraries: SVG for the time series, HTML for meters/bars.
// Colors come from CSS classes bound to tokens (tokens.css), so a theme switch
// re-colors everything without re-rendering. Data colors (project class
// palette) are the only inline colors.
import { h } from "./ui.js";
import { statusLabel } from "../utils.js";
import { axisTicks, fmtInt, fmtPct } from "../admin-core.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function s(tag, attrs = {}, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, String(v));
    for (const c of children) if (c) el.appendChild(c);
    return el;
}

// ---- Status distribution: 100% stacked bar + legend --------------------------
// segments: [{key, count, pct}] from admin-core.statusBreakdown.
export function statusStack(segments, { onSelect } = {}) {
    const total = segments.reduce((a, b) => a + b.count, 0);
    const summary = segments.map(sg => `${statusLabel(sg.key)}: ${sg.count}`).join(", ");
    const bar = h("div", { class: "stack-bar", role: "img", "aria-label": `Distribuição por status — ${summary}` });
    for (const sg of segments) {
        const seg = h("span", {
            class: `stack-seg st-${sg.key}`,
            title: `${statusLabel(sg.key)}: ${fmtInt(sg.count)} (${fmtPct(sg.pct)})`,
        });
        seg.style.flexGrow = String(sg.count);
        bar.appendChild(seg);
    }
    if (!total) bar.appendChild(h("span", { class: "stack-seg stack-empty" }));
    const legend = h("ul", { class: "stack-legend" });
    for (const sg of segments) {
        const item = h("li", { class: "stack-legend-item" },
            h("span", { class: `legend-dot st-${sg.key}` }),
            h("span", { class: "stack-legend-label", text: statusLabel(sg.key) }),
            h("span", { class: "stack-legend-value tabular", text: fmtInt(sg.count) }),
            h("span", { class: "stack-legend-pct tabular", text: fmtPct(sg.pct, 0) }));
        if (onSelect) {
            item.classList.add("is-link");
            item.tabIndex = 0;
            item.title = `Ver tiles: ${statusLabel(sg.key)}`;
            item.addEventListener("click", () => onSelect(sg.key));
            item.addEventListener("keydown", (ev) => { if (ev.key === "Enter") onSelect(sg.key); });
        }
        legend.appendChild(item);
    }
    return h("div", { class: "stack" }, bar, legend);
}

// ---- Horizontal bars (class distribution) ------------------------------------
// items: [{name, color, value, pct, valueText}]
export function hbarList(items) {
    const max = Math.max(1, ...items.map(i => i.value));
    const list = h("ul", { class: "hbar-list" });
    for (const it of items) {
        const fill = h("span", { class: "hbar-fill" });
        fill.style.width = `${(100 * it.value) / max}%`;
        fill.style.background = it.color;
        list.appendChild(h("li", { class: "hbar-row", title: `${it.name}: ${it.valueText} (${fmtPct(it.pct)})` },
            h("span", { class: "hbar-head" },
                h("span", { class: "hbar-swatch", style: { background: it.color } }),
                h("span", { class: "hbar-name", text: it.name }),
                h("span", { class: "hbar-value tabular", text: it.valueText }),
                h("span", { class: "hbar-pct tabular", text: fmtPct(it.pct) })),
            h("span", { class: "hbar-track" }, fill)));
    }
    return list;
}

// ---- Daily bar chart (SVG) ----------------------------------------------------
// series: [{date: "YYYY-MM-DD", count}] oldest → newest. Re-renders on resize
// so text never stretches; hover shows an HTML tooltip.
const DATE_FMT = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", timeZone: "UTC" });
const DATE_LONG = new Intl.DateTimeFormat("pt-BR", { weekday: "short", day: "2-digit", month: "long", timeZone: "UTC" });
const fmtDay = (iso, long = false) => (long ? DATE_LONG : DATE_FMT).format(new Date(`${iso}T00:00:00Z`)).replace(".", "");

export function dailyBarChart(series, { height = 220, label = "Tiles por dia", unit = "tiles" } = {}) {
    const wrap = h("div", { class: "chart chart-daily" });
    const tip = h("div", { class: "chart-tip hidden", role: "status" });
    const host = h("div", { class: "chart-svg-host" });
    wrap.append(host, tip);
    const total = series.reduce((a, r) => a + r.count, 0);
    const desc = `${label}: ${fmtInt(total)} ${unit} em ${series.length} dias.`;

    const draw = () => {
        const width = Math.max(320, Math.floor(host.clientWidth || 640));
        host.textContent = "";
        const m = { top: 12, right: 8, bottom: 26, left: 34 };
        const iw = width - m.left - m.right;
        const ih = height - m.top - m.bottom;
        const max = Math.max(0, ...series.map(r => r.count));
        const ticks = axisTicks(max, 4);
        const top = ticks[ticks.length - 1] || 1;
        const y = (v) => m.top + ih - (v / top) * ih;
        const band = iw / series.length;
        const bw = Math.max(3, Math.min(22, band - 4));
        const svg = s("svg", {
            width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
            "aria-label": desc, class: "chart-svg",
        });
        svg.appendChild(s("title", {}, document.createTextNode(desc)));
        const grid = s("g", { class: "chart-grid" });
        for (const t of ticks) {
            const yy = Math.round(y(t)) + 0.5;
            grid.appendChild(s("line", { x1: m.left, x2: width - m.right, y1: yy, y2: yy }));
            const tl = s("text", { x: m.left - 8, y: yy + 3.5, class: "chart-axis-label", "text-anchor": "end" });
            tl.textContent = fmtInt(t);
            grid.appendChild(tl);
        }
        svg.appendChild(grid);
        const bars = s("g", { class: "chart-bars" });
        const hits = s("g", { class: "chart-hits" });
        series.forEach((r, i) => {
            const cx = m.left + band * i + band / 2;
            const x0 = cx - bw / 2;
            const y0 = y(r.count);
            const bh = m.top + ih - y0;
            if (r.count > 0) {
                const rad = Math.min(4, bw / 2, bh);
                // Rounded top, square baseline.
                const d = `M${x0},${m.top + ih}V${y0 + rad}Q${x0},${y0} ${x0 + rad},${y0}`
                    + `H${x0 + bw - rad}Q${x0 + bw},${y0} ${x0 + bw},${y0 + rad}V${m.top + ih}Z`;
                bars.appendChild(s("path", { d, class: `chart-bar${i === series.length - 1 ? " is-today" : ""}`, "data-i": i }));
            }
            const hit = s("rect", { x: m.left + band * i, y: m.top, width: band, height: ih, class: "chart-hit", "data-i": i });
            hits.appendChild(hit);
            const showLabel = i === series.length - 1 || (series.length - 1 - i) % 7 === 0;
            if (showLabel) {
                const tl = s("text", { x: cx, y: height - 8, class: "chart-axis-label", "text-anchor": "middle" });
                tl.textContent = i === series.length - 1 ? "hoje" : fmtDay(r.date);
                svg.appendChild(tl);
            }
        });
        svg.append(bars, s("line", {
            class: "chart-baseline", x1: m.left, x2: width - m.right,
            y1: m.top + ih + 0.5, y2: m.top + ih + 0.5,
        }), hits);
        host.appendChild(svg);

        const show = (i) => {
            const r = series[i];
            bars.querySelectorAll(".chart-bar").forEach(b => b.classList.toggle("is-active", b.dataset.i === String(i)));
            tip.textContent = "";
            tip.append(h("span", { class: "chart-tip-date", text: fmtDay(r.date, true) }),
                h("strong", { class: "tabular", text: `${fmtInt(r.count)} ${unit}` }));
            tip.classList.remove("hidden");
            const cx = m.left + band * i + band / 2;
            const tw = tip.offsetWidth;
            const left = Math.max(0, Math.min(width - tw, cx - tw / 2));
            tip.style.left = `${left}px`;
            tip.style.top = `${Math.max(0, y(r.count) - tip.offsetHeight - 10)}px`;
        };
        hits.addEventListener("mousemove", (ev) => {
            const i = Number(ev.target.getAttribute?.("data-i"));
            if (Number.isInteger(i)) show(i);
        });
        svg.addEventListener("mouseleave", () => {
            tip.classList.add("hidden");
            bars.querySelectorAll(".chart-bar.is-active").forEach(b => b.classList.remove("is-active"));
        });
    };

    // Draw once attached (needs a width), then on resize.
    let lastW = 0;
    const ro = new ResizeObserver(() => {
        const w = Math.floor(host.clientWidth);
        if (w && w !== lastW) { lastW = w; draw(); }
    });
    ro.observe(host);
    // Table fallback for screen readers.
    const table = h("table", { class: "sr-only" },
        h("caption", { text: label }),
        h("thead", {}, h("tr", {}, h("th", { text: "Dia" }), h("th", { text: unit }))),
        h("tbody", {}, series.map(r => h("tr", {}, h("td", { text: r.date }), h("td", { text: String(r.count) })))));
    wrap.appendChild(table);
    return wrap;
}
