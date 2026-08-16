# Observability and errors

**Role:** How failures are classified, what is recorded, and which health endpoint means what.
**Not for:** The ownership ledger ([`STORE.md`](STORE.md)) or UI polling details ([`WEB.md`](WEB.md)).

Verified against: `app/errors.py`, `app/store/run_store.py`, `app/web/server.py`, `app/automation/runner.py` `_retrying`, `tests/test_errors.py` (**2026-08-17**).

## Ping vs health (do not conflate)

| Endpoint | I/O | Use |
|---|---|---|
| `GET /api/ping` | none | Process liveness; `railway.json` `healthcheckPath` |
| `GET /api/health` | Postgres `SELECT` when configured | Operator/UI capability flags |

Pointing a platform probe at `/api/health` lets a telemetry outage fail a deploy — the inversion this codebase refuses.

## Error taxonomy (`app.errors`)

`classify(exc)` is best-effort, **type first** then ordered regexes. Unrecognised → `unknown`. A rising unknown count means the table is stale.

| Class | Retry? (`RETRY_POLICY`) | Blame |
|---|---|---|
| `transient_dom` | 5 | nobody |
| `transient_net` | 3 | infrastructure |
| `auth` | 1 | nobody |
| `selector_miss` | no | us |
| `data_colour` | no | client |
| `data_invalid` | no | client |
| `nop_rejected` | no | client |
| `conflict` | no | expected |
| `unknown` | no | us |

`CombinationError` → `data_colour` regardless of message. `ConfigError` → `data_invalid`. Playwright `TimeoutError` → `transient_net`.

The runner applies this in `_retrying`. `RunStore.step` records `error_class` on failure but **does not** decide retries.

**Do not conflate** with `library.retry` (generic helper; must not wrap `save`).

## What is collected vs what is public

| Data | Collected | Public |
|---|---|---|
| Row status, error_class, error text (truncated 2000) | always (memory); Postgres if remote | run snapshot |
| Step name, duration, attempt | same | run snapshot rows[].steps |
| Events | memory cap 500; snapshot last 80 | run **detail** only, not list |
| LLM tokens/cost | `record_inference` | `llm_cost_usd` on snapshot |
| Admin password | never | never |
| Debug blobs / traces / tool-call dumps | **none** in this repo | n/a |

There is no token-gated debug route. There is no sticky session restore of debug fields — there is no chat session.

## UI grouping

`app.js` `renderFailures` groups `run.rows` by `error_class`. It does **not** query SQL view `failure_summary`. Same idea, different path — do not “simplify” by deleting one without checking the other.

## Non-goals

- p50/p95 dashboards or a metrics product — not implemented
- Retrying at the whole-row level (retries are per named step)
