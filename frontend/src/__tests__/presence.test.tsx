import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePresence } from "@/lib/live";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("usePresence (lets free-tier servers sleep when nobody is watching)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });
  afterEach(() => vi.useRealTimers());

  it("goes away after the tab has been hidden for a while, and back on return", () => {
    const { result } = renderHook(() => usePresence(10_000, 3_000));
    expect(result.current).toBe(true);
    act(() => setVisibility("hidden"));
    act(() => void vi.advanceTimersByTime(2_999));
    expect(result.current).toBe(true);
    act(() => void vi.advanceTimersByTime(1));
    expect(result.current).toBe(false);
    act(() => setVisibility("visible"));
    expect(result.current).toBe(true);
  });

  it("goes away after the idle period without interaction; any input resumes", () => {
    const { result } = renderHook(() => usePresence(10_000, 3_000));
    act(() => void vi.advanceTimersByTime(9_000));
    act(() => void window.dispatchEvent(new Event("keydown")));
    act(() => void vi.advanceTimersByTime(9_000));
    expect(result.current).toBe(true);
    act(() => void vi.advanceTimersByTime(1_000));
    expect(result.current).toBe(false);
    act(() => void window.dispatchEvent(new Event("pointerdown")));
    expect(result.current).toBe(true);
  });
});
