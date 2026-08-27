# Local development

**Role:** Get the console running on a Windows or Mac laptop, correctly.
**Not for:** Railway / Docker production ([`RAILWAY.md`](RAILWAY.md), [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md)) or product laws ([`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md)).

Verified against: `app/config.py`, `app/web/server.py`, `main.py` `Credentials.from_env`, `.env.example`, `sql.py`, `conftest.py`, `requirements.txt` (**2026-08-17**).

## What you are starting

One Python process. It serves the upload console **and** drives a real nopCommerce 3.x admin with Chromium.

You are **not** starting a local mesh. There is no `docker-compose` in this repo, no Redis, no Node build, and **no local database required**.

| Piece | Required to open the console? | Required to Preview / Commit? |
|---|---|---|
| Python 3 + pip | yes | yes |
| Packages in `requirements.txt` | yes | yes |
| Playwright Chromium | no (console still loads) | yes |
| `.env` with `ADMIN_BASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` | no (health will show not configured) | **yes** |
| A nopCommerce 3.x admin those creds can open | no | **yes** |
| Postgres (`DATABASE_URL`) | no | no |
| OpenAI (`OPENAI_API_KEY`) | no | no |

Postgres is only a **run store** (history, colour-map persistence, ownership ledger). It is not the product catalogue. The catalogue is nopCommerce.

## 1. One-time setup

Work from the **repo root** (the folder that contains `main.py` and `app/`). The local ownership ledger is the relative path `var/created-products.json` from the current working directory.

### Python

You need Python 3 with `pip` on your PATH.

- macOS / Linux: `python3 --version`
- Windows: `python --version` (if that fails, try `py -3 --version` and use `py -3` in place of `python` below)

A virtualenv is recommended (`.venv/` is gitignored):

```bash
python -m venv .venv
```

Activate it:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows cmd
.venv\Scripts\activate.bat

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

After activation, `python` and `pip` are the ones inside `.venv`.

### Install the app and Chromium

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

`playwright==` in `requirements.txt` and the Chromium that command downloads must match. As of this doc that pin is `playwright==1.61.0` — re-check the file if install errors mention a missing executable.

There is no npm step.

### Create `.env`

```bash
# macOS / Linux
cp .env.example .env

# Windows cmd
copy .env.example .env
```

Open `.env` and fill **all three** admin keys. The URL must include the `/Admin` path and must be `http` or `https`:

```
ADMIN_BASE_URL=https://your-shop.example.com/Admin
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=your-admin-password
```

Leave `DATABASE_URL` and `OPENAI_API_KEY` blank until you want those (sections below).

`.env` is gitignored. Never commit it.

The real process environment **wins** over `.env`. If a key is already set in your shell (even to empty), the file will not override it.

`HOST` and `PORT` already default to `127.0.0.1` and `8000`. You normally set neither locally.

## 2. Start the console

From the repo root, with the venv active:

```bash
python -m app.web.server
```

You should see `serving on http://127.0.0.1:8000`. Open that URL in your browser.

Expected startup lines when optionals are off (harmless):

- `run store unavailable (not configured) — runs will not be recorded`
- `OPENAI_API_KEY not set — colour tier 3 is off`

There is **no login screen**. The connection form is hidden on purpose; runs use the `ADMIN_*` values from the server. Do not unhide it as a local “fix”.

After you edit `.env`, **restart** this process. Settings are loaded once at import.

## 3. Check it worked

In a second terminal (or the browser):

```bash
# macOS / Linux
curl -s http://127.0.0.1:8000/api/ping
curl -s http://127.0.0.1:8000/api/health
```

Windows PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/ping
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

| Check | Healthy local (no Postgres, no OpenAI) |
|---|---|
| `/api/ping` | `{"ok": true}` |
| `admin_configured` | `true` once all three `ADMIN_*` are set |
| `llm_enabled` | `false` without an API key |
| `store_configured` | `false` without `DATABASE_URL` |
| `store_healthy` | `false`, `store_detail` typically `"not configured"` |

`/api/health` never includes the admin password or `DATABASE_URL`. It does include `admin_base_url` and `admin_email`.

If `admin_configured` is `false`, a run will fail with “An admin URL is required” (or missing email/password). That is the web path: it does **not** guess a host.

## 4. First run (Preview)

This automation can write a live catalogue. First time: **Preview**, not Commit.

1. Upload an `.xlsx` / `.xlsm` workbook (there is no sample workbook in the repo).
2. Read the parse table. Unknown columns **block the row**; they are not ignored.
3. Start a Preview. Chromium launches headless, signs in, fills the create form, and **does not save**.
4. Only use Commit when you intend to create products on that admin.

To watch the browser:

```
NOP_HEADED=1
```

in `.env` (or export it in the shell) and restart. `NOP_SLOWMO=300` adds a delay between actions. Headed mode needs a display — it is for a laptop, not a server.

## 5. Optional: Postgres

Skip this until you care about run history across restarts, the Colour map surviving a restart, or HTTP rollback (rollback **requires** the store).

1. Run any Postgres you already use (Postgres.app, Homebrew, Docker, or a remote instance). This repo does not start one for you.
2. Put a wire-protocol URL in `.env`:

   `DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/DBNAME`

   `postgres://` is accepted; `sql.py` normalises it to `postgresql://`.
3. From the repo root, apply the schema **once** (idempotent, one transaction):

   ```bash
   python sql.py app/store/schema.sql
   ```
4. Restart `python -m app.web.server`. `/api/health` should show `store_configured: true` and `store_healthy: true`.

Until the schema is applied, health often reports `UndefinedTable: relation "runs"`. Product writes still happen; telemetry writes are dropped.

Without Postgres, ownership falls back to `var/created-products.json` (unlocked; not safe for two workers). The in-memory run list is empty after a restart even if Postgres still has rows — that is a known gap, not a misconfiguration.

## 6. Optional: OpenAI (colour tier 3)

Without `OPENAI_API_KEY`, colours still resolve from the curated map and the heuristic. Unknown colours **fail the row** instead of being inferred. That is enough for the checked-in fixture colours.

Set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`, default `gpt-4o-mini`) only if you want that fallback. Restart the server.

## 7. `python main.py` is not the console

```bash
python main.py
```

runs the checked-in `parsed_rows.json` in dry-run and writes `report.xlsx` at the repo root. It does **not** serve the UI.

This path uses `Credentials.from_env()`. If `ADMIN_BASE_URL` is unset, it defaults to a **hardcoded** admin host (`https://propper.csp-1.com/Admin`) — not an empty URL. Set `ADMIN_*` in `.env` before using it, or you will drive that host.

The web console does not use that default. Do not “fix” one path to match the other.

## 8. Tests

`pytest` is **not** listed in `requirements.txt`. Install it when you want tests:

```bash
python -m pip install pytest
pytest                        # offline; no browser, no ADMIN_* required
pytest -m live                # real Chromium + real admin; needs ADMIN_*
pytest -m "live and cleanup"  # DELETES products in the local ledger — never a default
```

Live tests read `ADMIN_*` from the environment, then `.env`. They do **not** fall through to the hardcoded CLI host. Which file proves which law: [`../tests/README.md`](../tests/README.md).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Admin URL is required` / `admin_configured: false` | Empty `ADMIN_*` in `.env`, or a shell variable blocking `.env`. Restart after edits. |
| `Admin URL must be http(s)` | Missing scheme, or a `file://` path. Include `https://…/Admin`. |
| `Executable doesn't exist` | Playwright package installed, Chromium not. Re-run `python -m playwright install chromium` with the **same** `python` that runs the server. |
| Console loads, run never starts a browser | Chromium missing, or `ADMIN_*` incomplete. Check the server terminal, not only the UI. |
| `store_healthy: false` with `UndefinedTable` | `DATABASE_URL` set but `python sql.py app/store/schema.sql` not run. |
| `store_healthy: false` with `not configured` | No `DATABASE_URL` — expected. Console still works. |
| Run list empty after restart | In-process manager; list is not reloaded from Postgres. Known gap. |
| Ledgers / reports in a surprising folder | Process was not started from the repo root. |
| Drove the wrong shop with `python main.py` | Unset `ADMIN_BASE_URL`; CLI hardcoded default. Use `.env`. |
| PowerShell refuses `Activate.ps1` | Execution policy. Use `.venv\Scripts\activate.bat` from cmd, or `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. |

## What not to do

- Do not bind `HOST=0.0.0.0` on a laptop and share the URL. There is **no web authentication**; anyone who can reach it can start runs with the server’s `ADMIN_*`.
- Do not Commit against a customer catalogue as a first smoke test. Preview first.
- Do not unhide the connection form to “type creds in the UI”.
- Do not add a second frontend, CORS, or a local Docker stack to make this work. The shipped local path is the commands above.
- Do not commit `.env`, `var/`, or `report.xlsx`.
