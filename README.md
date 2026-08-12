# nopCommerce product upload

Upload a product workbook, and it creates the products — with their
specification, colour and size attributes, generated combinations, and the
right child SKU on each combination.

    xlsx -> parser -> rows -> runner (Playwright) -> nopCommerce admin
                                 |                        |
                                 +-> run store            +-> report.xlsx
                                     (Postgres)               (re-uploadable)

## Running it

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env          # then fill in ADMIN_* — see Configuration
python -m app.web.server      # http://127.0.0.1:8000
```

Tests, which need neither a browser nor credentials:

```bash
pytest                        # unit tests
pytest -m live                # drives a real admin; needs ADMIN_* set
pytest -m "live and cleanup"  # DELETES the products this automation created
```

## Configuration

`.env` (gitignored — never commit it). Create it yourself; the keys are:

| Key | Required | Without it |
|---|---|---|
| `ADMIN_BASE_URL` / `ADMIN_EMAIL` / `ADMIN_PASSWORD` | **yes** | nothing can run |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | no | colour tier 3 is off; unknown colours fail loudly instead of being inferred |
| `DATABASE_URL` | no | runs are kept in memory only; the UI still works, history is lost on restart |

Both integrations are optional on purpose. **Telemetry is never allowed to break
the automation** — if Postgres is unreachable mid-run, every write is dropped
with a warning and the products still get created.

### One manual step: the database schema

Apply the schema once against the Postgres instance (Railway Postgres, local, etc.):

```bash
python sql.py app/store/schema.sql
```

It is idempotent, so re-running it after a change is safe. Until it is applied
the header shows `store: schema missing` and runs simply are not recorded.

## How a run works

**Preview** (the default) signs in, resolves every colour code, and fills the
create form — then stops without saving. **Commit** creates products for real.

A preview cannot check attributes or combinations: nopCommerce only exposes
those tabs once a product exists. It checks the data and the form, which is
where most problems are anyway.

Per row, with the guard that matters:

| Situation | What happens |
|---|---|
| SKU exists, this automation created it | resumed — finishes configuring it |
| SKU exists, somebody else created it | **skipped, untouched** |
| SKU is new | created, then configured |

Several catalogue SKUs already exist as products nobody here made. Every product
this automation creates is recorded (`created_products`, or `var/created-products.json`
without Postgres) and that record is the only thing the cleanup path will delete.

Every write step is idempotent, so a run that dies halfway can simply be run
again — and the report it produces can be re-uploaded to finish the batch.

## Colour codes

Child SKUs carry a short code per colour (`CP-V-200 LE 2XL` — `LE` is
Hi-Viz Lime). The payload never says which code is which colour, so it is
resolved in three tiers:

| Tier | Source | Cost |
|---|---|---|
| 1 | curated map, scoped per product family | free |
| 2 | shape heuristic (`combination_skus.score`) | free |
| 3 | OpenAI, over what is left | ~$0.0001 |

**The map is scoped, not flat**, and this is not premature generality — the
shipped payload contains a direct counterexample:

    HardHat   Hi-Vis Lime -> HLE
    Vest      Hi-Viz Lime -> LE      Lime -> LE

The same colour, a different code per family. A flat map survives today only
because the `Vis`/`Viz` spellings differ.

Tier 3 is a **proposal, not a decision**: whatever it returns is verified
against the payload's own combination list (every combination consumed exactly
once, count equal to colours × sizes) before it is used. A hallucinated code
produces a failed row, never a wrong SKU in the catalogue. Model resolutions are
then held in the Colour map tab as *unconfirmed* until a human confirms them —
verification proves an assignment is self-consistent, which is not the same as
correct.

On the current payload tier 3 fires **0%** of the time; tiers 1 and 2 resolve
all 13 colours. That is the baseline to watch: a rising tier-3 share means the
map needs curating.

## Failures

Every failure is classified (`app/errors.py`), which is what makes retry policy
and triage possible:

* **transient** (grid detached, timeout, expired session) — retried automatically
* **deterministic** (unresolvable colour, bad row, changed selector) — **never
  retried**; five attempts would spend five minutes reaching the same answer

The UI groups failures by *cause*, not by row. A run with forty failures is
usually two problems.

## Deploying

One container serves both the console and the API, so there is one thing to
deploy, one origin, and no CORS to configure. Full Railway inventory and
runbook: [`docs/RAILWAY.md`](docs/RAILWAY.md).

    Dockerfile -> Railway -> https://<service>.up.railway.app

The image is built from `mcr.microsoft.com/playwright/python`, which already
carries Chromium and its system libraries. **Its tag and the `playwright==`
pin in `requirements.txt` must move together** — Playwright locates browsers by
version, so a mismatch fails at run time with "Executable doesn't exist", not
at build time. The build then launches headless Chromium for real and fails if
it cannot, so a broken browser stops the deploy instead of somebody's upload.

`python -m app.web.server` is the entry point in every environment. It binds
`HOST` (default `127.0.0.1`) and `PORT` (default `8000`); the Dockerfile sets
`HOST=0.0.0.0` and Railway injects `PORT`.

| Setting | Value |
|---|---|
| Build | Dockerfile, via `railway.json` |
| Healthcheck | `/api/ping` — liveness only, so an unreachable Postgres cannot fail a deploy |
| Variables | everything in `.env.example`; only the `ADMIN_*` block is required. Link Railway Postgres and set `DATABASE_URL=${{Postgres.DATABASE_URL}}` |
| Volume | mount at `/app/var` to keep reports and the local ledger across deploys |

How deploys are triggered depends on how the Railway service is wired: see
[`docs/RAILWAY.md`](docs/RAILWAY.md). There is no separate front-end pipeline,
because there is no separate front end.

Two things to know before giving anyone the URL:

* **There is still no authentication** — see Known gaps. A public URL is a
  public admin tool, and `ADMIN_*` being set server-side means a visitor does
  not need credentials of their own to start a run.
* Without a volume `var/` is ephemeral: run reports and the local
  created-products ledger do not survive a redeploy. Postgres covers the run
  history and the ledger; the report `.xlsx` files have no remote store.

## Layout

```
app/
  config.py        settings; the only place secrets are read
  errors.py        the error taxonomy + retry policy
  parser.py        xlsx -> rows (refuses columns it does not understand)
  colour/          the three-tier resolver + the curated map
  store/           Postgres run store; schema.sql lives here
  automation/      run_batch — the runner main.py drives
  web/             FastAPI API + the upload console
main.py            entry point: owns the browser, refuses unsafe runs
library.py         Playwright helpers (steps, retries, nop widgets)
pages/             page objects: landing, manage products, product form
combination_skus.py  splits child SKUs into (colour, size)
xlsxCreator.py     rows + summary -> the re-uploadable report
docs/DESIGN.md     design notes (colour, store, concurrency); some parts shipped
docs/RAILWAY.md    as-built Railway + Postgres production configuration
docs/DEPLOYMENT-PLAN.md  why one container / Playwright image choices
```

## Known gaps

* **No authentication on the web layer.** Do not expose it as it stands.
* **Attributes beyond colour and size are not modelled.** A workbook with a
  third attribute column is *rejected* rather than silently part-created — see
  `docs/DESIGN.md` for what supporting it would take.
* **Concurrency needs Postgres.** The local JSON ledger is an unlocked
  read-modify-write, so two workers lose each other's entries. `created_products`
  fixes this; apply the schema before running anything concurrently.
