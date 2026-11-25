import type { Metadata } from "next";
import { Suspense } from "react";

import { PageHeader } from "@/components/app-shell";
import { Skeleton } from "@/components/states";
import { RunsView } from "@/views/runs";

export const metadata: Metadata = { title: "History" };

export default function Page() {
  return (
    <>
      {/* Static: prerendered, so it paints before any JavaScript runs. */}
      <PageHeader
        title="History"
        description="Every run, newest first. Filter by outcome or by what started it."
      />
      {/* The filters live in the URL (runtime data), so this part streams in. */}
      <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}>
        <RunsView />
      </Suspense>
    </>
  );
}
