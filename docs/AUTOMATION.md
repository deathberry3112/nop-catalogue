# Automation — parse, drive, report

**Role:** How a workbook becomes catalogue writes (or a preview), and the page-object contracts.
**Not for:** Colour-tier internals ([`COLOUR.md`](COLOUR.md)), Postgres schema ([`STORE.md`](STORE.md)), HTTP field tables ([`WEB.md`](WEB.md)).

Verified against: `app/parser.py`, `app/configurator.py`, `main.py`, `app/automation/runner.py`, `app/automation/preflight.py`, `pages/*.py`, `library.py`, `combination_skus.py`, `xlsxCreator.py`, `app/rollback.py` (**2026-09-08**).

## Parser (`app.parser.parse_workbook`)

```
.xlsx → openpyxl → per sheet: header map → rows → _validate → ParseResult
```

`HEADER_MAP` is case-insensitive with collapsed whitespace, and names no attribute. **Attributes are positional, not hard-coded**: a `Product Attributes Type` column names an attribute, the next column holds its values, and a following `<values> Swatch` column holds its swatches. The type *cell* is the attribute's name; the column header is only the fallback when the cell is blank.

```
Product Attributes Type | Color         | Color Swatch | Product Attributes Type | Size
Product Attributes Type | Sign Material |              | Product Attributes Type | Sign Size
```

Colour and size are not special cases in the reader — they are the two attributes the clothing sheets happen to use. Adding an attribute is columns in the sheet, not a code change; `_check_grid`, `combination_skus.attribute_names` and the report's columns are all N-dimensional. What stays colour-specific is downstream and only engages when the row has a colour: swatch resolution and the legacy child-SKU code inference. `IMPLIED_ATTRIBUTES` lets an old sheet omit the second type column (see `SIZE_COLUMN_MISSING`).

Refusals (land on `row["errors"]`, not a thrown exception — the user should see every problem):

- Unknown headers (the silent-drop guard)
- Missing SKU / product name
- Duplicate parent SKU across the workbook
- Generate Combinations set without attribute values or child SKUs; or a gap in the grid — `_check_grid` takes the product of **every** filled attribute, so it names the exact combinations with no child SKU rather than comparing counts

Warnings (row still `valid_rows`; UI starts them **unselected**):

- Duplicate colours/child SKUs — prefix `duplicated:` (`DUPLICATE_WARNING`); console offers “Remove duplicates”; **not** auto-removed
- Child SKU not starting with parent SKU — children are still written verbatim; `combination_skus.base_prefix` can recover the real base
- Missing or mis-labelled size attribute column — forced to `"Size"` so sizes are not filed as colours

### Combined sheets (phase 1 + phase 2 on one sheet)

A sheet may carry **both** templates — the product columns, then the configurator ones. `_combined_boundary` recognises it and `_parse_combined` reads it into **one row per product**, with the locations attached to the product row.

```
Product name | SKU | … | Child SKU Varient | Save Product ‖ Configurator Areas | … | Save Product
                          phase 1                          ↑ boundary            phase 2
```

- **Ownership of a column is positional.** `configurator.block_starts_at` returns the leftmost phase-2 header; everything left of it is phase 1's, everything from it rightwards is phase 2's, and `SHARED_COMBINED_HEADERS` (Product name, SKU, Status, Note) belong to both. This is what resolves the template's **duplicated `Save Product` header** — first occurrence phase 1's, second phase 2's — with no special case. Each reader is handed the full-width header row with the other half masked to `None`, so neither reports the other's columns as unrecognised.
- **The halves count lines differently.** Phase 1 writes one line per variant, phase 2 one line per area, so a product with 7 sizes and 4 areas is 7 lines of which 4 also name a location. `configurator.parse_lines(require_location=True)` is what stops a size-only line reading as a location with no name — `_read_line`'s blank test asks whether any mapped cell is filled, and the shared SKU is one.
- **One row, not two.** Two rows would split the phases across separate lines of the report, so a re-uploaded report would no longer be the combined template. One row also fixes the order: the configurator tab lives on a product, so phase 2 cannot run until phase 1 has.
- A line with a SKU but a blank product half is still a **product** row, so the ownership guard applies to it. Only a line with no SKU at all stays a phase-2 row, and it fails as `SKU is required`.
- `BOOKKEEPING_FIELDS` keeps a report's Status/Note from making a phase-2-only line look like a variant.

`main.partition` then splits runnable / done / blocked. `main.run` refuses **commit** if `blocked` is non-empty **in the list it received**.

## Runner (`app.automation.runner.run_batch`)

Per row:

```
refuse unknown attributes         <- before any page load; see below
lookup find_by_sku
  exists and not owned → skipped (or fail if on_existing="fail")
  commit=false         → _preview_row  → status previewed
  commit and exists    → resume ProductPage
  commit and new       → add_new → fill_basics → save → claim_product + local ledger
                         → specification → _configure_attributes
                         → _configure_combined_areas   (only if row["locations"])
```

A combined row runs both phases inside one row: `_configure_combined_areas` re-opens the product (the combination grid is not the edit page) and calls the same `_configure_areas` a phase-2 row uses. Its status is **Added + Configured**, which is in both `DONE_STATUSES` sets so a re-uploaded report skips it.

### The attribute preflight (`app.automation.preflight`)

