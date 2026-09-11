# Architecture invariants

**Role:** What must remain true, with a falsifiable proof in live code.
**Not for:** How to run locally ([`LOCAL.md`](LOCAL.md)), current payload counts, Railway IDs, or the change loop ([`CHANGE_PROCESS.md`](CHANGE_PROCESS.md)).

Verified against: modules named in the Proof column (**2026-08-17**). If a row and the file disagree, the file wins — fix this table in the same change set.

Rules for reading this file:

- An invariant is falsifiable by reading the cited symbol.
- It names the **real gate**, not a lookalike helper.
- Dual paths that must not be conflated are called out.
- Volatile facts (how many payload SKUs, how many colours, Railway URLs) do **not** belong here. Re-verify them in mechanism docs.

---

## Catalogue truth

| Invariant | Proof | Detail doc |
|---|---|---|
| An unrecognised workbook column is a **row-level error**; the row does not run. The pipeline must not create a product from a sheet it does not fully understand. | `app/parser.py` `_validate` (unknown_headers → `errors`); `tests/test_parser.py` `test_an_unrecognised_column_blocks_the_row` | [`AUTOMATION.md`](AUTOMATION.md) |
| Child-SKU count must equal `colours × max(1, sizes)` when Generate Combinations is set; a mismatch is an error, not a warning. | `app/parser.py` `_validate`; `combination_skus.py` `combination_map` expected-count check | [`AUTOMATION.md`](AUTOMATION.md) |
| Colour codes are **scoped** `(scope, colour_key)`, not a flat `{colour: code}`. Scope is the row’s `source_sheet`. Lookup is scoped then `global`. Vis/Viz is **not** folded by `normalise`. | `app/colour/store.py` `normalise`, `ColourMap.lookup`, `SEED`; `tests/test_colour_resolution.py` `test_the_same_colour_can_carry_different_codes_per_scope` | [`COLOUR.md`](COLOUR.md) |
| Every colour→code assignment — map, heuristic, LLM, or elimination — is used only after `combination_skus.combination_map` succeeds (every payload combination consumed exactly once). A failed verification is a failed row, never a wrong SKU. | `app/colour/resolver.py` `resolve` verification block; `combination_skus.py` `combination_map`; `tests/test_colour_resolution.py` `test_a_wrong_mapping_does_not_survive_verification` | [`COLOUR.md`](COLOUR.md) |
| Tier 3 is a **proposal**. Out-of-set colour/code pairs from the model are dropped. Accepted LLM hits are persisted `source='llm', verified=False`. A verified human/seed entry is not overwritten by an unverified proposal. | `app/colour/llm.py` `infer`; `app/colour/resolver.py` `_persist`; `app/colour/store.py` `ColourMap.record`; `tests/test_colour_resolution.py` `test_an_out_of_set_code_from_the_model_is_dropped`, `test_a_verified_entry_is_not_overwritten_by_a_proposal` | [`COLOUR.md`](COLOUR.md) |
| Absent `OPENAI_API_KEY`, tier 3 is off. Colour resolution must still work via map + heuristic. LLM is never a hard dependency of a run. | `app/config.py` `Settings.llm_enabled`; `app/colour/llm.py` `available`; `tests/test_colour_resolution.py` `test_every_payload_row_resolves_without_the_model` | [`COLOUR.md`](COLOUR.md) |
| Swatch ids are chosen only from the **live dropdown** (`ProductPage.swatch_options`). Duplicate labels without a map entry, missing names, and stale id/label pairs are **unresolved** — leave the swatch unset. Never raise; never guess by similarity. | `app/colour/swatch.py` `resolve_swatches`, `_verify`, `swatches_for_row`; `app/automation/runner.py` `_resolve_swatches`; `tests/test_colour_swatches.py` | [`COLOUR.md`](COLOUR.md) |
| `ProductPage.fill_basics` writes **only** name and SKU. Fields the payload is silent about keep the admin’s defaults. | `pages/product_page.py` `fill_basics` | [`AUTOMATION.md`](AUTOMATION.md) |
| Specification option matching is exact first, then case/spacing fold against the **closed list nop offers**. A tie of two folded labels is a `StepError`, not a coin flip. Near-miss typos must not resolve. | `pages/product_page.py` `resolve_option`; `tests/test_spec_options.py` | [`AUTOMATION.md`](AUTOMATION.md) |

---

## Mutation gates (preview / commit / skip / resume / rollback)

