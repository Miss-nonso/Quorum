"use client";

/**
 * Heavy visual components (React Flow) load on demand, behind a
 * skeleton of the same size, so they stay out of every page's initial
 * JavaScript and cause no layout shift when they arrive.
 */
import dynamic from "next/dynamic";

import { Skeleton } from "./states";

export const DagView = dynamic(() => import("./dag-view").then((m) => m.DagView), {
  ssr: false,
  loading: () => <Skeleton className="h-[300px] w-full rounded-xl" />,
});
