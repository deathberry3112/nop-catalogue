# Automation — parse, drive, report

**Role:** How a workbook becomes catalogue writes (or a preview), and the page-object contracts.
**Not for:** Colour-tier internals ([`COLOUR.md`](COLOUR.md)), Postgres schema ([`STORE.md`](STORE.md)), HTTP field tables ([`WEB.md`](WEB.md)).

Verified against: `app/parser.py`, `main.py`, `app/automation/runner.py`, `pages/*.py`, `library.py`, `combination_skus.py`, `xlsxCreator.py`, `app/rollback.py` (**2026-08-17**).

## Parser (`app.parser.parse_workbook`)

```
.xlsx → openpyxl → per sheet: header map → rows → _validate → ParseResult
```

`HEADER_MAP` is case-insensitive with collapsed whitespace. Two columns share the header `Product Attributes Type`: **first = colour attribute, second = size attribute** (positional).

Refusals (land on `row["errors"]`, not a thrown exception — the user should see every problem):

- Unknown headers (the silent-drop guard)
- Missing SKU / product name
- Duplicate parent SKU across the workbook
- Generate Combinations set without colours or child SKUs, or count ≠ colours × sizes

Warnings (row still `valid_rows`; UI starts them **unselected**):

- Duplicate colours/child SKUs — prefix `duplicated:` (`DUPLICATE_WARNING`); console offers “Remove duplicates”; **not** auto-removed
- Child SKU not starting with parent SKU — children are still written verbatim; `combination_skus.base_prefix` can recover the real base
- Missing or mis-labelled size attribute column — forced to `"Size"` so sizes are not filed as colours

`main.partition` then splits runnable / done / blocked. `main.run` refuses **commit** if `blocked` is non-empty **in the list it received**.

## Runner (`app.automation.runner.run_batch`)

Per row:

```
lookup find_by_sku
  exists and not owned → skipped (or fail if on_existing="fail")
  commit=false         → _preview_row  → status previewed
  commit and exists    → resume ProductPage
  commit and new       → add_new → fill_basics → save → claim_product + local ledger
                         → specification → _configure_attributes
```

`_configure_attributes` resolves colours **before** generating combinations. Swatch misses warn and continue (cosmetic). Combination grid mismatch raises `library.StepError` with `mismatch_message`.

Steps that mutate the admin go through `_retrying` (taxonomy in `app.errors`). Preview colour resolution uses `store.step` without that retry loop.

## Preview vs commit (accepted limitation)

Preview **cannot** check attributes or combinations: nopCommerce hides those tabs until the product exists. It checks data (colour bijection) and the create form (`library.validation_errors`). Documented in the UI copy (`index.html` action-note) — do not “complete” preview by saving.

## Combination SKUs (`combination_skus.py`)

- `split_combination` / `base_prefix` / `implied_size` recover `(code, size)` without assuming grid order.
- `score` ranks codes against colour names; a tie or a zero score is a refusal in `colour_codes`.
- `combination_map(row, codes)` is the **verification gate** the colour pipeline must pass.
- `OVERRIDES` is empty and expected to stay empty; add only for a named colour that failed as ambiguous.

`base_prefix` uses **whole tokens** only. A character-wise common prefix would eat codes like `C`/`CAF`.

## Page objects

Verified against **nopCommerce 3.x** (Kendo horizontal `#admin-menu`, not 4.x AdminLTE sidebar).

| Class | Job | Dual-path trap |
|---|---|---|
| `LandingPage.login` | Wait for `#admin-menu`, not for `#Email` to disappear | Login form also exists on the storefront if `base_url` loses `/Admin` |
| `ManageProductsPage.find_by_sku` | Exact SKU box | `search_by_name` is substring — never use it as an existence check |
| `ProductPage` | Create/edit form; spec AJAX reload via `_await_option_reload` (mark nodes, do not compare list content) | `library.SEL["save"]` does not match this admin (`input[name=save]`) |
| `ProductPage.delete` | Header `#product-delete` then modal confirm | Clicking the hidden submit waits forever |

`library.py` is version-agnostic helpers. Flows live in `pages/` because a generic helper that matches nothing is worse than no helper.

## Report (`xlsxCreator.write_report`)

12 template columns + Status + Note. Status precedence: runner result → row errors → carried-in done status → Pending. Re-upload carries Status into `row["status"]` so `DONE_STATUSES` skip already-handled rows.

Keep `xlsxCreator.TEMPLATE_HEADERS` and `app.parser.HEADER_MAP` in lockstep; `tests/test_parser.py` `test_a_written_report_parses_back` is the round-trip.

## Rollback (`app.rollback`)

Reads **Postgres**, never the in-memory manager, so it works after restart. Plan source of truth is `created_products`. Cross-check against `run_rows.status == 'added'` is reported in `excluded`, not acted on.

Live cleanup tests (`tests/test_delete_products.py`) use `var/created-products.json` from the add tests, **not** this module — a second deletion path for the pytest ledger. Do not conflate with HTTP rollback.

## Design why (from code comments, not invented)

- Unknown column → error: an unexpected attribute would otherwise create a product with only colour/size and a clean “Added” report.
- Size column repeating “Color”: `add_attribute` is idempotent **by name**, so the size pass would fetch the colour mapping and file “OS” as a ninth colour.
- Combinations re-read the grid each pass: Kendo rebinds `tbody` after Update; stale row handles left SKUs unset.
- Spec option reload: comparing option **text** cannot see a swap of identical lists; `dataset.nopSeen` detects new nodes. A hard hat was filed as `HighVisVest` (first option) when the AJAX response won the race.

## Non-goals

- Deleting stray attribute values automatically
- Parallelism within a run (one page, sequential rows)
- Using `library.retry` around `save`
