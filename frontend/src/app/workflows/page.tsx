import type { Metadata } from "next";
import { Suspense } from "react";

import { WorkflowsView } from "@/views/workflows";

export const metadata: Metadata = { title: "Workflows" };

export default function Page() {
  return (
    <Suspense>
      <WorkflowsView />
    </Suspense>
  );
}
