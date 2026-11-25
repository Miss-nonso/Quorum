"use client";

import { useEffect, useState } from "react";

/**
 * The current time, refreshed every `intervalMs` (for "3 s ago" and running
 * durations). An interval of 0 means "don't tick".
 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (intervalMs <= 0) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
