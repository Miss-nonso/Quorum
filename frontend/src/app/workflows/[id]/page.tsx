import type { Metadata } from "next";
import { Suspense } from "react";

import { WorkflowDetailRoute } from "@/views/workflows";

export const metadata: Metadata = { title: "Workflow" };

export default function Page() {
  return (
    <Suspense>
      <WorkflowDetailRoute />
    </Suspense>
  );
}