Attributes exist at two levels in nopCommerce. `Catalog > Attributes > Product attributes` defines the **names** store-wide; a product's own tab maps it to one of them. `ProductPage.add_attribute` drives the second, and its Kendo dropdown can only offer what the first holds — matched **exactly, case-sensitively** (`_pick_kendo`).

So `run_batch` reads that store-wide list once (`pages/product_attributes.py`) and refuses any row naming something it does not hold, **before the SKU lookup**. Without it the row fails at the dropdown *after* the product has been created and saved, leaving a half-configured product behind. Raised as `preflight.UnknownAttribute`, classified `DATA_INVALID` **by type** — the message quotes the store's own names, and a store spelling its attribute "Colour" would otherwise match the `DATA_COLOUR` text pattern.

Two deliberate asymmetries:

- **Never creates the attribute.** The list is store-wide, so a typo (`Sign Materal`) would add a row every product in the catalogue can see and no run would flag it. Somebody adds the name once in the admin; every product using it then runs untouched.
- **Unreadable ≠ empty.** `names()` returns the complete list or raises — including when the pager's own count disagrees with what was read — and the runner treats a raise as "cannot check", falling back to the late failure. A moved selector costs the early warning; it never refuses work the admin would have accepted.

This is also the one attribute problem **preview can catch**: the store's list needs no saved product.

`_configure_attributes` resolves colours **before** generating combinations. Swatch misses warn and continue (cosmetic). Combination grid mismatch raises `library.StepError` with `mismatch_message`.

Steps that mutate the admin go through `_retrying` (taxonomy in `app.errors`). Preview colour resolution uses `store.step` without that retry loop.

## Preview vs commit (accepted limitation)

Preview **cannot** check a product's attributes or combinations: nopCommerce hides those tabs until the product exists. It checks data (colour bijection), the create form (`library.validation_errors`), and — since it needs no product — the attribute preflight above. A combined row whose product **already exists** also gets its Picario references and thumbnails checked (`_preview_configurator`); one that does not exist has its locations named and explicitly not checked. Documented in the UI copy (`index.html` action-note) — do not “complete” preview by saving.

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
| `ProductAttributesPage.names` | The store-wide attribute list, read once per run | Read-only by design; a partial read raises rather than reporting real attributes as missing. Grid/pager selectors **unverified** against the live admin |

`library.py` is version-agnostic helpers. Flows live in `pages/` because a generic helper that matches nothing is worse than no helper.

## Report (`xlsxCreator.write_report`)

12 template columns + Status + Note; a combined sheet writes `combined_headers` (the product template, then `COMBINED_TAIL`) and `combined_to_lines`, which zips the variant lines and the area lines onto the same lines so the report re-uploads as the same combined sheet. Status precedence: runner result → row errors → carried-in done status → Pending. Re-upload carries Status into `row["status"]` so `DONE_STATUSES` skip already-handled rows.

Keep `xlsxCreator.TEMPLATE_HEADERS` and `app.parser.HEADER_MAP` in lockstep; `tests/test_parser.py` `test_a_written_report_parses_back` is the round-trip.

## Rollback (`app.rollback`)

Reads **Postgres**, never the in-memory manager, so it works after restart.

Plan source of truth is **`run_rows`** — the products *this run worked on*, `status in {added, failed, resumed, skipped}`. `created_products` is consulted per SKU for **provenance**, not membership, and every planned product carries it as `origin`:

| `origin` | claim | means | deleting it… |
|---|---|---|---|
| `created` | live, this `run_id` | this run made it | returns the catalogue to where it started |
| `resumed` | live, earlier `run_id` | an earlier run made it, this one edited it | undoes that earlier run's creation too |
| `unclaimed` | none | the run wrote to it, nothing proves we made it | may not be ours to delete |
| `foreign` | none, row `skipped` | the **client** made it; the run never opened it | destroys data nothing here can recreate |

`Doomed.owned` is `origin in {created, resumed}`. The scope was widened from `created` alone on **2026-09-11** by explicit operator instruction; `origin` is what keeps that defensible, and the dialog groups on it, never preselects an unowned product, and demands a longer confirmation phrase (`DELETE NOT OURS`) once the selection includes one.

A live claim stamped after `runs.finished_at` **excludes** the SKU: a later run created what is standing there now (`_postdates`). `execute` releases the claim only for `owned` products and reports `unowned_deleted` separately.

Live cleanup tests (`tests/test_delete_products.py`) use `var/created-products.json` from the add tests, **not** this module — a second deletion path for the pytest ledger. Do not conflate with HTTP rollback.

## Design why (from code comments, not invented)

- Unknown column → error: an unexpected attribute would otherwise create a product with only colour/size and a clean “Added” report.
- Size column repeating “Color”: `add_attribute` is idempotent **by name**, so the size pass would fetch the colour mapping and file “OS” as a ninth colour.
- Combinations re-read the grid each pass: Kendo rebinds `tbody` after Update; stale row handles left SKUs unset.
- Spec option reload: comparing option **text** cannot see a swap of identical lists; `dataset.nopSeen` detects new nodes. A hard hat was filed as `HighVisVest` (first option) when the AJAX response won the race.

## Non-goals

- Creating store-wide product attributes (the preflight refuses; a human adds)
- Deleting stray attribute values automatically
- Parallelism within a run (one page, sequential rows)
- Using `library.retry` around `save`
