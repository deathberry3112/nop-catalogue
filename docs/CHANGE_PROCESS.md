# Change process

**Role:** The loop a change must follow, the safety gate, verification defaults, and this repo’s real breakage modes.
**Not for:** Product laws (those live in [`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md)) or mechanism walkthroughs.

Verified against: `main.py`, `app/automation/runner.py`, `app/parser.py`, `app/colour/`, `app/rollback.py`, `app/web/server.py`, `conftest.py`, `tests/` (**2026-08-17**).

```
Understand → Validate E2E → Log assumptions → Impact analysis → Risk invariants
  → Plan → Verify plan → Implement → Verify → Contracts/secrets → Update docs
```

Iterative: each new requirement restarts the loop. “This is a small tweak” is not a skip.

## Authority

1. Live application code
2. `/docs` (must match code)
3. Root `README.md`

When docs and code disagree: **trust code**, then fix the docs in the same change set. Never “fix” code to match an unused comment unless a human explicitly redesigns the product.

## Hard stop — safety gate

Do not edit a safety-sensitive area until you have:

1. **Read** [`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md) with the Read tool (**not from memory**)
2. **Opened** the live modules on the affected path (not docs alone)
3. **Cited** the specific invariant rows that apply

Then post a **visible change brief** (next section) before the first code edit. Skip the brief only for pure Q&A.

### Safety-sensitive areas

| Area | Why it can hurt | Doc | Code anchors |
|---|---|---|---|
| Colour codes / combination SKUs | Wrong child SKU in the live catalogue | [`COLOUR.md`](COLOUR.md), [`AUTOMATION.md`](AUTOMATION.md) | `app/colour/resolver.py` `resolve`, `combination_skus.combination_map` |
| LLM tier 3 | Hallucinated code written as fact | [`COLOUR.md`](COLOUR.md) | `app/colour/llm.py` `infer`, `resolver._persist` |
| Swatches | Wrong colour on the storefront, invisible until a customer sees it | [`COLOUR.md`](COLOUR.md) | `app/colour/swatch.py` `resolve_swatches`, `swatches_for_row` |
| Ownership / skip vs resume | Editing a client’s existing product | [`AUTOMATION.md`](AUTOMATION.md), [`STORE.md`](STORE.md) | `app/automation/runner.py` `run_batch`, `RunStore.claim_product` / `owned_skus` |
| Parser refusals | Silent drop of an unmodelled attribute | [`AUTOMATION.md`](AUTOMATION.md) | `app/parser.py` `_validate`, `HEADER_MAP` |
| Commit vs preview | Accidental writes to production | [`AUTOMATION.md`](AUTOMATION.md), [`WEB.md`](WEB.md) | `main.RunOptions.commit`, `runner._preview_row` |
| Rollback deletion | Deleting the wrong product | [`AUTOMATION.md`](AUTOMATION.md) | `app/rollback.py` `build_plan`, `execute` |
| Secrets / public API | Password, `DATABASE_URL`, or admin URL leakage | [`WEB.md`](WEB.md) | `app/config.py` `settings`, `app/web/server.py` `health`, `main.Credentials` |
| Admin writes / selectors | Half-configured products, stray attribute values | [`AUTOMATION.md`](AUTOMATION.md) | `pages/product_page.py`, `pages/manage_products.py` `find_by_sku` |
| Retry policy | Retrying a deterministic failure burns the batch | [`OBSERVABILITY.md`](OBSERVABILITY.md) | `app.errors.RETRY_POLICY`, `runner._retrying` |
| Deploy / Playwright pin | Browser missing at runtime, not at build | [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) | `requirements.txt` `playwright==`, `Dockerfile` `FROM` |

Do not hardcode volatile inventory counts (payload row counts, colour counts, Railway IDs) in process rules. Re-verify; see [`COLOUR.md`](COLOUR.md) and [`../verification/REPO_MAP.md`](../verification/REPO_MAP.md).

## Visible change brief (before first code edit)

1. **Success** — one sentence; user-visible vs internal
2. **E2E path** — entry → … → UI/API; modules opened
3. **Assumptions** — each resolved against code or marked blocking
4. **Impact** — must-change / must-not-change files; contracts at risk
5. **Invariants** — rows/sections cited from the invariants file
6. **Risks** — the failure modes in “Brainstorm breakage” below that apply
7. **Plan** — correct end-to-end behaviour, not the smallest incomplete patch

## After implementing

1. **Verify** the affected path (named offline tests for safety areas; live/e2e when UI or admin contracts change)
2. **Compatibility** — API / UI / schema / env; never expose secrets to the client
3. **Docs sync** in the **same change set**

## Dual-layer enforcement

For every law an LLM (or a hurried patch) could violate:

| Layer | Role |
|---|---|
| Prompt / instruction | Tell the model / agent the rule |
| Deterministic code | Gate, filter, scrub, validate, or refuse |
| Test | Prove the **code** layer (prompts are not a test) |

A rule that exists only in a prompt or a comment is **PROMPT-ONLY — FRAGILE**. Record it; do not pretend it is enforced. This harness documents gaps; it does not add sanitizers unless a later change asks.

This repo’s LLM surface is colour-code matching only (`app/colour/llm.py`). Topology vs prompts: the runner owns the graph (lookup → skip/preview/create → configure); the system prompt owns matching policy and is **not** allowed to invent codes. Verification is code (`combination_map`), not the prompt.

## Brainstorm breakage (this repo’s real risks)

Inventing records, acting before a readiness gate, breaking sticky state, bypassing the router, leaking debug, deploy-order fallbacks, dual-path confusion, secret leakage — specialised to **this** codebase:

| Risk | How it actually happens here |
|---|---|
| Invent a catalogue fact | Flat colour map; guessed swatch by string similarity; `fill_basics` writing fields the sheet did not specify |
| Act before a gate | Commit while parser `errors` remain on the **batch passed to** `main.run`; generate combinations before colours resolve; rollback while `runs.status` is `running` |
| Break ownership | Treat payload SKUs as “ours”; use name search instead of `find_by_sku`; lose `var/created-products.json` under concurrency; leave `deleted_at` set after recreating a SKU |
| Bypass the runner | Call page objects from a new entry point that launches its own browser; use the stub in `main.resolve_runner` in production |
| Conflate dual paths | `previewed` vs `skipped`; `/api/ping` vs `/api/health`; UI selected-rows commit vs `main.run` blocked-row refusal; `Credentials.from_env` default URL vs `settings().admin_base_url`; UI rollback button (`counts.added + failed + resumed + skipped`) vs server plan (`run_rows` by status); rollback row `status` (what the run did) vs `origin` (whose product it is) |
| Leak secrets | Serialise `Credentials` into a snapshot; put `DATABASE_URL` in a frontend doc; log the password (`LandingPage.login` logs email only — keep it that way) |
| Silent wrong product | Unrecognised workbook column dropped; second “Product Attributes Type” repeating “Color” filing sizes as colours; `generate_combinations` clicked twice (nop **appends**) |
| Retry a deterministic failure | Wrap colour resolution in `library.retry` instead of `runner._retrying` + `RETRY_POLICY` |
| Deploy-order trap | Bump `playwright==` without the Dockerfile `FROM` tag; healthcheck `/api/health` so Postgres can fail a deploy |
| Horizontal scale | Second replica: `RunManager` is in-process; volumes do not attach to multiple replicas |
| Unhide the connection form | `.conn { display:none }` is **accepted UX**. Unhiding it without adding web auth puts a password field on a public URL that already uses server-side `ADMIN_*` |

## Verification defaults

- Prefer **offline** tests for safety laws (`pytest` with no `-m live`).
- Use **live** browser tests when UI, routing, or admin DOM contracts change. They are **marker-gated**: a bare `pytest` never launches Chromium (`conftest.py` `_live_selected`).
- Cleanup/delete is **double-gated**: `@pytest.mark.cleanup` **and** `"cleanup"` in the `-m` expression.
- Live suites need `ADMIN_*` (env or `.env` via `resolve_setting` — **not** the same path as `app.config.settings()`).
- “Pass” means structure, flags, and refusals — not prose quality of LLM reasoning.
- Gitignored outputs: `var/`, `report.xlsx`, `.pytest_cache/`. Committed: tests and `parsed_rows.json`.

Named files: [`../tests/README.md`](../tests/README.md).

### Default offline set for safety-sensitive changes

```bash
pytest tests/test_parser.py tests/test_combination_skus.py \
       tests/test_colour_resolution.py tests/test_colour_swatches.py \
       tests/test_rollback.py tests/test_errors.py tests/test_run_options.py \
       tests/test_xlsx_report.py tests/test_report.py \
       tests/test_mismatch_message.py tests/test_spec_options.py \
       tests/test_spec_option_reload.py
```

If UI or FastAPI contracts change and no live/API test exists, that is a **GAP** — do not invent a passing test you did not run.

## GAP — recommended offline tests (do not exist today)

These are real holes, not implied coverage:

- HTTP contract tests for `/api/health` (password absent), `/api/ping` (no DB I/O), `POST /api/runs` 400s, rollback confirmation mismatch → 409
- `RunStore._safe` swallows remote errors and the run continues
- In-memory `RunManager` does not reload finished runs from Postgres after restart
- `Credentials.from_env` default base URL vs empty `settings().admin_base_url`

## Docs sync checklist

If you changed… update:

| Change | Docs |
|---|---|
| Flow / gates / dual paths | invariants + the matching mechanism doc |
| API / UI fields | [`WEB.md`](WEB.md) + tests README if a new harness exists |
| Schema / ledger | [`STORE.md`](STORE.md) + `app/store/schema.sql` itself |
| Colour / LLM | [`COLOUR.md`](COLOUR.md) |
| Deploy / pins | [`RAILWAY.md`](RAILWAY.md) / [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) — pins are facts, re-verify |
| New known gap | invariants Known gaps + this file’s GAP list |
