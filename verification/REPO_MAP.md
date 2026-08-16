# Living repo map

**Role:** Pin/contract map for this checkout. Re-verify facts with the snippets; do not treat dated numbers as laws.
**Not for:** Process sermons or the full invariant tables.

Verified against the tree on **2026-08-17**.

## What this product is

nopCommerce 3.x admin RPA: upload `.xlsx` → parse → Playwright create/resume products (spec, colour/size, combinations, child SKUs). One FastAPI process also serves the console. Optional Postgres + optional OpenAI.

## Pins (re-verify)

```bash
# Playwright pin must match Dockerfile FROM tag
grep -E 'playwright==|FROM mcr.microsoft.com/playwright' requirements.txt Dockerfile
```

As of this map: `playwright==1.61.0` and `mcr.microsoft.com/playwright/python:v1.61.0-noble`.

```bash
# Payload fixture (not a law)
python3 -c "import json; d=json.load(open('parsed_rows.json')); r=d['rows']; print(len(r), d.get('sheets'))"
```

## Entry points

| Entry | Command / symbol |
|---|---|
| HTTP server | `python -m app.web.server` → `app.web.server:api` / `main()` |
| Laptop setup | [`docs/LOCAL.md`](../docs/LOCAL.md) |
| Automation | `main.run(rows, RunOptions, Credentials, store=)` |
| Runner | `app.automation.runner.run_batch` |
| DDL | `python sql.py app/store/schema.sql` |
| Local dry-run | `python main.py` (uses `parsed_rows.json`; see Credentials default-URL dual path) |
| Report regen | `python xlsxCreator.py` |

## Public contracts

See [`docs/WEB.md`](../docs/WEB.md). Must not leak: `ADMIN_PASSWORD`, `OPENAI_API_KEY`, `DATABASE_URL`.

## Generated vs living

| Path | Kind |
|---|---|
| `/docs` | Living |
| `verification/REPO_MAP.md` (this file) | Living |
| `parsed_rows.json` | Fixture — consumed by tests and `main.DEFAULT_PAYLOAD` |
| `var/` | Ephemeral (gitignore) |
| `.env` | Secret (gitignore) |
| `report.xlsx` | Generated (gitignore) |

## Safety modules (open these, not docs alone)

`app/parser.py`, `app/colour/*`, `combination_skus.py`, `app/automation/runner.py`, `app/rollback.py`, `app/store/run_store.py`, `main.py` (`run`, `Credentials`, `RunOptions`), `pages/manage_products.py` `find_by_sku`, `pages/product_page.py`, `app/web/server.py`, `app/errors.py`.