| Invariant | Proof | Detail doc |
|---|---|---|
| `RunOptions.commit` defaults to **False**. A run with no options must not write to the catalogue. | `main.py` `RunOptions`; `tests/test_run_options.py` `test_defaults_are_safe` | [`AUTOMATION.md`](AUTOMATION.md) |
| **Preview** (`commit=False`) may resolve colours and fill the create form; it must not save. Attribute/combination tabs are unreachable until a product exists — preview must not pretend it checked them. | `app/automation/runner.py` `_preview_row`; status `previewed` | [`AUTOMATION.md`](AUTOMATION.md) |
| **Commit** creates or resumes for real. `main.run` **refuses commit** if any row in the batch it received carries parser `errors`. | `main.py` `run` (“Commit refused”); `tests/test_run_options.py` `test_run_refuses_to_commit_a_partial_batch` | [`AUTOMATION.md`](AUTOMATION.md) |
| **Do not conflate** the commit gate with the UI: the console only POSTs **selected** rows (`app/web/static/app.js` `selectedRows`). Unticking a bad row is a real fix. The server still refuses if those selected rows include errors. | `app/web/static/app.js` `syncActions`, `startRun`; `main.py` `partition` / `run` | [`WEB.md`](WEB.md) |
| SKU lookup before every add is **exact** (`#GoDirectlyToSku`), not the product-name search. Name search is substring and would skip an add that should happen. | `pages/manage_products.py` `find_by_sku` vs `search_by_name`; live `tests/test_add_products.py` `test_sku_lookup_is_exact_while_the_name_search_is_not` | [`AUTOMATION.md`](AUTOMATION.md) |
| If the SKU exists and is **not** in the ownership set: **skip, open nothing, change nothing** (`on_existing="skip"`, default). `on_existing="fail"` raises instead. Do not conflate this with preview. | `app/automation/runner.py` `run_batch` (`existing and sku not in owned`); status `skipped` | [`AUTOMATION.md`](AUTOMATION.md) |
| Ownership is `RunStore.owned_skus(base_url)` **union** the local JSON ledger. Postgres `created_products` is the concurrency-safe record; `var/created-products.json` is an unlocked fallback — **not** safe for two workers. | `app/automation/runner.py` `owned`, `_local_ledger_skus`; `app/store/run_store.py` `claim_product`, `owned_skus` | [`STORE.md`](STORE.md) |
| `claim_product` runs in **exactly one place**: immediately after a **new** product’s first successful save. Resume / skip / preview never claim. | `app/automation/runner.py` `run_batch` (create/`save` path only) | [`STORE.md`](STORE.md) |
| Rollback's deletion set is **this run's rows**: `run_rows WHERE run_id = ? AND status in {added, failed, resumed, skipped}`. Payload SKUs and `previewed`/`pending` rows are never in it. **Widened 2026-09-11** from "only what this run created" (`created_products WHERE run_id = ?`) on explicit operator instruction — it now reaches products an earlier run created and products the **client** created that this run only looked up. | `app/rollback.py` `build_plan`, `DELETABLE_STATUSES`; `tests/test_rollback.py` | [`AUTOMATION.md`](AUTOMATION.md) |
| Ownership did not stop mattering — it became per-product. Every `Doomed` carries `origin`: `created` (live claim, this run), `resumed` (live claim, earlier run), `foreign` (no claim, row `skipped` — the client's), `unclaimed` (no claim, but the run wrote to it). `owned` is `origin in {created, resumed}`. The dialog **must** group on it and must never preselect an unowned product. | `app/rollback.py` `Doomed.origin`, `CLAIMED_ORIGINS`; `app/web/static/app.js` `RB_TIERS`, `rbWord`; `tests/test_rollback.py` | [`AUTOMATION.md`](AUTOMATION.md) |
| A live claim stamped **after** `runs.finished_at` excludes that SKU: a later run created what stands there now, so it is that run's to undo. Cannot-tell (missing or unparseable timestamp) never excludes. | `app/rollback.py` `_postdates`, `_as_datetime`; `tests/test_rollback.py` `test_a_later_runs_replacement_is_not_this_runs_to_delete` | [`AUTOMATION.md`](AUTOMATION.md) |
| `execute` releases the ownership claim **only** for `owned` products — there is nothing to retire on one the automation never claimed — and reports what it deleted without a claim as `unowned_deleted`. | `app/rollback.py` `_release`, `execute`; `tests/test_rollback.py` | [`STORE.md`](STORE.md) |
| Rollback is refused when the store is not configured, the run is still `queued`/`claimed`/`running`, or the run is unknown. `confirm_skus` is a **check**, not an instruction — disagreement raises `ConfigError` (HTTP 409). | `app/rollback.py` `build_plan`; `app/web/runs.py` `RunManager.rollback`; `app/web/server.py` `rollback_run` | [`WEB.md`](WEB.md) |
| Immediately before the delete click, the edit page SKU (and recorded product id, if any) must match. nopCommerce reuses product ids after delete. | `app/rollback.py` `execute`; `pages/product_page.py` `delete` | [`AUTOMATION.md`](AUTOMATION.md) |
| `generate_combinations` is a no-op when combinations already exist (nop **appends**). Combination SKUs are applied by reading the Attributes cell, not by grid order. | `pages/product_page.py` `generate_combinations`, `apply_combination_skus` | [`AUTOMATION.md`](AUTOMATION.md) |
| Nothing in this codebase **deletes an attribute value**. A stray value from an earlier run survives re-run; the mismatch message must say so. | `app/automation/runner.py` `mismatch_message`; `tests/test_mismatch_message.py` | [`AUTOMATION.md`](AUTOMATION.md) |
| Re-uploaded reports skip rows whose `status` is in `DONE_STATUSES` (`added`/`done`/`success`/`complete`/`skipped`). Keep `main.DONE_STATUSES` in sync with `xlsxCreator.DONE_STATUSES`. | `main.py` `partition`, `_already_done`; `xlsxCreator.py` `DONE_STATUSES`; `tests/test_run_options.py` `test_every_done_status_is_treated_as_done` | [`AUTOMATION.md`](AUTOMATION.md) |

**Do not conflate row statuses:** `skipped` = someone else’s product, the runner left it **untouched**. `previewed` = dry-run of *our* path, nothing saved. `resumed` = ours, already existed, configuration continued. `added` = this run created it. `failed` after save may still be **claimed**.

All four of those except `previewed` are now rollback-eligible, but they are **not** equivalent: what the runner did to a `skipped` product is nothing, and deleting one destroys data this project did not create and cannot recreate. `status` says what the run did; `origin` says whose the product is. Keep them distinct.

**Do not conflate rollback UI vs server:** the Delete button is hidden unless `run.commit` and `counts.added + counts.failed + counts.resumed + counts.skipped` is non-zero (`app.js` `renderRun`). That now covers every status the server plan can reach, closing the gap where a run that saved and then died configuring had products to undo while the button stayed hidden. The button is still only a hint — `build_plan` refuses independently, and the UI must never be the thing that makes a deletion safe.

---

## Secrets, public contracts, observability

| Invariant | Proof | Detail doc |
|---|---|---|
| Nothing except `app.config.settings()` (and `main.Credentials.from_env` / `RunOptions.from_env` for the CLI/run object) reads secret env vars. `DATABASE_URL` never goes to the browser. | `app/config.py`; `app/web/server.py` module docstring | [`WEB.md`](WEB.md) |
| `Credentials.password` is `repr=False`. Health reports **presence** of admin config, plus `admin_base_url` and `admin_email` (treated as non-secret). The password never appears in `/api/health` or run snapshots. | `main.py` `Credentials`; `app/web/server.py` `health`; `tests/test_run_options.py` `test_password_never_appears_in_repr` | [`WEB.md`](WEB.md) |
| Admin URL must be `http` or `https`. `file://`, `javascript:`, etc. are `ConfigError`. | `main.py` `Credentials.__post_init__`; `tests/test_run_options.py` `test_non_http_admin_urls_are_rejected` | [`WEB.md`](WEB.md) |
| `/api/ping` is liveness only — no I/O. Deploy healthchecks must use ping, **not** `/api/health` (that round-trips Postgres). | `app/web/server.py` `ping`, `health`; `railway.json` `healthcheckPath` | [`OBSERVABILITY.md`](OBSERVABILITY.md) |
| Telemetry must never break automation. `RunStore._safe` swallows remote errors; missing `DATABASE_URL` degrades to the in-memory mirror. | `app/store/run_store.py` `_safe`, module docstring | [`STORE.md`](STORE.md) |
| `runs.options` is a redacted snapshot (`commit`, `on_existing`). Credentials are not stored. | `app/web/runs.py` `RunManager.start`; `app/store/schema.sql` `runs.options` comment | [`STORE.md`](STORE.md) |
| Run list snapshots drop `rows` and `events`. Detail snapshots cap events at the last 80 (in-memory tail 500). Events are narrative, not the status source of truth. | `app/web/runs.py` `list`; `app/store/run_store.py` `event`, `snapshot` | [`OBSERVABILITY.md`](OBSERVABILITY.md) |
| There is **no application authentication**. Anyone who can reach the HTTP origin can parse, start runs, confirm colours, and (with a store) roll back. This is a **known gap**, not an implied SSO. | `app/web/server.py` module docstring; [`RAILWAY.md`](RAILWAY.md) Security | [`WEB.md`](WEB.md) |

**Do not conflate credential sources:**

| Path | Base URL when unset |
|---|---|
| `app.config.settings()` / web layer | `""` — `/api/runs` returns 400 “An admin URL is required” |
| `main.Credentials.from_env()` | hardcoded `https://propper.csp-1.com/Admin` |

`python main.py` without `ADMIN_BASE_URL` would drive that default host. The web path does not. Do not “fix” one to match the other without an explicit product decision.

**Do not conflate test credential loading:** `conftest.resolve_setting` reads env then `.env`. Production `settings()` loads `.env` only for keys **not** already in `os.environ` (`_load_env_file`).

---

## Retry, browsers, process topology

| Invariant | Proof | Detail doc |
|---|---|---|
| Deterministic `ErrorClass` values are **never** in `RETRY_POLICY`. Runner retries go through `_retrying` + `classify`, not a blanket `library.retry` on a save. | `app/errors.py` `RETRY_POLICY`; `app/automation/runner.py` `_retrying`; `tests/test_errors.py` `test_deterministic_failures_are_never_retried` | [`OBSERVABILITY.md`](OBSERVABILITY.md) |
| `main.py` owns Chromium launch. `run_batch` must receive an already-created `page` and must not call `sync_playwright()`. | `main.py` `run`; `app/automation/runner.py` module docstring | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| `--no-sandbox` is justified **only** because the browser visits one admin the operator configured — never arbitrary user URLs. | `main.py` `CHROMIUM_ARGS` comments | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| `playwright==` in `requirements.txt` and the Dockerfile `FROM` tag must move together. | `requirements.txt`; `Dockerfile` | [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) |
| `RunManager` is in-process. `MAX_CONCURRENT = 4`. Replicas must stay at 1. Schema columns `worker_id` / `lease_expires_at` are **unused** (no claim loop). | `app/web/runs.py`; `app/store/schema.sql` | [`STORE.md`](STORE.md), [`RAILWAY.md`](RAILWAY.md) |

**Do not conflate retry helpers:** `library.retry` is a generic DOM helper. Catalogue retry **policy** is `app.errors.RETRY_POLICY` applied by `runner._retrying`. Saving a product must not be wrapped in `library.retry` (a retried save can create twice — stated in `library.retry` docstring).

**Do not conflate colour resolvers:** `app.colour.resolve` is the production pipeline. `combination_skus.colour_codes` is the original single-tier heuristic still used when `combination_map(row)` is called **without** `codes`. The runner always passes `resolution.codes`.

---

## Prompt vs code (LLM surface)

| Behaviour the prompt asks for | Enforced in code? |
|---|---|
| Match colours to supplied codes; never invent a code | **Yes** — `infer` drops pairs outside `colours`/`codes`; `resolve` then `combination_map` | 
| Complete bijection | **Yes** — leftover colours → `Resolution.error`; verification count check |
| `temperature=0`, JSON schema strict | **Yes** — `llm.py` `infer` (`response_format` json_schema strict) |
| Timeout 10s, 2 transport retries | **Yes** — `TIMEOUT_SECONDS`, `TRANSPORT_RETRIES` |
| Persist only after human confirm | **No** — LLM hits are persisted immediately as `verified=False` and **used** on the next map lookup. Human confirm promotes `verified=True`. **Accepted behaviour**, documented in [`COLOUR.md`](COLOUR.md) |
| Kill switch without API key | **Yes** — `available()` / `llm_enabled` |

SYSTEM_PROMPT itself is **PROMPT-ONLY** for the *style* of matching (initials+last letter vs dropped qualifier). Correctness is the code gate, not the prose.

---

## Known gaps (real, not features)

| Gap | Evidence |
|---|---|
| No web-layer authentication | `app/web/server.py` |
| Connection form hidden (`app.css` `.conn { display:none }`); operators cannot type per-request admin creds in the shipped UI. Runs use server `ADMIN_*`. **Accepted UX** — do not “fix” by unhiding without adding auth. | `app/web/static/app.css` |
| Attributes beyond colour and size are not modelled (unknown column → error) | `app/parser.py`; README Known gaps |
| Local JSON ledger is unlocked RMW; concurrent workers need Postgres | `app/automation/runner.py` `_record_local` |
| `RunManager` does not hydrate the run list from Postgres after restart | `app/web/runs.py` `_runs` |
| SQL views `failure_summary` / `step_health` are unused by Python; the UI groups failures from the in-memory snapshot | `app/store/schema.sql`; `app.js` `renderFailures` |
| Multi-worker leases not implemented | schema columns only; `RunManager` docstring |
| No GitHub Actions / CI in this repo; Railway builds on `main` but does not run pytest | no `.github/`; `railway.json` |
| `main.Credentials.from_env` hardcoded default admin URL | `main.py` `Credentials.from_env` |
| `main.resolve_runner` falls back to `_stub_run_batch` if import fails. Production imports `run_batch`. The stub is unused while the runner module is present. | `main.py` `resolve_runner`, `_stub_run_batch` |
| No HTTP/API automated tests | `tests/` is parser/colour/rollback/unit + live Playwright |

## GAP — recommended tests

See [`CHANGE_PROCESS.md`](CHANGE_PROCESS.md). Do not list tests here as passing unless they exist and you have seen them.
