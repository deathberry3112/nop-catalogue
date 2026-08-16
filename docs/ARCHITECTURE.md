# Architecture

**Role:** Stack as implemented, package layout, end-to-end run path, and surfaces.
**Not for:** The law table ([`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md)) or the change loop ([`CHANGE_PROCESS.md`](CHANGE_PROCESS.md)).

Verified against: `main.py`, `app/web/server.py`, `app/web/runs.py`, `app/automation/runner.py`, `Dockerfile`, `railway.json`, `requirements.txt` (**2026-08-17**).

## What this product is

A **single Python process** that accepts a product `.xlsx`, parses it into rows, and drives a **nopCommerce 3.x admin** with Playwright to create or resume products (specification, colour/size attributes, generated combinations, child SKUs). A FastAPI app serves a no-build static console and JSON API. Optional Postgres records runs; optional OpenAI is colour-code tier 3 only.

It is not a storefront, not a multi-service mesh, and not a second frontend pipeline.

## Stack as implemented

| Piece | As shipped |
|---|---|
| Language | Python 3 (no `pyproject.toml`; `requirements.txt`) |
| HTTP | FastAPI `api` in `app/web/server.py`; uvicorn |
| UI | Three static files in `app/web/static/` — no bundler, no SPA framework |
| Browser RPA | Playwright sync API, Chromium; `playwright==1.61.0` |
| Workbooks | openpyxl |
| Optional LLM | `openai` SDK, `gpt-4o-mini` default, JSON schema mode |
| Optional DB | Postgres via `psycopg2` + `DATABASE_URL` (Railway Postgres in production) |
| Deploy | One Docker image from `mcr.microsoft.com/playwright/python:v1.61.0-noble`; Railway service `app` |
| CI in this repo | **None** (no `.github/workflows`). Railway rebuilds `app` on pushes to `main` when the GitHub source is connected — that is a deploy, not a pytest gate |

Python package manager: `pip` + `requirements.txt`. No npm, no Cargo, no Go.

## Package layout

```
main.py                 run() + RunOptions + Credentials + Chromium launch
app/config.py           the only settings object; loads .env without overwriting the real env
app/parser.py           xlsx → rows (refuses unknown columns)
app/errors.py           ErrorClass + RETRY_POLICY
app/colour/             map / heuristic / LLM + swatches
app/store/              Postgres client, RunStore, schema.sql
app/automation/runner.py  run_batch — login, loop rows, skip/preview/create
app/web/                FastAPI + RunManager + static console
app/rollback.py         plan + execute deletion of this run's claims
pages/                  LandingPage, ManageProductsPage, ProductPage
library.py              Playwright helpers (not flows)
combination_skus.py     split/score/verify combination SKUs
xlsxCreator.py          rows + summary → re-uploadable report
sql.py                  DDL/query CLI against DATABASE_URL
parsed_rows.json        checked-in parser payload (fixture, not a living doc)
conftest.py / tests/    offline unit tests + marker-gated live Playwright
Dockerfile / railway.json / .env.example
```

There are no `apps/` or `libs/` workspaces. There is no worker service, no Redis, no queue.

## End-to-end happy path

```
Browser  GET /
         POST /api/parse          parse_workbook → preview table
         POST /api/runs           RunManager.start
              │
              ├─ RunStore.start   in-memory snapshot + optional INSERT runs
              └─ daemon thread    semaphore (MAX_CONCURRENT=4)
                    main.run(rows, RunOptions, Credentials, store=)
                      partition → runnable / done / blocked
                      [commit + blocked → ConfigError, no browser]
                      Chromium launch (main.build_launch_kwargs)
                      run_batch(page, runnable, …)
                        LandingPage.login
                        owned = store.owned_skus ∪ local ledger
                        for each row:
                          find_by_sku
                          exists ∩ not owned → skipped (untouched)
                          not commit → preview (resolve colours, fill_basics, no save)
                          commit ∩ new → add_new, fill_basics, save, claim_product
                          commit ∩ owned → resume ProductPage
                          spec + attributes + combinations (commit only)
                      xlsxCreator.write_report → var/reports/{run_id}.xlsx
         GET /api/runs/{id}       poll snapshot every 1.5s
         GET /api/runs/{id}/report
```

Entry point in every environment: `python -m app.web.server`  
Binds `HOST` (default `127.0.0.1`) and `PORT` (default `8000`). Dockerfile sets `HOST=0.0.0.0`; Railway injects `PORT`.

Laptop setup (venv, `.env`, optional Postgres, first Preview): [`LOCAL.md`](LOCAL.md).

Local convenience: `python main.py` runs `parsed_rows.json` in dry-run and writes `report.xlsx`. That path uses `Credentials.from_env()`, including its hardcoded default base URL — see invariants.

## What launches the browser

`main.run` is the only production launch site (rollback launches a second time in `RunManager.rollback`). Page objects and `run_batch` take a `page`. `main.resolve_runner()` imports `app.automation.runner.run_batch` and falls back to `_stub_run_batch` if that import fails. The stub is **unused**; `app/automation/__init__.py` exports the real `run_batch`.

## Public HTTP surface

See [`WEB.md`](WEB.md) for field-level contracts. Routes:

| Method | Path | Effect |
|---|---|---|
| GET | `/`, `/app.css`, `/app.js` | Console (no-cache + mtime query) |
| GET | `/api/ping` | `{ok: true}` — deploy healthcheck |
| GET | `/api/health` | Capability flags; **no password** |
| POST | `/api/parse` | Workbook → row envelope |
| POST | `/api/runs` | Start worker thread; `{run_id}` |
| GET | `/api/runs` | In-process summaries only |
| GET | `/api/runs/{id}` | Snapshot |
| GET/POST | `/api/runs/{id}/rollback` | Plan / delete |
| GET | `/api/runs/{id}/report` | `.xlsx` |
| GET/POST | `/api/colours`, `/confirm`, `/refresh` | Colour map review |

No webhooks. No authenticated session. CORS middleware is present with default `*` and **does not fire** in the one-origin topology.

## Config / secrets boundary

| Secret | Where it lives | Browser |
|---|---|---|
| `ADMIN_PASSWORD` | env / `.env` via `settings()`; optional POST body on start/rollback | Typed into a **CSS-hidden** form; never stored in `localStorage`; never in API responses |
| `ADMIN_EMAIL`, `ADMIN_BASE_URL` | same | Health **does** return them; UI may prefill |
| `OPENAI_API_KEY` | `settings().openai_api_key` | Never; health has `llm_enabled` / `llm_model` |
| `DATABASE_URL` | `settings().database_url` | Never |

`.env` is gitignored. `.env.example` documents keys with empty values.

## Data stores

- **Postgres** (optional): schema in `app/store/schema.sql`. Apply with `python sql.py app/store/schema.sql`.
- **In-memory `RunStore.state`**: what the UI polls; required even when Postgres is down.
- **`var/`** (gitignored): reports, local colour-map JSON, local created-products JSON. Needs a volume in production (`/app/var`).

No ORM migrations directory — the SQL file is the migration, and it is written to be idempotent.

## What is in schema or unused fallbacks

| Present | Used? |
|---|---|
| `runs.worker_id` / `lease_expires_at` | Written / **no** claim loop |
| SQL views `failure_summary`, `step_health` | **No** — UI groups from the snapshot |
| `main.resolve_runner` stub | **No** — `app.automation.runner.run_batch` is the path |
| `python-dotenv` | **No** — `app.config._load_env_file` |
| CDN / S3 / separate frontend | **No** — see [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) |

Resume after a partial commit is report re-upload + `DONE_STATUSES`, not a “retry failed rows” button.

## Non-goals

- Modelling a third product attribute
- Horizontal scaling of `app`
- Object storage for reports
- Web authentication (explicit gap)
- Using retrieval/RAG — there is none; colour examples in the LLM prompt come from the in-process colour map, not a vector index
