# Web console and HTTP API

**Role:** Public contracts, what the browser may see, and UI behaviour that looks surprising but is intentional.
**Not for:** Runner internals ([`AUTOMATION.md`](AUTOMATION.md)) or Postgres DDL ([`STORE.md`](STORE.md)).

Verified against: `app/web/server.py`, `app/web/runs.py`, `app/web/static/index.html`, `app.js`, `app.css`, `app/config.py` (**2026-08-17**).

## Topology

One origin serves HTML/CSS/JS and `/api/*`. There is no API base-URL injection and no CORS traffic as shipped. `CORS_ALLOW_ORIGINS` (default `*`) keeps allowed origins out of the source. `allow_credentials` is **not** set: the admin password travels in JSON, never a cookie.

Static assets: `Cache-Control: no-cache, must-revalidate` plus `?v=<mtime>` on CSS/JS. Wrong cache = black 300×150 SVG rectangles.

## Authn / authz

**None.** Known gap. A public URL is a public admin tool: server-side `ADMIN_*` means a visitor does not need nopCommerce credentials of their own.

## Accepted UX — connection form is hidden

```css
/* app/web/static/app.css */
.conn { display:none; }
```

Markup for URL/email/password **stays in the DOM** because `readCreds` / `restoreCreds` / the password toggle query those ids; deleting the nodes throws at load. Empty fields fall back per-field to `ADMIN_*` in `RunManager.start`.

**Do not unhide** as a drive-by fix. Unhiding without web auth puts a password field on an unauthenticated page that can already start runs.

localStorage: `nop.theme`, `nop.admin` (`base_url` + `email` only). Password is never stored.

## API contracts

### `GET /api/ping`

`{"ok": true}` — no DB, no settings. Railway healthcheck.

### `GET /api/health`

| Field | Meaning |
|---|---|
| `admin_configured` | all three of base URL, email, password present on the **server** |
| `admin_base_url`, `admin_email` | values (not secrets per code comment) |
| `llm_enabled`, `llm_model` | key present / model name |
| `store_configured` | `DATABASE_URL` set |
| `store_healthy`, `store_detail` | live `SELECT` against `runs` |
| `colour_map_entries` | `len(MAP.entries)` in this process |

Must **not** grow a `password` or `database_url` field.

### `POST /api/parse`

Accept `.xlsx` / `.xlsm`. Body is `ParseResult.to_payload()` plus `filename`. Envelope fields: `source_file`, `sheets`, `total_rows`, `valid_rows`, `sheet_errors`, `sheet_warnings`, `rows`.

Row fields the UI uses: `sku`, `product_name`, `source_sheet`, `spec_value`, `colors`, `sizes`, `combinations`, `errors`, `warnings`, `duplicates`.

### `POST /api/runs` body (`StartRun`)

`rows`, `commit=false`, `filename`, `base_url`, `email`, `password`. Each credential falls back independently to `settings()`. Returns `{run_id}`. 400 on `ConfigError` or missing admin URL/creds.

### `GET /api/runs`

Newest first. **Strips** `rows` and `events`. **In-process only** — restart empties the list even if Postgres still has history. GAP.

### `GET /api/runs/{id}` snapshot (`RunStore.snapshot`)

`id`, `status`, `source_filename`, `commit`, `target_base_url`, `total_rows`, `completed`, `counts`, `colour_tiers`, `llm_cost_usd`, `error`, timestamps, `remote`, `rows` (payload order), `events` (last 80).

Row objects include `steps` and `error_class`. Must not include admin passwords.

### Rollback

`GET …/rollback` → `Plan.as_dict()`: `products`, `excluded`, `blocked`, `count`, `base_url`.

`POST …/rollback` body: `skus` (echo of what was shown), optional creds. 409 if confirmation set ≠ recomputed plan, run still in progress, store missing, etc.

UI: type `DELETE` **exactly** (case-sensitive) to arm. Button hidden unless `commit && counts.added && status !== running`.

### Colour review

`GET /api/colours` → `entries` + `pending` (unverified count).  
`POST /api/colours/confirm` → `MAP.record(..., source="human", verified=True)`.

## Run execution

Playwright sync API cannot run in the asyncio loop. Each run is a **daemon thread**. HTTP handlers only read snapshots. Semaphore `MAX_CONCURRENT = 4` (timeout 1800s to acquire; rollback waits 600s).

Reports: `var/reports/{run_id}.xlsx` (`REPORT_DIR`). Download may 404 if the volume is empty after a deploy without `/app/var`.

## UI behaviour agents must not “fix”

| Behaviour | Why |
|---|---|
| Preview is the primary button; Commit is a modal | Cheap protection against a bad sheet vs production |
| Clean rows start selected; warned/errored rows do not | A row you have not looked at should not enter the catalogue |
| Warned rows **can** be committed if ticked | Warning ≠ parser error; modal lists warnings |
| Failures grouped by `error_class` | Forty rows are usually two causes |
| ETA from **this run’s** finished-row median | Combination count dominates duration |
| Activity log rebuilds only when a signature changes | Avoid snapping open `<details>` shut every 1.5s poll |
| Product URLs in the row table are `target=_blank` | Admin is a different origin |

## Dual-path: UI commit vs `main.run` commit

The console never sends blocked rows if they stay unticked. `main.run` still refuses commit if **its** `rows` argument contains `errors`. Both gates are real. Do not remove the server gate because the UI “already” filters, or vice versa.

## Non-goals

- Cookie sessions / CSRF tokens (not built)
- Reloading finished runs from Postgres into `GET /api/runs` after a process restart
- A separate frontend build
