#!/usr/bin/env python3
"""Delete the products this automation created, so adding can be tested again.

    pytest -m "live and cleanup" tests/test_delete_products.py -s

DESTRUCTIVE, and deliberately awkward to trigger: it is marked `cleanup` and
additionally refuses to run unless "cleanup" is in the -m expression, so a
plain `pytest -m live` can never delete anything.

It deletes exactly what is in var/created-products.json — the ledger the add
test writes — and re-checks the SKU on the edit page before every delete.
Several payload SKUs already exist as products this automation did not create
(CP-30-C100V is product 388, complete with attributes and combinations);
deleting "the payload SKUs" would destroy them, so the payload is never the
source of truth here. The ledger is.
"""

from __future__ import annotations

import pytest

from app.store import RunStore
from app.store.client import store_available
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


@pytest.fixture()
def cleanup_requested(request):
    if "cleanup" not in (request.config.getoption("-m") or ""):
        pytest.skip("destructive — ask for it by name: pytest -m 'live and cleanup'")


@pytest.fixture()
def release_claim(admin_credentials):
    """Retire the **Postgres** ownership claim as well as the JSON ledger entry.

    Deleting a product without this leaves `created_products` saying the
    automation still owns a SKU whose product is gone — and `owned_skus()`
    filters on `deleted_at`, so that stale claim makes a later run treat the
    SKU as ours. When somebody re-creates it, the run resumes and EDITS a
    product this automation never made. That is not hypothetical: an audit on
    2026-09-11 found 31 of 35 ownership records stale, 17 of them pointing at
    SKUs that existed as somebody else's product.

    `var/created-products.json` is this test's own ledger and the thing it
    iterates; Postgres is the one production reads. Both have to be crossed
    out, and only the HTTP rollback (`app/rollback.py` `_release`) was doing it.
    """
    store = RunStore("live-cleanup", remote=store_available())

    def release(sku: str) -> None:
        # _safe() swallows a missing or unreachable store: a cleanup run
        # without Postgres configured must still delete and still pass.
        store.release_product(admin_credentials.base_url, sku)

    return release


@pytest.mark.live
@pytest.mark.cleanup
def test_delete_products_this_automation_created(cleanup_requested, signed_in,
                                                created_ledger, release_claim):
    """Delete every ledger entry, verify it is gone, then drop it from the ledger."""
    ledger = created_ledger.all()
    if not ledger:
        pytest.skip(f"ledger is empty ({created_ledger.path}) — nothing was created")

    page = signed_in
    products = ManageProductsPage(page)
    deleted, already_gone, failed = [], [], []

    print(f"\ndeleting {len(ledger)} product(s) from {created_ledger.path}")
    for sku in list(ledger):
        url = products.find_by_sku(sku)
        if not url:
            already_gone.append(sku)
            created_ledger.forget(sku)
            release_claim(sku)          # the claim outlives the product otherwise
            continue

        try:
            page.goto(url)
            product = ProductPage(page)

            # Last line of defence. The ledger stores a URL, and product ids get
            # reused after a delete; confirm this page really is this SKU before
            # touching the Delete button.
            on_page = page.locator(ProductPage.SEL["sku"]).input_value()
            assert on_page == sku, f"ledger says {sku}, edit page says {on_page} — refusing"

            product.delete()
            assert products.find_by_sku(sku) is None, f"{sku} is still in the catalogue"
            created_ledger.forget(sku)
            release_claim(sku)          # part of the delete, not a tidy-up
            deleted.append(sku)
        except Exception as exc:
            failed.append((sku, str(exc)))

    print("\n--- delete summary ---")
    for sku in deleted:
        print(f"  deleted     {sku}")
    for sku in already_gone:
        print(f"  gone already {sku}")
    for sku, why in failed:
        print(f"  FAILED      {sku}: {why}")
    print(f"ledger now: {list(created_ledger.all())}")

    assert not failed, f"{len(failed)} delete(s) failed: {failed}"


@pytest.mark.live
@pytest.mark.cleanup
def test_ledger_is_empty_afterwards(cleanup_requested, created_ledger):
    """Whatever was deleted must have left the ledger, or the next cleanup will
    go looking for products that no longer exist."""
    assert created_ledger.all() == {}, (
        f"still recorded as created: {list(created_ledger.all())}"
    )
