import type { Metadata } from "next";
import { Suspense } from "react";

import { WorkersView } from "@/views/fleet";

export const metadata: Metadata = { title: "Workers" };

export default function Page() {
  return (
    <Suspense>
      <WorkersView />
    </Suspense>
  );
}
