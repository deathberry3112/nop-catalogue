#!/usr/bin/env python3
"""Adding the payload's products through the admin, with the existing-SKU guard.

    pytest -m live tests/test_add_products.py -s
    NOP_HEADED=1 NOP_SLOWMO=200 pytest -m live tests/test_add_products.py -s

These tests WRITE to the catalogue. Every product they create is written to
var/created-products.json, and tests/test_delete_products.py deletes exactly
that set and nothing else — several payload SKUs already exist as products
nobody here made, and they must survive a cleanup.

The rule for each row: look the SKU up exactly; if it is already a product,
open its edit page and add nothing; only otherwise create it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import combination_skus
import library
import xlsxCreator
from pages.landing_page import LandingPage
from pages.manage_products import ManageProductsPage
from pages.product_page import ProductPage


@pytest.fixture()
def signed_in(live_page, admin_credentials):
    if not admin_credentials.email or not admin_credentials.password:
        pytest.skip("ADMIN_EMAIL / ADMIN_PASSWORD not set")
    LandingPage(live_page, url=admin_credentials.base_url).login(
        admin_credentials.email, admin_credentials.password)
    return live_page


def write_run_report(rows, created, resumed, skipped, failed, path):
    """Turn this run's outcome into the .xlsx the user gets back.

    xlsxCreator wants a run summary shaped like main.run()'s, keyed by SKU. Its
    `error` field doubles as the Note column — that is the contract, not a
    misuse: the module documents Note as "the error message for a failed row, or
    why it was skipped".

    A row absent from `results` comes out Pending, which is what should happen
    to rows a halted run never reached.
    """
    results = []
    results += [{"sku": sku} for sku, _ in created]
    results += [{"sku": sku, "status": xlsxCreator.STATUS_ADDED,
                 "error": "created by an earlier run; configuration completed"}
                for sku, _ in resumed]
    results += [{"sku": sku, "status": xlsxCreator.STATUS_SKIPPED,
                 "error": f"already in the catalogue ({url}) and not created by "
                          "this automation — left untouched"}
                for sku, url in skipped]
    results += [{"sku": sku, "error": why} for sku, why in failed]

    return xlsxCreator.write_report(rows, {"results": results}, path)


def configure_attributes(page, products, product, row):
    """Colours, sizes, combinations and their SKUs for one row.

    Each step re-opens the product first. The attribute value editor and the
    combination generator both navigate away from the edit page, so carrying on
    from wherever the last step finished is how you end up clicking a tab that
    is no longer on screen.
    """
    if not row.get("colors"):
        return

    colour_id = product.add_attribute(row["color_attr"])
    product.add_attribute_values(colour_id, row["colors"])

    if row.get("sizes"):
        page.goto(products.find_by_sku(row["sku"]))
        size_id = product.add_attribute(row["size_attr"])
        product.add_attribute_values(size_id, row["sizes"])

    if not row.get("generate_combinations"):
        return

    page.goto(products.find_by_sku(row["sku"]))
    product.generate_combinations()

    report = product.apply_combination_skus(
        combination_skus.combination_map(row),
        colour_attribute=row["color_attr"],
        size_attribute=row["size_attr"],
        allow_out_of_stock=bool(row.get("allow_out_of_stock")),
    )
    if report["unmatched"] or report["missing"]:
        raise library.StepError(
            f"combinations do not line up — unmatched rows: {report['unmatched']}, "
            f"payload entries with no row: {list(report['missing'])}",
            step="combinations")


@pytest.mark.live
def test_existing_sku_opens_the_edit_page_instead_of_adding(signed_in, rows):
    """The guardrail itself: a SKU already in the catalogue must resolve to its
    edit page, so the caller can see it exists rather than creating a second."""
    products = ManageProductsPage(signed_in)

    existing = [(row["sku"], products.find_by_sku(row["sku"])) for row in rows]
    found = [(sku, url) for sku, url in existing if url]
    if not found:
        pytest.skip("no payload SKU currently exists — nothing to guard against")

    sku, url = found[0]
    assert "/Product/Edit/" in url, f"{sku} resolved to {url}"
    signed_in.goto(url)
    assert signed_in.locator(ProductPage.SEL["sku"]).input_value() == sku, (
        f"{url} is not the edit page for {sku}"
    )
    print(f"\nalready in the catalogue, will not be re-added:")
    for sku, url in found:
        print(f"  {sku:14} -> {url}")


@pytest.mark.live
def test_sku_lookup_is_exact_while_the_name_search_is_not(signed_in, rows):
    """The guardrail must not report a product that isn't there.

    The name search matches substrings, so a prefix of a real SKU comes back
    with rows. If the guardrail were built on that, an add would be skipped for
    a product that does not exist and nobody would know why. The SKU box is an
    exact lookup, and this pins that difference.

    The prefix is derived from a payload SKU rather than hardcoded: an earlier
    version of this test asserted "CP-30-C100" did not exist, which stopped
    being true the moment the add test created it.
    """
    products = ManageProductsPage(signed_in)
    real = rows[0]["sku"]
    if not products.find_by_sku(real):
        pytest.skip(f"{real} is not in the catalogue on this run")

    prefix = real[:4]                               # "CP-3" — a strict prefix
    assert products.search_by_name(prefix) > 0, (
        f"expected the name search to match {prefix!r} as a substring"
    )
    assert products.find_by_sku(prefix) is None, (
        f"the SKU lookup matched the prefix {prefix!r} — it is not an exact match"
    )


@pytest.mark.live
def test_add_products_from_payload(signed_in, rows, created_ledger,
                                   report_path):
    """Create every payload product that isn't already there."""
    page = signed_in
    products = ManageProductsPage(page)
    created, resumed, skipped, failed = [], [], [], []

    for row in rows:
        sku = row["sku"]
        existing = products.find_by_sku(sku)

        # Somebody else's product: open it, change nothing. This is the guard.
        if existing and sku not in created_ledger:
            skipped.append((sku, existing))
            continue

        try:
            if existing:
                # Ours, from an earlier run. Pick up where that run stopped —
                # otherwise a product created but not fully configured is
                # "existing" forever and never gets finished. Every step below
                # is a no-op when it has already been done.
                page.goto(existing)
                product = ProductPage(page)
                resumed.append((sku, existing))
            else:
                product = products.add_new()
                product.fill_basics(row).save()
                if not product.is_saved():
                    raise library.StepError(f"saved but no product id: {page.url}",
                                            step="save")
                created_ledger.record(sku, page.url)   # record before anything else
                created.append((sku, page.url))

            if row.get("spec_value"):
                product.add_specification(row["spec_value"])
            configure_attributes(page, products, product, row)
        except Exception as exc:
            failed.append((sku, str(exc)))

    print("\n--- add summary ---")
    for sku, url in created:
        print(f"  created  {sku:14} {url}")
    for sku, url in resumed:
        print(f"  resumed  {sku:14} {url}")
    for sku, url in skipped:
        print(f"  existed  {sku:14} {url}  (not ours — untouched)")
    for sku, why in failed:
        print(f"  FAILED   {sku:14} {why}")
    print(f"\nledger ({created_ledger.path}): {list(created_ledger.all())}")

    # Write the report before asserting. A run that fails halfway is exactly
    # when you want to know which rows made it — asserting first would leave
    # the one useful output unwritten.
    counts = write_run_report(rows, created, resumed, skipped, failed, report_path)
    print(f"report: {report_path}")
    print(f"  {', '.join(f'{k}: {v}' for k, v in sorted(counts.items())) or 'no rows'}")

    assert not failed, f"{len(failed)} row(s) failed: {failed}"
    assert len(created) + len(resumed) + len(skipped) == len(rows)


