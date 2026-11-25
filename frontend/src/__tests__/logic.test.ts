import { describe, expect, it } from "vitest";

import { layoutDag } from "@/lib/dag-layout";
import { formatDuration, formatRelative, runDuration } from "@/lib/format";
import { narrate } from "@/lib/narrate";
import { attemptsFromEvents, timelineBounds } from "@/lib/timeline";
import type { RunEvent } from "@/lib/types";

const SAMPLE = [
  { key: "fetch", depends_on: [] },
  { key: "validate", depends_on: ["fetch"] },
  { key: "transform", depends_on: ["validate"] },
  { key: "store", depends_on: ["transform"] },
  { key: "report", depends_on: ["transform"] },
  { key: "notify", depends_on: ["store", "report"] },
];

describe("layoutDag", () => {
  it("puts every node to the right of everything it depends on", () => {
    const pos = new Map(layoutDag(SAMPLE).map((p) => [p.key, p]));
    for (const n of SAMPLE)
      for (const d of n.depends_on) expect(pos.get(d)!.x).toBeLessThan(pos.get(n.key)!.x);
  });

  it("stacks parallel branches in one column without overlap", () => {
    const layout = layoutDag(SAMPLE);
    const branch = layout.filter((p) => p.key === "store" || p.key === "report");
    expect(new Set(branch.map((p) => p.column)).size).toBe(1);
    expect(branch[0].y).not.toBe(branch[1].y);
    const slots = layout.map((p) => `${p.x},${p.y}`);
    expect(new Set(slots).size).toBe(slots.length);
  });

  it("is independent of declaration order", () => {
    const shuffled = [...SAMPLE].reverse();
    const a = Object.fromEntries(layoutDag(SAMPLE).map((p) => [p.key, p.column]));
    const b = Object.fromEntries(layoutDag(shuffled).map((p) => [p.key, p.column]));
    expect(b).toEqual(a);
  });

  it("rejects cycles instead of looping forever", () => {
    expect(() =>
      layoutDag([
        { key: "a", depends_on: ["b"] },
        { key: "b", depends_on: ["a"] },
      ]),
    ).toThrow(/cycle/);
  });
});

const ev = (
  id: number,
  type: string,
  ms: number,
  payload: Record<string, unknown> = {},
  task = "t1",
  node = "fetch",
): RunEvent => ({
  id,
  run_id: "r1",
  task_id: task,
  node_key: node,
  type,
  payload,
  at: new Date(Date.UTC(2026, 0, 1, 0, 0, 0, ms)).toISOString(),
});

describe("attemptsFromEvents", () => {
  it("builds one bar per attempt, ending at the right event", () => {
    const attempts = attemptsFromEvents([
      ev(1, "task_claimed", 0, { attempt: 1, worker_id: "w-a" }),
      ev(2, "lease_expired", 400),
      ev(3, "task_retry_scheduled", 400), // follow-up to the expiry: no second bar
      ev(4, "task_claimed", 600, { attempt: 2, worker_id: "w-b" }),
      ev(5, "task_succeeded", 900),
    ]);
    expect(attempts.map((a) => [a.attempt, a.outcome, a.end - a.start, a.workerId])).toEqual([
      [1, "lease_expired", 400, "w-a"],
      [2, "succeeded", 300, "w-b"],
    ]);
  });

  it("keeps an unfinished attempt open until now", () => {
    const now = Date.UTC(2026, 0, 1, 0, 0, 5);
    const [a] = attemptsFromEvents([ev(1, "task_claimed", 0, { attempt: 1 })], now);
    expect(a.outcome).toBe("running");
    expect(a.end).toBe(now);
  });

  it("tracks parallel tasks independently", () => {
    const attempts = attemptsFromEvents([
      ev(1, "task_claimed", 0, {}, "t1", "store"),
      ev(2, "task_claimed", 10, {}, "t2", "report"),
      ev(3, "task_succeeded", 50, {}, "t2", "report"),
      ev(4, "task_succeeded", 80, {}, "t1", "store"),
    ]);
    expect(attempts.map((a) => [a.nodeKey, a.end - a.start])).toEqual([
      ["store", 80],
      ["report", 40],
    ]);
    expect(timelineBounds(attempts, 0)[1] - timelineBounds(attempts, 0)[0]).toBe(1000);
  });
});

describe("narrate", () => {
  it("explains failure recovery in plain language", () => {
    const text = narrate(ev(1, "lease_expired", 0, { worker_id: "abcdef1234" })).text;
    expect(text).toContain("worker abcdef12 went silent");
    expect(text).toContain("back in the queue");
  });

  it("marks outcomes with a tone so they can be styled consistently", () => {
    expect(narrate(ev(1, "task_succeeded", 0)).tone).toBe("good");
    expect(narrate(ev(1, "task_dead_lettered", 0)).tone).toBe("bad");
    expect(narrate(ev(1, "stale_write_rejected", 0)).tone).toBe("warn");
    expect(narrate(ev(1, "something_new", 0)).text).toBe("something new (fetch)");
  });
});

describe("format", () => {
  it.each([
    [250, "250 ms"],
    [1500, "1.5 s"],
    [42_000, "42 s"],
    [125_000, "2 m 05 s"],
    [3_780_000, "1 h 03 m"],
    [-1, "—"],
  ])("formatDuration(%i) = %s", (ms, text) => expect(formatDuration(ms)).toBe(text));

  it("formats relative times", () => {
    const now = Date.UTC(2026, 0, 1, 12);
    expect(formatRelative(new Date(now - 2_000).toISOString(), now)).toBe("just now");
    expect(formatRelative(new Date(now - 90_000).toISOString(), now)).toBe("1 min ago");
    expect(formatRelative(new Date(now + 30_000).toISOString(), now)).toBe("in 30 s");
    expect(formatRelative(null, now)).toBe("—");
  });

  it("measures running runs up to now", () => {
    expect(runDuration("2026-01-01T00:00:00Z", null, Date.UTC(2026, 0, 1, 0, 0, 3))).toBe(3000);
    expect(runDuration(null, null)).toBeNull();
  });
});
