"""run_batch — the automation proper.

main.resolve_runner imports this. If the import fails it falls back to a stub;
production uses this path.

    run_batch(page, rows, *, base_url, email, password, commit, on_existing,
              progress, store=None) -> summary

CONTRACT (main.py's): take an already-created `page`, log in, loop the rows,
return a summary. Never launch a browser — main.py owns that, so there is
exactly one place that configures Chromium.

Per row, the flow and its guard:

    look the SKU up exactly
      exists, not ours   -> skip, touch nothing        <- the guard
      exists, ours       -> resume, finish configuring it
      new                -> create, then configure

The guard matters more than it looks: several payload SKUs already exist as
products nobody here made, and "add the payload" without it would edit a
client's live catalogue. Ownership comes from the store when Postgres is
configured (shared between concurrent workers) and the local ledger otherwise.

Every write step is idempotent — adding an attribute that exists returns it,
generating combinations that exist returns them — which is what makes both
resume and retry safe.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

import combination_skus
import library
from app.colour import resolve as resolve_colours
from app.colour import swatches_for_row
from app.errors import ErrorClass, classify
from pages.landing_page import LandingPage
from pages.manage_products import ManageProductsPage
from pages.product_page import ProductPage

log = logging.getLogger("automation.runner")


class _NullStore:
    """Stands in when no store was supplied, so the runner never branches on
    whether telemetry exists. Every method is a no-op with the right shape."""

    run_id = None

    def row_started(self, row, index=0): return None
    def row_finished(self, *a, **k): pass
    def event(self, *a, **k): pass
    def record_inference(self, *a, **k): pass
    def claim_product(self, *a, **k): pass
    def owned_skus(self, base_url): return set()

    def step(self, row_id, name, attempt=1):
        from contextlib import nullcontext
        return nullcontext({})


def _retrying(store, row_id: str | None, name: str,
              action: Callable[[], Any]) -> Any:
    """Run one step, retrying only what is worth retrying.

    The taxonomy decides. A transient DOM error — Kendo rebinding the grid
    under an open editor, which is common and expected — is retried up to five
    times. An unresolvable colour is not retried at all: five attempts spend
    five minutes arriving at the identical answer. That distinction is the
    entire reason app.errors exists.
    """
    attempt = 1
    while True:
        try:
            with store.step(row_id, name, attempt=attempt) as detail:
                return action()
        except Exception as exc:
            classified = classify(exc)
            if not classified.retryable or attempt >= classified.max_attempts:
                raise
            log.warning("%s failed (%s), attempt %d/%d: %s", name,
                        classified.error_class.value, attempt,
                        classified.max_attempts, exc)
            store.event(f"retrying {name} after {classified.error_class.value}",
                        level="warn", row_id=row_id,
                        data={"attempt": attempt, "error": str(exc)[:500]})
            time.sleep(0.5 * attempt)
            attempt += 1


def mismatch_message(report: dict, row: dict) -> str:
    """Say what to DO about a combination grid that does not match the payload.

    The two halves of the report mean completely different things, and the old
    message printed both as raw dicts and left the reader to work out which:

      unmatched  the catalogue generated combinations the workbook never
                 described. Almost always ONE stray attribute VALUE left on the
                 product by an earlier run — the grid rows are just its cross
                 product, so naming the value is worth far more than listing
                 them. Nothing in this codebase ever deletes an attribute
                 value, which is why it survives every retry and re-run until a
                 human removes it. That is the sentence people actually need.

      missing    the workbook described combinations nopCommerce did not
                 generate. The attribute set is incomplete, not polluted — the
                 opposite problem, and a different fix.

    The real case: a row whose size attribute column repeated "Color" filed
    "OS" as a ninth colour, and the run afterwards reported
    `unmatched rows: [{'Color': 'OS', 'Size': 'OS'}]` — true, and nearly
    useless.
    """
    known: dict[str, set[str]] = {}
    known.setdefault(row.get("color_attr") or "Color", set()).update(
        c.casefold() for c in row.get("colors") or [])
    known.setdefault(row.get("size_attr") or "Size", set()).update(
        s.casefold() for s in row.get("sizes") or [])

    unmatched = report.get("unmatched") or []
    missing = list(report.get("missing") or [])
    parts: list[str] = []

    if unmatched:
        stray: dict[str, set[str]] = {}
        for combination in unmatched:
            for attribute, value in (combination or {}).items():
                if str(value).casefold() in known.get(attribute, set()):
                    continue
                stray.setdefault(attribute, set()).add(str(value))
        if stray:
            named = "; ".join(f"{attribute} = "
                              + ", ".join(repr(v) for v in sorted(values))
                              for attribute, values in sorted(stray.items()))
            parts.append(
                f"this product carries attribute value(s) the workbook does not "
                f"list — {named}. That is usually left over from an earlier run, "
                f"and nothing here deletes an attribute value, so it will "
                f"survive re-running. Remove it on the product's Product "
                f"attributes tab, delete the product's existing combinations, "
                f"then run this row again")
        else:
            parts.append(
                f"{len(unmatched)} generated combination(s) match nothing in the "
                f"workbook: {unmatched}")

    if missing:
        parts.append(
            f"{len(missing)} workbook entry(ies) had no combination generated "
            f"for them: {missing}. The product's attribute values are missing "
            f"something the workbook expects")

    return "combinations do not line up — " + ". Also, ".join(parts)


def _resolve_swatches(product, colour_id: str, row: dict) -> tuple[dict, list[str]]:
    """({colour: swatch id}, ["Colour — why not"]) for this row's colours.

    Driven by the sheet's Color Swatch column; see app.colour.swatch. A row that
    leaves that column blank gets no swatches and says nothing about it.

    Never raises, and that is the policy this encodes: a swatch is decoration on
    a product that is otherwise correct, so failing to find one must not fail the
    row. What could not be settled comes back named, with the reason, so the run
    reports it rather than leaving somebody to notice blanks on the storefront.
    """
    if not row.get("swatches"):
        return {}, []
    options = product.swatch_options(colour_id)
    if not options:
        return {}, []
    return swatches_for_row(row, options)


def _configure_attributes(page, products, product, row, store, row_id) -> dict:
    """Colours, sizes, combinations and their SKUs for one row.

    Each stage re-opens the product first. The attribute value editor and the
    combination generator both navigate away from the edit page, so carrying on
    from wherever the last stage finished means clicking a tab that is no longer
    on screen.
    """
    tiers: dict[str, int] = {}
    if not row.get("colors"):
        return tiers

    colour_id = _retrying(store, row_id, "attribute_colour",
                          lambda: product.add_attribute(row["color_attr"]))

    # Which swatch each colour gets. Resolved against the dropdown the admin is
    # actually offering, so nothing here can name a swatch that does not exist.
    # A colour that cannot be resolved confidently is reported and left unset —
    # never guessed. A missing swatch is cosmetic; the wrong one is a lie about
    # the product, and it would be invisible until a customer saw it.
    swatches, unresolved = _resolve_swatches(product, colour_id, row)
    if unresolved:
        log.warning("%s: no swatch for %s", row["sku"], "; ".join(unresolved))
        store.event("colours left without a swatch", level="warn", row_id=row_id,
                    data={"unresolved": unresolved})

    _retrying(store, row_id, "values_colour",
              lambda: product.add_attribute_values(colour_id, row["colors"],
                                                   swatches=swatches))

    if row.get("sizes"):
        page.goto(products.find_by_sku(row["sku"]))
        size_id = _retrying(store, row_id, "attribute_size",
                            lambda: product.add_attribute(row["size_attr"]))
        _retrying(store, row_id, "values_size",
                  lambda: product.add_attribute_values(size_id, row["sizes"]))

    if not row.get("generate_combinations"):
        return tiers

    # Resolve the colour codes BEFORE generating anything. If the colours
    # cannot be matched to the payload's child SKUs there is no point creating
    # combinations we would then be unable to name — and the failure is the
    # client's data, not the browser's, so it should cost one second, not one
    # page load.
    resolution = resolve_colours(row)
    store.record_inference(resolution, row_id)
    tiers = resolution.tier_counts
    if resolution.used_llm:
        store.event("colour codes resolved by model", row_id=row_id,
                    data={"tiers": tiers, "codes": resolution.codes})
    if not resolution.ok:
        raise combination_skus.CombinationError(
            resolution.error or "colours could not be resolved")

    wanted = combination_skus.combination_map(row, resolution.codes)

    page.goto(products.find_by_sku(row["sku"]))
    _retrying(store, row_id, "generate_combinations",
              lambda: product.generate_combinations())

    def apply() -> dict:
        report = product.apply_combination_skus(
            wanted,
            colour_attribute=row["color_attr"],
            size_attribute=row["size_attr"],
            allow_out_of_stock=bool(row.get("allow_out_of_stock")),
        )
        if report["unmatched"] or report["missing"]:
            raise library.StepError(mismatch_message(report, row),
                                    step="combinations")
        return report

    _retrying(store, row_id, "combination_skus", apply)
    return tiers


def _preview_row(page, products, row, store, row_id, existing: str | None) -> dict:
    """A dry run of one row: everything except the irreversible click.

    This is a real rehearsal, not a different code path with the writes removed.
    It catches, in order of how often they actually bite:

      * colours that cannot be matched to the payload's child SKUs — the most
        common data problem, and it costs one second here instead of one page
        load and a half-configured product
      * a SKU that already belongs to somebody else's product
      * a create form that rejects the name or SKU

    What it CANNOT check is anything requiring a saved product: nopCommerce
    will not show the attribute tabs until the product exists. So a preview
    verifies the data and the form, and stops. Saying so plainly matters more
    than implying a completeness we cannot deliver.
    """
    tiers: dict[str, int] = {}

    if row.get("colors") and row.get("generate_combinations"):
        with store.step(row_id, "resolve_colours"):
            resolution = resolve_colours(row)
            store.record_inference(resolution, row_id)
            tiers = resolution.tier_counts
            if not resolution.ok:
                raise combination_skus.CombinationError(
                    resolution.error or "colours could not be resolved")

    if existing:
        store.event(f"{row['sku']} already exists — would be skipped or resumed",
                    row_id=row_id, data={"url": existing})
        return tiers

    with store.step(row_id, "fill_form"):
        product = products.add_new()
        product.fill_basics(row)
        problems = library.validation_errors(page)
        if problems:
            raise library.StepError("the form rejected this row: "
                                    + "; ".join(problems), step="fill_form")
    store.event(f"{row['sku']} would be created", row_id=row_id)
    return tiers


def run_batch(page, rows, *, base_url: str, email: str, password: str,
              commit: bool, on_existing: str,
              progress, store=None) -> dict:
    """Drive `rows` through the admin and report what happened."""
    store = store or _NullStore()
    started = time.monotonic()

    LandingPage(page, url=base_url).login(email, password)
    store.event("signed in", data={"base_url": base_url})

    products = ManageProductsPage(page)
    owned = store.owned_skus(base_url) | _local_ledger_skus()
    results: list[dict] = []
    created = 0

    for index, row in enumerate(rows, 1):
        sku = row["sku"]
        progress(index, len(rows), sku)
        row_id = store.row_started(row, index)
        row_started = time.monotonic()
        tiers: dict[str, int] = {}
        # Set only on the CREATE path, so it means exactly what the rollback
        # means by ownership. A row that dies after this is set left a real,
        # half-configured product behind — recording it here is what lets the
        # run screen say so instead of reporting a bare 'failed'.
        made: tuple[str, str | None] | None = None

        try:
            existing = _retrying(store, row_id, "lookup",
                                 lambda: products.find_by_sku(sku))

            if existing and sku not in owned:
                # Somebody else's product. Open nothing, change nothing.
                if on_existing == "fail":
                    raise library.StepError(
                        f"{sku} already exists at {existing} and was not created "
                        f"by this automation", step="lookup")
                store.row_finished(row_id, status="skipped", product_url=existing,
                                   error="already in the catalogue and not created "
                                         "by this automation — left untouched")
                results.append({"sku": sku, "error": None, "status": "Skipped",
                                "url": existing})
                continue

            if not commit:
                # Dry run. Everything that can be checked without writing gets
                # checked; the one irreversible click is what we stop short of.
                tiers = _preview_row(page, products, row, store, row_id, existing)
                store.row_finished(row_id, status="previewed",
                                   product_url=existing, colour_tiers=tiers)
                results.append({"sku": sku, "error": None, "status": "Pending",
                                "url": existing})
                continue

            if existing:
                page.goto(existing)
                product = ProductPage(page)
                outcome = "resumed"
            else:
                product = _retrying(store, row_id, "create",
                                    lambda: products.add_new())

                def save() -> None:
                    product.fill_basics(row).save()
                    if not product.is_saved():
                        raise library.StepError(
                            f"saved but no product id: {page.url}", step="save")

                _retrying(store, row_id, "save", save)
                made = (page.url, product.product_id)
                store.claim_product(base_url, sku, page.url, product.product_id)
                _record_local(sku, page.url)
                owned.add(sku)
                created += 1
                outcome = "added"

            if row.get("spec_value"):
                _retrying(store, row_id, "specification",
                          lambda: product.add_specification(row["spec_value"]))

            tiers = _configure_attributes(page, products, product, row, store, row_id)

            url = products.find_by_sku(sku) or page.url
            store.row_finished(row_id, status=outcome, product_url=url,
                               product_id=product.product_id, colour_tiers=tiers)
            results.append({"sku": sku, "error": None, "url": url,
                            "status": "Added" if outcome == "added" else "Added"})

        except Exception as exc:
            classified = classify(exc)
            log.error("%s failed (%s): %s", sku, classified.error_class.value, exc)
            # `made` carries the product this row created before it died, so a
            # failed row points at the orphan it left rather than at nothing.
            store.row_finished(row_id, status="failed", error=str(exc),
                               error_class=classified.error_class.value,
                               product_url=made[0] if made else None,
                               product_id=made[1] if made else None,
                               colour_tiers=tiers)
            results.append({"sku": sku, "error": str(exc),
                            "error_class": classified.error_class.value})
        finally:
            log.info("%s took %.1fs", sku, time.monotonic() - row_started)

    failed = sum(1 for r in results if r.get("error"))
    return {
        "succeeded": len(results) - failed,
        "failed": failed,
        "created": created,
        "duration_seconds": round(time.monotonic() - started, 1),
        "results": results,
    }


# ---------------------------------------------------------------------------
# The local ledger. Superseded by created_products in Postgres, kept as the
# fallback for a run with no store — but NOT safe for concurrent workers: it is
# an unlocked read-modify-write, so two processes lose each other's entries.
# Configure DATABASE_URL (and apply schema) before running anything concurrently.
# ---------------------------------------------------------------------------

_LEDGER = Path("var/created-products.json")


def _local_ledger_skus() -> set[str]:
    import json
    if not _LEDGER.exists():
        return set()
    try:
        return set(json.loads(_LEDGER.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _record_local(sku: str, url: str) -> None:
    import json
    try:
        data = {}
        if _LEDGER.exists():
            data = json.loads(_LEDGER.read_text(encoding="utf-8"))
        data[sku] = url
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        log.warning("could not update the local ledger: %s", exc)