@pytest.mark.live
def test_created_products_are_findable_and_correct(signed_in, created_ledger, rows):
    """Everything the ledger claims was created is really there, with the right
    SKU, name and specification value."""
    if not created_ledger.all():
        pytest.skip("nothing has been created yet")

    page = signed_in
    by_sku = {row["sku"]: row for row in rows}
    products = ManageProductsPage(page)

    # The ledger can hold products from a run over a wider payload than this one
    # (NOP_ROW_LIMIT). Verify what this run's rows cover and leave the rest be.
    for sku in [s for s in created_ledger.all() if s in by_sku]:
        url = products.find_by_sku(sku)
        assert url, f"{sku} is in the ledger but not in the catalogue"

        page.goto(url)
        product = ProductPage(page)
        assert page.locator(ProductPage.SEL["sku"]).input_value() == sku
        assert page.locator(ProductPage.SEL["name"]).input_value() == by_sku[sku]["product_name"]

        row = by_sku[sku]
        expected_spec = row.get("spec_value")
        if expected_spec:
            product.open_tab("Specification attributes")
            assert product.has_specification(expected_spec), (
                f"{sku}: specifications are {product.specifications()}, "
                f"expected {expected_spec!r}"
            )
            # Exactly one, not merely "the right one is in there". A mis-selected
            # option adds a second row rather than replacing the first, so a
            # presence check would pass on a product filed under two categories.
            assert product.specifications() == [expected_spec], (
                f"{sku}: expected only {expected_spec!r}, found "
                f"{product.specifications()}"
            )

        if not row.get("colors"):
            print(f"  verified {sku:14} {url}")
            continue

        product.open_tab("Product attributes")
        mappings = product.attribute_mappings()
        assert row["color_attr"] in mappings, (
            f"{sku}: no {row['color_attr']!r} attribute (has {list(mappings)})")

        values = product.attribute_values(mappings[row["color_attr"]])
        assert set(values) == set(row["colors"]), (
            f"{sku}: colours are {sorted(values)}, expected {sorted(row['colors'])}")

        if row.get("sizes"):
            page.goto(url)
            product.open_tab("Product attributes")
            mappings = product.attribute_mappings()
            assert row["size_attr"] in mappings, f"{sku}: no {row['size_attr']!r} attribute"
            sizes = product.attribute_values(mappings[row["size_attr"]])
            assert set(sizes) == set(row["sizes"]), (
                f"{sku}: sizes are {sorted(sizes)}, expected {sorted(row['sizes'])}")

        if row.get("generate_combinations"):
            page.goto(url)
            product.open_tab("Product attributes")
            product.open_tab("Attribute combinations")
            wanted = combination_skus.combination_map(row)
            combos = product.combinations()
            actual = {
                (c["attributes"].get(row["color_attr"]),
                 c["attributes"].get(row["size_attr"]) or None): c["sku"]
                for c in combos
            }
            assert actual == wanted, (
                f"{sku}: combination SKUs are wrong.\n  got:      {actual}\n"
                f"  expected: {wanted}")

            expected_stock = bool(row.get("allow_out_of_stock"))
            wrong = [c["attributes"] for c in combos
                     if c["allow_out_of_stock"] != expected_stock]
            assert not wrong, (
                f"{sku}: allow-out-of-stock should be {expected_stock} on every "
                f"combination; wrong on {wrong}")
            print(f"  verified {sku:14} {len(wanted):2} combos  {url}")
        else:
            print(f"  verified {sku:14} {url}")
