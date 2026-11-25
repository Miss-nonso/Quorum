import type { Metadata } from "next";
import { Suspense } from "react";

import { RunDetailRoute } from "@/views/runs";

export const metadata: Metadata = { title: "Run" };

export default function Page() {
  return (
    <Suspense>
      <RunDetailRoute />
    </Suspense>
  );
}
