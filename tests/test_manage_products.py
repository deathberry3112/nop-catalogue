#!/usr/bin/env python3
"""Reaching Catalog > Products > Manage Products through the menu.

    pytest -m live tests/test_manage_products.py -s
    NOP_HEADED=1 NOP_SLOWMO=300 pytest -m live tests/test_manage_products.py -s

Navigating by URL would be one line; these walk the menu because the menu is
what a human uses and the only thing that can break independently of the page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import library
from pages.landing_page import LandingPage
from pages.manage_products import ManageProductsPage


@pytest.fixture()
def signed_in(live_page, admin_credentials):
    """The shared page, authenticated. login() is a no-op when the session is
    already good, so this costs one URL check per test after the first."""
    if not admin_credentials.email or not admin_credentials.password:
        pytest.skip("ADMIN_EMAIL / ADMIN_PASSWORD not set")
    LandingPage(live_page, url=admin_credentials.base_url).login(
        admin_credentials.email, admin_credentials.password)
    return live_page


@pytest.mark.live
def test_menu_reaches_manage_products(signed_in):
    """Hover Catalog, hover Products, click Manage Products, and land there."""
    page = signed_in
    products = ManageProductsPage(page)

    # Start honest: on the dashboard, not already on the product list.
    assert ManageProductsPage.URL_PATH.casefold() not in page.url.casefold(), (
        f"test started on the product list already: {page.url}"
    )

    products.open_via_menu().verify()

    assert ManageProductsPage.URL_PATH.casefold() in page.url.casefold()
    assert "manage products" in page.title().casefold()
    assert page.locator(ManageProductsPage.SEL["grid"]).is_visible(), "no product grid"
    assert page.locator(ManageProductsPage.SEL["search_name"]).is_visible(), "no search box"
    assert products.heading() == "Manage Products", (
        f"section header reads {products.heading()!r}"
    )

    print(f"\nlanded on: {page.url}")
    print(f"title:     {page.title()!r}")
    print(f"heading:   {products.heading()!r}")
    print(f"grid rows: {products.product_count()}")


@pytest.mark.live
def test_menu_path_is_reported_when_an_item_is_missing(signed_in):
    """A renamed menu entry must fail saying what it looked for and what was
    there — the failure mode a selector change actually produces."""
    products = ManageProductsPage(signed_in)
    products.MENU_PATH = ("Catalog", "Products", "Manage Widgets")
    try:
        with pytest.raises(library.StepError) as excinfo:
            products.open_via_menu()
    finally:
        del products.MENU_PATH                      # back to the class attribute

    message = str(excinfo.value)
    assert "Manage Widgets" in message
    assert "Manage Products" in message, "the error should list what it did find"
    print(f"\nreported: {message}")
