# Deployment runbook

The public demo runs as:

- **the dashboard** on **Vercel** (free Hobby tier), and
- **the API, schedulers, workers and PostgreSQL** on **one small VPS** with Docker Compose, behind
  **Caddy** for automatic HTTPS.

Everything up to the last mile is prepared in this repository. The steps below need your accounts
(domain, VPS, Vercel, GitHub), which is why they are a runbook rather than automation.

## Cost

| Item | Choice | Approx. monthly cost |
| --- | --- | --- |
| VPS | 2 vCPU / 2–4 GB RAM (e.g. Hetzner CX22, DigitalOcean Basic, Vultr) | $5–7 |
| Dashboard | Vercel Hobby | $0 |
| TLS | Let's Encrypt via Caddy | $0 |
| Images | GitHub Container Registry (public) | $0 |
| Domain | Any registrar (optional; a subdomain you own works) | ~$1 (amortised) |
| **Total** | | **≈ $6–8 / month** |

Prices are as of late 2026 and vary by provider and region; check before you buy. The compose
file sets memory limits that fit in 2 GB (Postgres 768 MB, API 384 MB, 2 × scheduler and 3 ×
worker at 192 MB each).

## 1. DNS

Create an `A` (and `AAAA`, if you have IPv6) record for the API host, e.g.
`api.conductor.example.com → <VPS IP>`. The dashboard's domain is configured in Vercel (step 5).

## 2. Server

On a fresh Ubuntu 24.04 VPS:

```bash
# Docker Engine + Compose plugin (official convenience script; review it first if you prefer)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker

# Firewall: SSH, HTTP (ACME challenge + redirect), HTTPS
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw allow 443/udp && sudo ufw enable

sudo mkdir -p /opt/conductor && sudo chown "$USER" /opt/conductor
git clone https://github.com/OWNER/conductor.git /opt/conductor
cd /opt/conductor
cp deploy/.env.prod.example deploy/.env
```

Edit `deploy/.env`:

| Variable | Value |
| --- | --- |
| `API_DOMAIN` | `api.conductor.example.com` |
| `ACME_EMAIL` | your email (Let's Encrypt expiry notices) |
| `DASHBOARD_ORIGIN` | the dashboard's origin, e.g. `https://conductor.example.com` (CORS) |
| `POSTGRES_PASSWORD` | `openssl rand -base64 32` |
| `CONDUCTOR_IMAGE` | `ghcr.io/OWNER/conductor-backend:latest` (published by the Release workflow) |

`deploy/.env` is git-ignored. It's the only place secrets live.

## 3. First deploy

```bash
cd /opt/conductor
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env pull
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env ps
curl -fsS https://api.conductor.example.com/api/v1/health
```

`migrate` and `seed` run once and exit; then the API, 2 schedulers and 3 workers start. Caddy
obtains the certificate on the first request.

> If you'd rather build on the server than pull from GHCR, set
> `CONDUCTOR_IMAGE=conductor-backend:local` and run
> `docker build -t conductor-backend:local backend` first.

## 4. GitHub

1. Push the repository. CI (`.github/workflows/ci.yml`) runs lint, types, the backend suite
   (including chaos tests) against PostgreSQL 16, the dashboard checks, Playwright + axe end to end,
   compose/Caddy validation and image builds.
2. Replace `OWNER/REPO` in the README badges.
3. **Releases** (`.github/workflows/release.yml`): pushing a tag `v*` publishes the backend image
   to GHCR. Make the package public (Packages → conductor-backend → Settings) so the VPS can pull it
   anonymously, or `docker login ghcr.io` on the server.
4. **Automatic deploys (optional)**: in the repository settings, create an environment
   `production` with secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` (a key authorised
   only for deploys) and `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan <host>`), plus variables
   `DEPLOY_ENABLED=true` and `API_URL=https://api.conductor.example.com`. Each tag then backs up
   the database, pulls, restarts and smoke-tests `/health`.

## 5. Vercel (dashboard)

1. Import the repository in Vercel; set **Root Directory** to `frontend` (framework: Next.js).
2. Environment variable: `NEXT_PUBLIC_API_URL=https://api.conductor.example.com`.
3. Add your domain (e.g. `conductor.example.com`). It must equal `DASHBOARD_ORIGIN` on the VPS.
4. Deploys happen on every push to `main`.

## 6. Operations

**Backups.** `./deploy/backup.sh` writes a compressed `pg_dump` to `deploy/backups/` and prunes
copies older than 14 days. Schedule it and copy dumps off the box:

```bash
crontab -e
# 17 3 * * *  /opt/conductor/deploy/backup.sh >> /opt/conductor/deploy/backups/backup.log 2>&1
```

Restore with the command in the script's header. Demo data is ephemeral by design (auto-reset
every 5 minutes for runs older than 30), so backups mainly protect the schema and workflows.

