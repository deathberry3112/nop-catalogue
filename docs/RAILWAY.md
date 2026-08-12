# Railway infrastructure — as built

This document describes the **production Railway deployment** for
`nop-commerce-catalogue-automation`, and how the application expects to be
configured. Facts below were taken from the Railway project and from the code
in this repo. If something disagrees with the dashboard later, trust the
dashboard for IDs/URLs and update this file.

**Naming note:** Railway environment `production` is this app’s deploy target.
That is separate from which **nopCommerce catalogue** `ADMIN_BASE_URL` points
at. Today that admin is still a **dev** shop (`zinggreenproductsdev…` as
reported by `/api/health`). Renaming the Railway project does not change that.

Related docs:

- [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) — why one container, Playwright
  image choices, CORS, healthcheck philosophy
- [`DESIGN.md`](DESIGN.md) — colour / store / concurrency design rationale
- [`.env.example`](../.env.example) — every application variable

---

## Topology

```
Browser
   │  HTTPS
   ▼
┌─────────────────────────────────────────────┐
│  Railway service: app                       │
│  Dockerfile → python -m app.web.server      │
│  FastAPI + static console + Playwright      │
│  volume: /app/var  (app-volume)             │
└───────────────┬─────────────────────────────┘
                │  DATABASE_URL
                │  host: postgres.railway.internal:5432
                ▼
┌─────────────────────────────────────────────┐
│  Railway service: Postgres                  │
│  image: ghcr.io/railwayapp-templates/       │
│         postgres-ssl:18                     │
│  volume: /var/lib/postgresql/data           │
└─────────────────────────────────────────────┘
                │
                ▼ (outbound HTTPS from app)
         nopCommerce Admin (ADMIN_BASE_URL)
         OpenAI (optional, OPENAI_API_KEY)
```

There is **one application container**. The upload console, HTTP API, and
Playwright RPA workers all run in that process (`RunManager` starts daemon
threads). There is no separate frontend service, no worker service, and no
Redis/queue.

---

## Inventory (verified)

| Item | Value |
|---|---|
| Railway project name | `nop-commerce-catalogue` |
| Railway project id | `4d349624-f7f5-4a71-98ca-d96a49818fd1` |
| Environment name | `production` |
| App service name | `app` |
| App service id | `1bc6787a-caf5-42f6-a612-83b9fec5e068` |
| Public URL | `https://app-production-9539.up.railway.app` |
| App private hostname | `app.railway.internal` |
| Postgres service name | `Postgres` |
| Postgres image | `ghcr.io/railwayapp-templates/postgres-ssl:18` |
| DB host (from app) | `postgres.railway.internal` |
| DB port | `5432` |
| DB name | `railway` |
| DB user | `postgres` |
| App volume | `app-volume` → mount path `/app/var` (size quota 5000 MB) |
| Postgres volume | `postgres-volume` → `/var/lib/postgresql/data` (size quota 5000 MB) |
| GitHub repo for auto-deploy | `pranayupbott/nop-commerce-catalogue-automation` on branch `main` |
| OpenAI | **Not set** (`llm_enabled: false` on `/api/health`) unless you add `OPENAI_API_KEY` |

Verify at any time:

```bash
curl -sS https://app-production-9539.up.railway.app/api/ping
curl -sS https://app-production-9539.up.railway.app/api/health
```

Healthy response shape (values vary):

```json
{
  "admin_configured": true,
  "store_configured": true,
  "store_healthy": true,
  "store_detail": "ok",
  "llm_enabled": false
}
```

`store_healthy: false` with `UndefinedTable: relation "runs"` means
`DATABASE_URL` is set but [`app/store/schema.sql`](../app/store/schema.sql) has
not been applied yet.

### Data on redeploy

Redeploying **`app`** (including after a GitHub merge) does **not** wipe
Postgres. `runs` / `created_products` / colour tables live on
`postgres-volume`. Files under `/app/var` survive on `app-volume`. Only
deleting the Postgres service or its volume loses DB data.

---

## How the app is built and started

Config in repo: [`railway.json`](../railway.json)

| Setting | Value | Notes |
|---|---|---|
| Builder | `DOCKERFILE` | [`Dockerfile`](../Dockerfile) |
| Base image | `mcr.microsoft.com/playwright/python:v1.61.0-noble` | Must stay paired with `playwright==1.61.0` in [`requirements.txt`](../requirements.txt) |
| Start command | `python -m app.web.server` | Same as Dockerfile `CMD` |
| Healthcheck path | `/api/ping` | Liveness only — no DB I/O |
| Healthcheck timeout | `300` seconds | First boot can be slow (Playwright image) |
| Restart policy | `ON_FAILURE` | |

Runtime bind:

- Dockerfile sets `HOST=0.0.0.0`
- Railway injects `PORT`
- Server: [`app/web/server.py`](../app/web/server.py)

