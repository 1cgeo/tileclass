// Tests for frontend/js/admin-core.js — pure helpers behind the admin panel
// (status breakdown, daily series, axis ticks, formatting, row actions).
import { describe, it, expect } from "vitest";
import {
    statusBreakdown, STATUS_ORDER, fillDailySeries, addDaysIso, niceCeil, axisTicks,
    initials, avatarTone, fmtInt, fmtCompact, fmtPct, fmtEtaDays, relativeTime,
    tileRowActions, actionLabel, actionTone, pageWindow, trailingAverage,
} from "../../frontend/js/admin-core.js";

describe("statusBreakdown", () => {
    it("moves paused tiles out of in_progress/in_review into a virtual bucket", () => {
        const out = statusBreakdown(
            { pending: 10, in_progress: 5, in_review: 3, reviewed: 2 },
            { in_progress: 2, in_review: 1 },
        );
        const by = Object.fromEntries(out.map(s => [s.key, s.count]));
        expect(by).toEqual({ reviewed: 2, in_review: 2, in_progress: 3, paused: 3, pending: 10 });
    });

    it("keeps STATUS_ORDER, drops empty buckets and sums pct to 100", () => {
        const out = statusBreakdown({ problem: 1, reviewed: 3, pending: 4 });
        expect(out.map(s => s.key)).toEqual(["reviewed", "pending", "problem"]);
        expect(out.every(s => STATUS_ORDER.includes(s.key))).toBe(true);
        expect(out.reduce((a, s) => a + s.pct, 0)).toBeCloseTo(100, 6);
    });

    it("returns an empty list (no NaN) when there are no tiles", () => {
        expect(statusBreakdown({}, {})).toEqual([]);
    });

    it("never yields negative counts when paused exceeds the raw bucket", () => {
        const out = statusBreakdown({ in_progress: 1 }, { in_progress: 3 });
        expect(out.find(s => s.key === "in_progress")).toBeUndefined();
        expect(out.find(s => s.key === "paused").count).toBe(3);
    });
});

describe("fillDailySeries", () => {
    it("produces a contiguous, zero-filled, oldest-first window ending today", () => {
        const rows = [{ date: "2026-10-03", count: 4 }, { date: "2026-10-01", count: 2 }];
        const s = fillDailySeries(rows, "2026-10-03", 5);
        expect(s).toEqual([
            { date: "2026-09-29", count: 0 }, { date: "2026-09-30", count: 0 },
            { date: "2026-10-01", count: 2 }, { date: "2026-10-02", count: 0 },
            { date: "2026-10-03", count: 4 },
        ]);
    });

    it("ignores rows outside the window and handles month/year boundaries", () => {
        const s = fillDailySeries([{ date: "2025-12-01", count: 9 }], "2026-01-02", 3);
        expect(s.map(r => r.date)).toEqual(["2025-12-31", "2026-01-01", "2026-01-02"]);
        expect(s.every(r => r.count === 0)).toBe(true);
    });

    it("addDaysIso is timezone-independent", () => {
        expect(addDaysIso("2026-03-01", -1)).toBe("2026-02-28");
        expect(addDaysIso("2024-02-28", 1)).toBe("2024-02-29");
    });
});

describe("axis helpers", () => {
    it("niceCeil rounds up to 1/2/2.5/5 × 10^k", () => {
        expect(niceCeil(0)).toBe(1);
        expect(niceCeil(7)).toBe(10);
        expect(niceCeil(13)).toBe(20);
        expect(niceCeil(21)).toBe(25);
        expect(niceCeil(180)).toBe(200);
        expect(niceCeil(250)).toBe(250);
    });

    it("axisTicks uses integer steps (no 2.5 / 7.5 labels)", () => {
        expect(axisTicks(8, 4)).toEqual([0, 2, 4, 6, 8]);
        expect(axisTicks(9, 4)).toEqual([0, 5, 10, 15, 20]);
        expect(axisTicks(17, 4)).toEqual([0, 5, 10, 15, 20]);
        expect(axisTicks(130, 4)).toEqual([0, 50, 100, 150, 200]);
        expect(axisTicks(3, 4)).toEqual([0, 1, 2, 3, 4]);
    });

    it("axisTicks spans 0..max with even steps and covers the data", () => {
        const t = axisTicks(17, 4);
        expect(t[0]).toBe(0);
        expect(t[t.length - 1]).toBeGreaterThanOrEqual(17);
        const step = t[1] - t[0];
        t.forEach((v, i) => expect(v).toBeCloseTo(i * step, 6));
    });

    it("axisTicks of an all-zero series still has a usable scale", () => {
        const t = axisTicks(0, 4);
        expect(t[t.length - 1]).toBeGreaterThan(0);
    });
});

