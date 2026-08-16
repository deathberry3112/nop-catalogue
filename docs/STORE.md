# Run store and ownership ledger

**Role:** What Postgres records, what stays in memory, and the telemetry-must-not-break-automation rule.
**Not for:** Colour map semantics ([`COLOUR.md`](COLOUR.md)) or HTTP snapshot shapes ([`WEB.md`](WEB.md)).

Verified against: `app/store/schema.sql`, `client.py`, `run_store.py`, `app/config.py`, `sql.py` (**2026-08-17**).

## When the store is on

`settings().store_enabled` ≡ `bool(DATABASE_URL)`. Without it: UI still works; `RunStore.remote` is false; writes are skipped; `owned_skus()` returns empty from Postgres and the runner unions the **local JSON ledger**.

Apply DDL once (idempotent):

```bash
python sql.py app/store/schema.sql
```

`sql.py` runs the whole file in **one transaction**. A mid-file failure commits nothing. `postgres://` is normalised to `postgresql://`.

## Grains

```
runs  →  run_rows  →  row_steps
              ↓
           events     (append-only narrative; not status source of truth)
```

Status lives on typed columns (`run_status`, `row_status`, `step_status`). `events` answers “what happened in order”; “all failed rows” is an indexed query on `run_rows`.

`row_status` includes `previewed` (dry-run rehearsed, not saved). That is **not** `skipped`.

## Telemetry rule

Every remote write goes through `RunStore._safe`. Postgres down, schema missing, or network error → log a warning, keep automating. The in-memory `state` dict is what `/api/runs/{id}` polls, so the console works before the DB exists and after it disappears mid-run.

`/api/health` may show `store_healthy: false`; `/api/ping` must not care.

## Ownership (`created_products`)

Unique `(base_url, sku)`. Global, not per-run — it must outlive a run and be shared by workers.

| Operation | Symbol | Effect |
|---|---|---|
| Claim | `RunStore.claim_product` | INSERT … ON CONFLICT UPDATE, **`deleted_at = NULL`** |
| List ours | `owned_skus` | `deleted_at IS NULL` |
| Release | `release_product` | stamp `deleted_at` (soft) |

Clearing `deleted_at` on reclaim is load-bearing: a rolled-back then recreated SKU would otherwise stay invisible to `owned_skus` and to rollback.

Local fallback: `var/created-products.json` (relative path `var/created-products.json` from **cwd** — Dockerfile `WORKDIR /app` so it lands next to `ROOT/var`). Unlocked read-modify-write. **GAP** under concurrency.

## What is stored vs redacted

`runs.options` JSON: `{commit, on_existing}` from `RunManager.start`. No password.

`run_rows.payload` JSONB: the parsed row verbatim (includes sheet data, not admin passwords).

`llm_inferences`: input/output of tier 3, `accepted` = survived verification.

## Connection pool

`ThreadedConnectionPool` min 1 max 8, `connect_timeout=15`. Callers must not hold a connection across Playwright waits. Logs DSN **without** userinfo (`split("@")[-1]`).

`healthy()`: `SELECT id FROM runs LIMIT 1`. `UndefinedTable` → schema not applied.

## Schema vs app (gaps)

| In schema | Used by Python today? |
|---|---|
| `runs.worker_id`, `lease_expires_at` | Written (`_worker_id`) / unused for claiming |
| Views `failure_summary`, `step_health` | **No** — UI aggregates from the snapshot |
| `colour_swatches` | `app/colour/swatch.py` when store enabled |
| Row-level security | **Not implemented** |

## Non-goals

- Automated event retention
- Making telemetry a deploy gate