The Dockerfile **launches Chromium at build time** with the app’s own launch
kwargs. A missing system library fails the build, not the first upload.

---

## Environment variables

### Required for catalogue runs

| Variable | Where set | Purpose |
|---|---|---|
| `ADMIN_BASE_URL` | Railway `app` service variables | nopCommerce admin base, including `/admin` |
| `ADMIN_EMAIL` | Railway `app` service variables | Admin login |
| `ADMIN_PASSWORD` | Railway `app` service variables | Admin password |

Without these, `/api/health` reports `admin_configured: false` and
`POST /api/runs` refuses with a 400 (`An admin URL is required`).

### Required for durable run history / ownership ledger

| Variable | Where set | Purpose |
|---|---|---|
| `DATABASE_URL` | Railway `app` service, value reference `${{Postgres.DATABASE_URL}}` | Postgres wire URL for the run store |

That reference resolves to a URL whose **host is `postgres.railway.internal`**
(private project network). The app never talks to PostgREST and does not use a
Supabase service key.

Without `DATABASE_URL`, the UI still works; runs stay in memory only and the
ownership ledger falls back to `var/created-products.json`.

### Optional

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Colour resolution tier 3 |
| `HOST` / `PORT` | Bind address (container/platform set these) |
| `CORS_ALLOW_ORIGINS` | Unused in the one-origin topology |
| `NOP_HEADED` | Must stay off on Railway (no display) |

Canonical list with comments: [`.env.example`](../.env.example).
Secrets are read only through [`app/config.py`](../app/config.py).

### Variables Railway injects (do not invent these)

Examples on the `app` service: `RAILWAY_PUBLIC_DOMAIN`,
`RAILWAY_PRIVATE_DOMAIN`, `RAILWAY_VOLUME_MOUNT_PATH`, `RAILWAY_PROJECT_ID`,
`RAILWAY_SERVICE_ID`, etc. The application code does not require them; they are
platform metadata.

---

## Database schema

DDL file: [`app/store/schema.sql`](../app/store/schema.sql)

- Idempotent (`IF NOT EXISTS`, enum `DO` blocks, `CREATE EXTENSION IF NOT EXISTS pgcrypto`)
- Tables used by the app: `runs`, `run_rows`, `row_steps`, `events`,
  `colour_codes`, `colour_swatches`, `llm_inferences`, `created_products`
- Views `failure_summary` / `step_health` exist in SQL; the Python app does not
  query them today

### Apply / re-apply from a laptop

Postgres has **no public TCP URL** here (only the private
`postgres.railway.internal` hostname). Use an SSH tunnel:

```bash
# terminal 1 — keep open
railway link   # if needed; select nop-commerce-catalogue
railway connect Postgres --tunnel-only --port 55432

# terminal 2 — password from Railway → Postgres → Variables (POSTGRES_PASSWORD)
export PATH="$(brew --prefix postgresql@18)/bin:$PATH"   # or any psql
PGPASSWORD='…' psql -h 127.0.0.1 -p 55432 -U postgres -d railway \
  -v ON_ERROR_STOP=1 -f app/store/schema.sql
```

Or, once SSH into the **app** container works for your account:

```bash
railway ssh -s app -- python sql.py app/store/schema.sql
```

Local development against Homebrew/Docker Postgres:

```bash
export DATABASE_URL=postgresql://USER@localhost:5432/nop_automation
python sql.py app/store/schema.sql
```

---

## Volumes and persistence

| Path | Backing | Survives redeploy? | Contents |
|---|---|---|---|
| `/app/var` | `app-volume` | Yes | Reports (`var/reports/`), local colour map JSON, local created-products JSON if used |
| Postgres data | `postgres-volume` | Yes | All run-store tables |
| Container filesystem elsewhere | ephemeral | No | — |

Report `.xlsx` files are **not** uploaded to object storage; without the
`/app/var` volume they disappear on redeploy even if Postgres still has the run
row.

---

## Scaling and concurrency (hard limits)

- **Replicas must stay at 1** for `app`. `RunManager` is in-process; a second
  replica would not share in-memory run state. Railway volumes are also
  incompatible with multi-replica attachments in the usual setup.
- In-process cap: `MAX_CONCURRENT = 4` Chromium instances
  ([`app/web/runs.py`](../app/web/runs.py)).
- Schema columns `worker_id` / `lease_expires_at` exist for a future multi-worker
  claim loop; that loop is **not** implemented.

---

## Security posture (as shipped)

1. **No application authentication.** Anyone who can reach the public URL can
   call the API and start runs. Server-side `ADMIN_*` means visitors do not need
   nopCommerce credentials of their own.
2. The public hostname is a normal Railway service domain — not private by
   default. Keeping exposure limited means not sharing the URL, removing the
   domain, or adding auth (none of which are built into the app today).
3. `DATABASE_URL` stays on the server. The browser only talks to FastAPI.
4. Prefer **Preview** for day-to-day use. Use **Commit** only when you accept
   writes to whatever catalogue `ADMIN_BASE_URL` names.

