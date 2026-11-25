import type { Metadata } from "next";
import { Suspense } from "react";

import { DemoView } from "@/views/demo";

export const metadata: Metadata = { title: { absolute: "Quorum: kill a worker, watch nothing get lost" } };

export default function Page() {
  return (
    <Suspense>
      <DemoView />
    </Suspense>
  );
}
