"use client";

import {
  Activity,
  Boxes,
  FlaskConical,
  History,
  LayoutDashboard,
  Moon,
  Server,
  Skull,
  Sun,
  Workflow as WorkflowIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useSyncExternalStore, type ReactNode } from "react";

import { useLive, type ConnectionState } from "@/lib/live";
import { useStats } from "@/lib/queries";

const NAV = [
  { href: "/", label: "Demo", icon: FlaskConical },
  { href: "/overview", label: "Overview", icon: LayoutDashboard },
  { href: "/workflows", label: "Workflows", icon: WorkflowIcon },
  { href: "/runs", label: "History", icon: History },
  { href: "/workers", label: "Workers", icon: Server },
  { href: "/dead-letters", label: "Dead letters", icon: Skull },
];

const CONNECTION: Record<ConnectionState, { label: string; tone: string; hint: string }> = {
  connecting: { label: "Connecting", tone: "ready", hint: "Opening the live event stream…" },
  live: { label: "Live", tone: "succeeded", hint: "Receiving live updates." },
  reconnecting: {
    label: "Reconnecting",
    tone: "ready",
    hint: "Stream dropped; retrying. No events will be lost.",
  },
  offline: { label: "Offline", tone: "failed", hint: "Can't reach the API. Data may be stale; retrying." },
  paused: {
    label: "Paused",
    tone: "pending",
    hint: "Live updates pause while you're away, so the free demo server can sleep. Move the mouse or press a key to resume.",
  },
};

export function ConnectionIndicator() {
  const { state } = useLive();
  const meta = CONNECTION[state];
  return (
    <span
      role="status"
      aria-live="polite"
      title={meta.hint}
      className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-2.5 py-1 text-xs font-medium"
    >
      <span className="relative flex size-2">
        {state === "live" ? (
          <span
            className="absolute inline-flex size-full animate-ping rounded-full opacity-60 motion-reduce:hidden"
            style={{ backgroundColor: `var(--st-${meta.tone})` }}
          />
        ) : null}
        <span
          className="relative inline-flex size-2 rounded-full"
          style={{ backgroundColor: `var(--st-${meta.tone})` }}
        />
      </span>
      <span>{meta.label}</span>
      <span className="sr-only">: {meta.hint}</span>
    </span>
  );
}

// The theme lives on <html data-theme> (set before paint by the layout script);
// subscribe to it rather than copying it into React state.
function subscribeTheme(onChange: () => void) {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => observer.disconnect();
}
const readTheme = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");

function ThemeToggle() {
  const theme = useSyncExternalStore(subscribeTheme, readTheme, () => null);
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="rounded-lg p-2 text-muted hover:bg-surface-2 hover:text-text"
      aria-label={theme ? `Switch to ${next} theme` : "Toggle theme"}
      onClick={() => {
        document.documentElement.dataset.theme = next;
        try {
          localStorage.setItem("quorum-theme", next);
        } catch {
          // storage unavailable (private mode): the choice lasts for this page only
        }
      }}
    >
      {theme === "dark" ? (
        <Sun aria-hidden="true" className="size-4" />
      ) : (
        <Moon aria-hidden="true" className="size-4" />
      )}
    </button>
  );
}

function DlqCount() {
  const { data } = useStats();
  const n = data?.open_dead_letters ?? 0;
  if (!n) return null;
  return (
    <span
      className="ml-auto rounded-full px-1.5 text-xs font-semibold"
      style={{ color: "var(--st-dead)", backgroundColor: "var(--st-dead-bg)" }}
    >
      {n}
      <span className="sr-only"> open</span>
    </span>
  );
}

function NavList({ pathname }: { pathname: string | null }) {
  const isActive = (href: string) =>
    pathname !== null && (href === "/" ? pathname === "/" : pathname.startsWith(href));
  return (
    <nav aria-label="Main" className="mx-auto max-w-7xl overflow-x-auto px-2">
      <ul className="flex gap-1">
        {NAV.map(({ href, label, icon: Icon }) => (
          <li key={href}>
            <Link
              href={href}
              aria-current={isActive(href) ? "page" : undefined}
              className={`flex items-center gap-2 border-b-2 px-3 py-2.5 text-sm whitespace-nowrap transition-colors ${
                isActive(href)
                  ? "border-accent font-medium text-text"
                  : "border-transparent text-muted hover:text-text"
              }`}
            >
              <Icon aria-hidden="true" className="size-4" />
              {label}
              {href === "/dead-letters" ? <DlqCount /> : null}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** The pathname is runtime data, so it streams in under Suspense; the fallback is the same nav, unhighlighted. */
function MainNav() {
  return <NavList pathname={usePathname()} />;
}

/** Free hosting sleeps when idle; tell the first visitor why the page is waiting. */
function WakingBanner() {
  const { wakingUp } = useLive();
  if (!wakingUp) return null;
  return (
    <div
      role="status"
      className="border-b px-4 py-2.5 text-center text-sm"
      style={{ borderColor: "var(--st-ready)", backgroundColor: "var(--st-ready-bg)" }}
    >
      <strong>Waking up the demo server…</strong> It runs on free hosting, which sleeps when nobody is using
      it. The first visit takes up to a minute; this page updates by itself.
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-dvh">
      <a
        href="#main"
        className="sr-only z-50 rounded-lg bg-accent px-3 py-2 text-accent-fg focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-30 border-b border-border bg-surface/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-4 px-4">
          <Link href="/" className="flex items-center gap-2 font-semibold">
            <span className="grid size-7 place-items-center rounded-lg bg-accent text-accent-fg">
              <Activity aria-hidden="true" className="size-4" />
            </span>
            Quorum
          </Link>
          <span className="hidden text-sm text-muted md:inline">
            Distributed workflow engine on PostgreSQL
          </span>
          <div className="ml-auto flex items-center gap-2">
            <ConnectionIndicator />
            <ThemeToggle />
          </div>
        </div>
        <Suspense fallback={<NavList pathname={null} />}>
          <MainNav />
        </Suspense>
      </header>
      <WakingBanner />
      <main id="main" tabIndex={-1} className="mx-auto min-h-[85dvh] max-w-7xl px-4 py-6 focus:outline-none">
        {children}
      </main>
      <footer className="mx-auto flex max-w-7xl items-center gap-2 px-4 pb-8 text-xs text-muted">
        <Boxes aria-hidden="true" className="size-3.5" />
        At-least-once delivery, effectively-once effects. PostgreSQL is the only data store.
      </footer>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-1 max-w-2xl text-sm text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
