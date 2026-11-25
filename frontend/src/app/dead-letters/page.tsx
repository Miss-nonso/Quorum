import type { Metadata } from "next";
import { Suspense } from "react";

import { DeadLettersView } from "@/views/fleet";

export const metadata: Metadata = { title: "Dead letters" };

export default function Page() {
  return (
    <Suspense>
      <DeadLettersView />
    </Suspense>
  );
}