**Metrics.** `/metrics` is blocked at Caddy. Scrape it on the Docker network
(`http://api:8000/metrics`) or through an SSH tunnel:
`ssh -L 8000:localhost:8000 host` after publishing the API on localhost, or run Prometheus as
another service on the compose network.

**Logs.** `docker compose … logs -f api worker scheduler` (JSON lines with `request_id`,
`run_id`, `task_id`, `worker_id`), rotated at 3 × 10 MB per container.

**Update / roll back.** Tag a release (automatic) or set `CONDUCTOR_IMAGE` to a specific tag and
`up -d`. Migrations are additive in this codebase; take a backup before upgrading across a
migration.

## 7. Verify the demo's safety controls

After deploying, check each control from a terminal (replace the host):

```bash
API=https://api.conductor.example.com/api/v1
SB=verify$(openssl rand -hex 8)

# General writes are locked on the public demo (403 disabled_in_demo)
curl -s -X POST $API/jobs -H 'content-type: application/json' -d '{"task_type":"sleep"}'

# Demo writes need a sandbox id (422 without)
curl -s -X POST $API/demo/runs

# Per-sandbox run cap → 429 with Retry-After after 3 concurrent runs
for i in 1 2 3 4; do curl -s -o /dev/null -w "%{http_code} " -X POST $API/demo/runs -H "X-Sandbox-Id: $SB"; done; echo

# Kill cooldown → second kill within 10 s is 429
curl -s -X POST $API/demo/chaos/kill-worker -H "X-Sandbox-Id: $SB" >/dev/null
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/demo/chaos/kill-worker -H "X-Sandbox-Id: $SB"

# Metrics are not public (404)
curl -s -o /dev/null -w "%{http_code}\n" https://api.conductor.example.com/metrics
```

Also confirm in the dashboard that a killed worker disappears, its task is recovered, and a fresh
worker appears (Docker's restart policy).

| Control | Where |
| --- | --- |
| No arbitrary code; built-in task types only | ADR 0007, `worker/registry.py` |
| `http_fetch` limited to an allowlist (`api` only in prod), no redirects, timeout, size cap | `tasks/pipeline.py`, `CONDUCTOR_HTTP_ALLOWLIST` |
| Per-visitor sandboxes; cancel/replay only your own runs | `demo/service.py`, `api/routes/resources.py` |
| Per-IP action budget (20/min), kill cooldown (10 s/IP, 6/min globally) | `demo/limits.py`, `api/routes/demo.py` |
| Run caps (3 per sandbox, 30 global), queue cap (400) | `demo/service.py` |
| Chaos only affects the demo worker pool (`CONDUCTOR_DEMO_CHAOS=true`) | `worker/runtime.py` |
| Auto-reset of old demo data | scheduler leader duty, `demo/service.reset_demo_data` |
| No accounts, no cookies, no personal data; sandbox ids are random | `frontend/src/lib/sandbox.ts` |
