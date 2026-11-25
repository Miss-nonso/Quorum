# Conductor dashboard

Next.js (App Router) + TypeScript + Tailwind, TanStack Query, React Flow and Recharts.

```bash
npm ci
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev   # http://localhost:3000
```

| Script | What it does |
| --- | --- |
| `npm run dev` / `build` / `start` | Develop, build and serve |
| `npm run lint` / `typecheck` / `format:check` | ESLint, `tsc`, Prettier |
| `npm test` | Vitest: pure logic (DAG layout, timeline, narration) and component tests |
| `npm run test:e2e` | Playwright + axe against a running backend (`make e2e` starts one) |

**Live data.** One `EventSource` (`src/lib/live.tsx`) feeds TanStack Query's cache: snapshots for
stats and workers, and invalidation for runs touched by an event. It reconnects on its own with
`Last-Event-ID`, so no events are lost. The header shows the connection state.

**Accessibility.** Status is always icon, text and colour together. Colour tokens meet WCAG AA in
both themes. The DAG nodes are real buttons, and the graph has a text equivalent. The timeline
has a data-table equivalent. Focus is visible everywhere, there's a skip link, and
`prefers-reduced-motion` is respected. axe runs on every page in CI.
