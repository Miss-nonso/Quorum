import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AppShell } from "@/components/app-shell";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Quorum", template: "%s · Quorum" },
  description:
    "A distributed job scheduling and workflow engine built on PostgreSQL. Kill a worker and watch it recover.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#12151c" },
  ],
};

// Runs before first paint: apply the saved or system theme, and whether the demo guide was
// dismissed, so neither causes a flash or a layout shift.
const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem("quorum-theme");if(t!=="light"&&t!=="dark"){t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"}document.documentElement.dataset.theme=t;document.documentElement.dataset.guide=localStorage.getItem("quorum-guide-done")==="1"?"done":"todo"}catch(e){}})();`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