describe("formatting", () => {
    it("initials split usernames on dots/spaces", () => {
        expect(initials("ana.souza")).toBe("AS");
        expect(initials("Maria Clara")).toBe("MC");
        expect(initials("admin")).toBe("AD");
        expect(initials("")).toBe("?");
    });

    it("avatarTone is stable and within 1..6", () => {
        for (const n of ["ana", "bruno.lima", "x", ""]) {
            const t = avatarTone(n);
            expect(t).toBeGreaterThanOrEqual(1);
            expect(t).toBeLessThanOrEqual(6);
            expect(avatarTone(n)).toBe(t);
        }
    });

    it("pt-BR number formats", () => {
        expect(fmtInt(12345)).toBe("12.345");
        expect(fmtInt("x")).toBe("—");
        expect(fmtCompact(950)).toBe("950");
        expect(fmtCompact(12300)).toBe("12 mil");
        expect(fmtCompact(4_200_000)).toBe("4,2 mi");
        expect(fmtPct(61.73)).toBe("61,7%");
        expect(fmtPct(5, 0)).toBe("5%");
    });

    it("fmtEtaDays covers empty, sub-day, days and months", () => {
        expect(fmtEtaDays(null)).toBe("—");
        expect(fmtEtaDays(0)).toBe("—");
        expect(fmtEtaDays(0.4)).toBe("< 1 dia");
        expect(fmtEtaDays(1.2)).toBe("1 dia");
        expect(fmtEtaDays(17.6)).toBe("18 dias");
        expect(fmtEtaDays(90)).toBe("3 meses");
    });

    it("relativeTime buckets", () => {
        const now = Date.parse("2026-10-03T12:00:00Z");
        expect(relativeTime("2026-10-03T11:59:30Z", now)).toBe("agora");
        expect(relativeTime("2026-10-03T11:55:00Z", now)).toBe("há 5 min");
        expect(relativeTime("2026-10-03T09:00:00Z", now)).toBe("há 3 h");
        expect(relativeTime("2026-10-02T12:00:00Z", now)).toBe("há 1 dia");
        expect(relativeTime("2026-09-20T12:00:00Z", now)).toBe("há 13 dias");
        expect(relativeTime(null, now)).toBe("");
        expect(relativeTime("garbage", now)).toBe("");
    });
});

describe("tileRowActions", () => {
    it("mirrors the backend state machine gates", () => {
        expect(tileRowActions({ status: "pending" })).toEqual(["assign", "block"]);
        expect(tileRowActions({ status: "classified" })).toEqual(["assign", "block"]);
        expect(tileRowActions({ status: "reviewed" })).toEqual(["rereview", "block"]);
        expect(tileRowActions({ status: "in_progress" })).toEqual(["pause", "unassign"]);
        expect(tileRowActions({ status: "in_review", paused_at: "x" })).toEqual(["unassign"]);
        expect(tileRowActions({ status: "blocked" })).toEqual(["unblock"]);
        expect(tileRowActions({ status: "problem" })).toEqual([]);
        expect(tileRowActions(null)).toEqual([]);
    });

    it("never offers block on states the backend rejects", () => {
        for (const status of ["in_progress", "in_review", "problem", "blocked"]) {
            expect(tileRowActions({ status })).not.toContain("block");
        }
    });
});

describe("history labels", () => {
    it("translates known actions and passes unknown through", () => {
        expect(actionLabel("classify")).toBe("Classificou");
        expect(actionLabel("report_problem")).toBe("Reportou problema");
        expect(actionLabel("something_new")).toBe("something_new");
        expect(actionTone("review")).toBe("ok");
        expect(actionTone("report_problem")).toBe("err");
        expect(actionTone("pause")).toBe("paused");
        expect(actionTone("assign_classify")).toBe("neutral");
    });
});

describe("pageWindow", () => {
    it("lists every page when there are few", () => {
        expect(pageWindow(0, 3)).toEqual([0, 1, 2]);
    });

    it("keeps first/last/current±1 with ellipses", () => {
        expect(pageWindow(10, 20)).toEqual([0, "…", 9, 10, 11, "…", 19]);
        expect(pageWindow(0, 20)).toEqual([0, 1, 2, 3, "…", 19]);
        expect(pageWindow(19, 20)).toEqual([0, "…", 16, 17, 18, 19]);
    });
});

describe("trailingAverage", () => {
    it("averages the last n entries", () => {
        const s = [1, 2, 3, 4, 5, 6, 7, 8].map((count) => ({ count }));
        expect(trailingAverage(s, 7)).toBeCloseTo(5, 6);
        expect(trailingAverage([], 7)).toBe(0);
    });
});
