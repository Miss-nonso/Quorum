# Free deployment: Vercel + Render + Neon ($0/month)

The public demo can run entirely on free tiers:

| Piece | Service (free plan) | What runs there |
| --- | --- | --- |
| Dashboard | **Vercel** Hobby | Next.js app (`frontend/`) |
| Backend | **Render** free web service | `quorum all`: API + scheduler in one process, 2 supervised worker subprocesses |
| Database | **Neon** Free | PostgreSQL (direct connection) |
| CI | **GitHub Actions** (public repo) | Lint, types, tests, chaos, Playwright + axe |

Free tiers change; the numbers below were checked in November 2026. Check each provider's
pricing page (and what its sign-up asks for) before you rely on them.

## How the free shape differs from the VPS shape

Render's free plan runs one web process and has **no free background workers**, so
`quorum all` puts everything in one container:

- the API serves on `$PORT`;
- the scheduler (leader election, reaper, cron, priority aging, demo reset) runs in the API's
  event loop;
- workers run as **real subprocesses** under a supervisor that restarts any that exit. The demo's
  "kill a worker" still crashes a genuine process mid-task, and a new worker replaces it.

The engine is unchanged: the processes coordinate only through PostgreSQL, exactly as in the
multi-container setup. A chaos test runs `quorum all` for real
(`backend/tests/chaos/test_all_in_one.py`): migrate, seed, run the sample, kill a worker, check
that a new one replaces it, and check that shutdown leaves no orphans. Measured memory: about
250 MB for the API, scheduler and two workers, within Render's 512 MB.

## What "free" costs you (be upfront with visitors)

| Limit | Effect | What Quorum does about it |
| --- | --- | --- |
| Render free services **sleep after 15 min** without traffic; waking takes about a minute | The first visitor waits | The dashboard shows "Waking up the demo server…" and retries by itself |
| Render: 750 free instance-hours a month, per workspace | Plenty for one service | |
| Neon: **100 compute-hours a month** (≈ 400 h at the smallest 0.25 CU size), scale-to-zero after 5 min idle | The database must be allowed to sleep | The dashboard **pauses its live stream and polling** when the tab is hidden or idle for 10 minutes, so an open tab doesn't keep the server and database awake. Don't add an uptime pinger: it would use up the compute hours |
| Neon: 1 GB storage | Demo data must stay small | The scheduler deletes finished demo runs older than 30 minutes, every 5 minutes |
| Render free instance: 0.1 CPU, 512 MB | Slower runs under load | Concurrent demo runs capped at 10; 2 workers × 2 slots |

## 1. Push the code to GitHub

Create an **empty public** repository (no README or license), then from the project root:

```bash
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

CI runs on the push. The `release.yml` workflow is only needed for the VPS route and does
nothing until you push a `v*` tag.

## 2. Database: Neon

1. Sign up at neon.com, then **Create project**: Postgres 16 (or newer), in the region closest to
   where you'll run Render (e.g. AWS Frankfurt ↔ Render Frankfurt).
2. Compute: set the size to **0.25 CU** (min and max). It's plenty for the demo, and it stretches
   the 100 CU-hours to about 400 hours of awake time.
3. Open **Connect**, turn **Connection pooling OFF**, and copy the connection string. It looks
   like `postgresql://…@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require`.
   Paste it into Render as-is: Quorum converts it for its driver.

> Why direct and not pooled? Neon's pooler runs PgBouncer in transaction mode, which doesn't
> support `LISTEN/NOTIFY` or session-level advisory locks. Quorum uses both: wake-ups and the
> live stream, and scheduler leader election.

## 3. Backend: Render

1. Sign up at render.com with GitHub and allow access to the repository.
2. **New → Blueprint**, pick the repository. Render reads `render.yaml` and proposes the
   `quorum-api` service on the free plan. Before applying:
   - `QUORUM_DATABASE_URL` = the Neon direct connection string;
   - `QUORUM_CORS_ORIGINS` = your Vercel URL (you can set a placeholder now and change it after
     step 4, e.g. `https://quorum-demo.vercel.app`);
   - optionally change `region` in `render.yaml` to match Neon.
3. Apply. The first build takes a few minutes. On boot the container migrates the database, seeds
   the sample workflow, then starts. Check `https://<service>.onrender.com/api/v1/health`, and
   `/docs` for the API reference.

The blueprint deploys only after CI passes on a commit (`autoDeployTrigger: checksPass`).

## 4. Dashboard: Vercel

1. Sign up at vercel.com with GitHub, then **Add New → Project** and pick the repository.
2. **Root Directory:** `frontend` (framework is detected as Next.js).
3. **Environment variable:** `NEXT_PUBLIC_API_URL` = `https://<service>.onrender.com` (no trailing
   slash).
4. Deploy. Your URL will be something like `https://<project>.vercel.app`. Rename the project for a
   nicer subdomain if you like.
5. Back in Render, set `QUORUM_CORS_ORIGINS` to that exact URL (scheme included, no trailing
   slash). Render redeploys automatically.

## 5. Finish the README

Replace the placeholders: `Miss-nonso/Quorum` in the badges, and the two "Live demo" / "API docs" links
at the top, with your Vercel and Render URLs.

## 6. Verify

```bash
API=https://<service>.onrender.com/api/v1
curl -s $API/health                                   # {"status":"ok",...}
curl -s $API/meta                                     # {"demo_mode":true,...}
curl -s -X POST $API/jobs -H 'content-type: application/json' -d '{"task_type":"sleep"}'
# -> 403 disabled_in_demo (general writes are locked on the public demo)
```

Then open the dashboard: run the pipeline, kill a worker, watch the recovery, inject failures and
replay. If the page says it's waking up, wait about a minute.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Dashboard shows "Can't reach the API" in the browser console with a CORS error | `QUORUM_CORS_ORIGINS` must equal the dashboard origin exactly (`https://…vercel.app`, no trailing slash) |
| Render logs: `LISTEN` or advisory lock errors | You used Neon's **pooled** string (host contains `-pooler`). Use the direct one |
| Render logs: out of memory | Set `QUORUM_ALL_IN_ONE_WORKERS=1` |
| Neon compute hours running out | Something keeps the backend awake (an uptime monitor, a bot). Remove it; the free demo is meant to sleep |
| Sample run fails at `fetch` | The sample calls the container itself on `127.0.0.1:$PORT`; don't set `QUORUM_DEMO_DATASET_URL` on Render |

## Moving off free tiers later

Same code, two routes:

- **Keep this shape, remove the sleep:** upgrade the Render service to a paid instance and Neon to
  a paid plan.
- **Multi-container on a small VPS:** separate API, two schedulers and N workers behind Caddy, as in
  [deployment.md](deployment.md).
