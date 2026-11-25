import type { Metadata } from "next";
import { Suspense } from "react";

import { RunsView } from "@/views/runs";

export const metadata: Metadata = { title: "History" };

export default function Page() {
  return (
    <Suspense>
      <RunsView />
    </Suspense>
  );
}
