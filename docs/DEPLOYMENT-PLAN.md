# Deployment — as built

One container serves the console and the API. That is the whole architecture,
and every other decision here follows from it.

    GitHub push -> Railway builds Dockerfile -> container serves UI + API

## Why there is no CDN, no bucket, and no second pipeline

The console is three unbuilt static files (`app/web/static/`) that FastAPI
serves itself (`app/web/server.py`), and every call it makes is a root-relative
path. It is not a build artifact and it has no independent lifecycle.

Splitting it onto S3 + CloudFront was evaluated and rejected. It would have
required inventing a build step, an API-base-URL injection mechanism, and
rewrites of all nine `fetch` call sites plus the report `<a href>` — and it
would have *created* a cross-origin boundary that does not currently exist.

Putting CloudFront in front of Railway as a pure proxy was also evaluated and
rejected, for three reasons that only showed up on inspection:

* It would have cached nothing. Every response sets `no-cache, must-revalidate`
  (`app/web/server.py`), which CloudFront honours.
* Railway already issues and renews TLS certificates and supports custom
  domains, so the CDN duplicated what the platform gives free.
* CloudFront's origin response timeout maxes out at 120 s, and
  `POST /api/runs/{id}/rollback` runs synchronously — it can wait up to 600 s
  for a browser slot alone. CloudFront would have broken that endpoint.
  Railway's edge allows ~15 minutes, which covers it.

The CDN would have added an operational component and a network hop to every
1500 ms poll in exchange for nothing this application needs.

## Playwright on Railway — the four things that make it work

**1. The base image.** `mcr.microsoft.com/playwright/python:v1.61.0-noble`
ships Chromium and its ~60 system libraries, pretested against that browser
build. The alternative — `playwright install --with-deps` on a generic base —
is an apt transaction and a 150 MB download on every build.

**2. The version lock.** `requirements.txt` pins `playwright==1.61.0` to match
the image tag. Playwright resolves browsers by version; a mismatch does not
fail the build, it fails the first run with "Executable doesn't exist".
**Bump the pin and the `FROM` tag together, or not at all.**

**3. A build-time launch, not an assumption.** The Dockerfile runs
`main.ensure_dependencies()` and then really launches headless Chromium with
the application's own `build_launch_kwargs`, renders markup, and asserts on the
text. A path check alone cannot tell you a system library is missing; a launch
can. A broken browser therefore fails the build instead of somebody's upload.

**4. Sandbox and shared memory.** The image runs as root, which disables
Chromium's sandbox — `main.CHROMIUM_ARGS` already passes `--no-sandbox` for
exactly this, safe because the browser only ever visits one admin site.
Playwright recommends `--ipc=host`, which Railway cannot supply; the mitigation
is already in the code as `--disable-dev-shm-usage`.

## Serving

`python -m app.web.server` is the entry point everywhere. It reads `HOST`
(default `127.0.0.1`) and `PORT` (default `8000`), so local behaviour is
unchanged and the container differs only by environment: the Dockerfile sets
`HOST=0.0.0.0` and Railway injects `PORT`.

The healthcheck is `/api/ping`, which does no I/O. It is deliberately not
`/api/health`, because that makes a live Supabase round-trip — pointing a
deploy gate at it would let a telemetry outage block a deploy, inverting the
rule the rest of this codebase follows.

## CORS

There is no cross-origin traffic: one origin serves the console and the API, in
every environment including local. `CORS_ALLOW_ORIGINS` (default `*`) exists so
that fronting the app with something later is a variable change rather than a
code change, and so allowed origins are never a literal in the source. As
shipped, the middleware never fires.

## CI/CD

Railway's GitHub integration watches a branch and redeploys on every push to
it, including merge commits. Because the container carries the console, that
single trigger deploys everything. There is no second pipeline and nothing to
invalidate.

## What is not solved

* **No authentication.** Unchanged from before deployment, and now reachable
  from anywhere. See Known gaps in the README.
* **`var/` is ephemeral.** Run reports, the local colour map, and the
  created-products ledger all live under `ROOT/var` and reset on redeploy
  unless a Railway volume is mounted at `/app/var`. Supabase covers the run
  history and the ledger; the report `.xlsx` files have no remote store.
* **No horizontal scaling.** `RunManager` is in-process by design, and Railway
  volumes are incompatible with replicas anyway. `MAX_CONCURRENT = 4` permits
  four simultaneous Chromium instances in one container; size the plan for it.

## Files this added

| File | Purpose |
|---|---|
| `Dockerfile` | Playwright base, dependency install, build-time Chromium proof |
| `.dockerignore` | keeps `.env`, `var/`, `.venv` and caches out of the image |
| `railway.json` | Dockerfile builder, `/api/ping` healthcheck, restart policy |
| `.env.example` | every variable, documented, no values |
| `app/web/server.py` | env-driven bind, `/api/ping`, env-driven CORS |
| `requirements.txt` | `playwright` pinned to the image version |
| `README.md` | the Deploying section |

## Rollback

Every change is additive and the defaults are the previous behaviour, so
reverting the commits restores the original local run exactly. On the platform,
redeploy any earlier deployment from the Railway dashboard.