---

## GitHub auto-deploy (setup)

Goal: every push/merge to `main` rebuilds and redeploys the `app` service from
this repo’s Dockerfile. Postgres is untouched by those deploys.

### Prerequisites

- Railway account that owns project `nop-commerce-catalogue`
- GitHub repo: `pranayupbott/nop-commerce-catalogue-automation`
- **Railway GitHub App installed** on that user/org with access to the repo  
  (without this, CLI reports `No GitHub installation found` even if a repo name
  appears on the service)

### Current state

- Service `app` is connected to
  `pranayupbott/nop-commerce-catalogue-automation` branch `main`
- Pushes/merges to that repo’s `main` rebuild and redeploy `app`

### Steps (reconnect / reinstall)

**1. Install / authorize the Railway GitHub App** (required once)

1. Open https://github.com/apps/railway-app  
   (or Railway dashboard → your account → GitHub)
2. Install / Configure → grant access to
   `pranayupbott/nop-commerce-catalogue-automation` (or all repos)
3. Use the **pranayupbott** GitHub identity (not a different fork owner)

**2. Connect / confirm the `app` source on branch `main`**

CLI:

```bash
railway link -p 4d349624-f7f5-4a71-98ca-d96a49818fd1
railway service source connect \
  --repo pranayupbott/nop-commerce-catalogue-automation \
  --branch main \
  --service app
```

Or dashboard: **nop-commerce-catalogue** → **app** → **Settings** → **Source**
→ connect the repo → branch **`main`** → Save.
Expect no red “GitHub Repo not found” banner.

**3. Confirm**

```bash
railway status --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
for e in d['environments']['edges']:
  for s in e['node']['serviceInstances']['edges']:
    n = s['node']
    if n['serviceName'] == 'app':
      print('repo=', (n.get('source') or {}).get('repo'))
"
```

Expect `repo= pranayupbott/nop-commerce-catalogue-automation`.

**Git remote:** this checkout’s `origin` should be

`https://github.com/pranayupbott/nop-commerce-catalogue-automation.git`

Pushes/PRs go there; Railway watches that repo’s `main`.

### After it is connected

| Event | Result |
|---|---|
| Merge / push to `main` | New Docker build of `app`, then replace the running container |
| Postgres data | Unchanged |
| `/app/var` reports | Unchanged (volume) |
| Variable edits in dashboard | Still need **Apply / Deploy** if Railway stages them |

Optional later: enable **PR environments** on the project if you want preview
deploys per pull request (separate from production). Not required for
merge-to-`main` deploys.

### Manual deploy (still works)

```bash
railway up --service app
```

Use this for a one-off from a dirty tree; prefer GitHub once the App is
installed so production tracks `main`.

---

## Application ↔ store mapping

| Concern | Module | Mechanism |
|---|---|---|
| Settings | `app/config.py` | `DATABASE_URL` → `store_enabled` |
| Pool / SQL helpers | `app/store/client.py` | `ThreadedConnectionPool`, `fetch_all`, `execute`, `healthy` |
| Run telemetry + ledger | `app/store/run_store.py` | Best-effort writes via `_safe`; in-memory mirror for UI polling |
| Colour codes / swatches | `app/colour/store.py`, `app/colour/swatch.py` | SQL select / upsert when store enabled |
| Rollback plan reads | `app/rollback.py` | Parameterised `SELECT`s via `fetch_all` |
| DDL CLI | `sql.py` | `psycopg2.connect(DATABASE_URL)` |

Telemetry rule (unchanged): if Postgres is down mid-run, writes are logged and
dropped; product automation continues.

---

## Preview vs Commit (operational)

| Mode | Behaviour |
|---|---|
| **Preview** (`commit=false`, default in the UI) | Signs in, resolves colours, fills the create form, **does not save** |
| **Commit** (`commit=true`) | Creates / configures products for real on `ADMIN_BASE_URL` |

---

## Failure modes worth recognising

| Symptom | Likely cause |
|---|---|
| `store_configured: false` | `DATABASE_URL` missing on the service |
| `store_healthy: false`, relation `runs` missing | Schema not applied |
| `admin_configured: false` | `ADMIN_*` missing **or** variables staged but not applied/deployed |
| Build fails on Chromium probe | `playwright==` pin ≠ Dockerfile image tag |
| Reports vanish after redeploy | `/app/var` volume missing or not mounted |
| Two replicas / weird run UI | Horizontal scale attempted — unsupported |
| Push to `main` does not deploy | GitHub source not connected, or wrong branch watched |

---

## What this deployment deliberately is not

- Not Supabase, not PostgREST, not OneClickSupabase
- Not a multi-service “frontend / backend / RPA” split
- Not authenticated at the web layer
- Not automatically “safe to point at a live customer catalogue” until auth and
  operational controls exist — even when the Railway project is named production
