# Tests

**Role:** Which harness proves which law, how to run it, and what “pass” means.
**Not for:** Product mechanism narrative ([`../docs/AUTOMATION.md`](../docs/AUTOMATION.md)).

Verified against: `conftest.py`, `tests/*.py` (**2026-08-17**).

## How to run

```bash
pytest                        # offline unit tests; no browser, no ADMIN_* required
pytest -m live                # real Chromium + real admin; needs ADMIN_BASE_URL / EMAIL / PASSWORD
pytest -m "live and cleanup"  # DELETES ledger products; never part of a default run
```

Headed debugging uses the same knobs as production:

```bash
NOP_HEADED=1 NOP_SLOWMO=300 pytest -m live
```

Narrow the fixture payload:

```bash
NOP_ROW_SKUS=CP-30-C100,CP-V-200 pytest -m live
NOP_ROW_LIMIT=2 pytest -m live
```

There is no `pytest.ini` / `pyproject.toml`. Markers are registered in `conftest.py` `pytest_configure`.

## Gating (do not weaken)

| Gate | Mechanism |
|---|---|
| Bare `pytest` never launches Chromium | `conftest._live_selected`: skip unless `"live"` is in `-m` |
| Live tests need credentials | `admin_credentials` / per-test skips |
| Cleanup never runs under `pytest -m live` alone | `@pytest.mark.cleanup` **and** `"cleanup"` in the `-m` expression (`cleanup_requested`) |

Live credentials resolve **env then `.env`** via `conftest.resolve_setting` — **not** `app.config.settings()`. A missing `ADMIN_BASE_URL` fails the session rather than falling through to `Credentials.from_env`’s hardcoded host. That difference is load-bearing.

“Pass” means refusals, flags, SKU maps, and DOM assertions — not LLM prose quality.

## Offline vs live vs destructive

| Kind | Files | Network / admin |
|---|---|---|
| Offline | parser, combination_skus, colour_*, rollback, errors, run_options, xlsx/report, mismatch_message, spec_options, spec_option_reload | No |
| Live (read/navigate/write) | smoke_navigation, login, manage_products, add_products | Yes — add_products **writes** |
| Live + cleanup | delete_products | Yes — **deletes** `var/created-products.json` entries only |

Session-scoped live browser: one page, state leaks forward **by design**. Tests that need a known URL should `library.goto` themselves.

## Map: tests → laws

| Law | Test file (offline unless noted) |
|---|---|
| Unknown columns block the row | `test_parser.py` `test_an_unrecognised_column_blocks_the_row` |
| Combination count / bijection | `test_parser.py`, `test_combination_skus.py` |
| Scoped colour map; no silent LLM write | `test_colour_resolution.py` |
| Swatch: no guess, stale id refused | `test_colour_swatches.py` |
| Rollback set = claims, not payload | `test_rollback.py` |
| Deterministic errors never retried | `test_errors.py` |
| Commit default false; commit+blocked refused; password not in repr; non-http URL refused | `test_run_options.py` |
| Report Status honest / round-trip | `test_xlsx_report.py`, `test_report.py`, `test_parser.py` `test_a_written_report_parses_back` |
| Stray attribute value named in mismatch | `test_mismatch_message.py` |
| Spec option fold without inventing | `test_spec_options.py` |
| Spec AJAX reload detected by marking nodes | `test_spec_option_reload.py` (in-process Playwright page, not `live`) |
| Exact SKU lookup vs name search | `test_add_products.py` (live) |
| Do not delete client-owned payload SKUs | `test_delete_products.py` (live and cleanup) — ledger, not payload |

`test_spec_option_reload.py` launches Playwright against **local HTML**, not the admin. It is not marker `live`.

## Default verify set (safety-sensitive code)

```bash
pytest tests/test_parser.py tests/test_combination_skus.py \
       tests/test_colour_resolution.py tests/test_colour_swatches.py \
       tests/test_rollback.py tests/test_errors.py tests/test_run_options.py \
       tests/test_xlsx_report.py tests/test_report.py \
       tests/test_mismatch_message.py tests/test_spec_options.py \
       tests/test_spec_option_reload.py
```

When UI, routing, or admin DOM contracts change: add `pytest -m live` (and cleanup only when you intend to delete).

## Gitignored vs committed

| Committed | Gitignored |
|---|---|
| `tests/`, `conftest.py`, `parsed_rows.json` | `var/`, `report.xlsx`, `.pytest_cache/`, `__pycache__/` |

## Accepted behaviours — do not fail as bugs

- Live tests share one tab; isolation is not guaranteed
- Preview cannot assert attributes/combinations
- Payload SKUs that already exist and are not in the ledger are **skipped**, not deleted
- `test_the_payload_needs_no_model_at_all` failing means the fixture/map drifted — treat as a colour-map problem, not “turn on OpenAI in CI”

## GAP — no tests today

- FastAPI routes (`/api/health` password absence, ping, rollback 409)
- `RunStore._safe` swallow path
- `RunManager` restart / empty list
- Hidden `.conn` form / unauthenticated start

Do not invent passing tests you did not run.
