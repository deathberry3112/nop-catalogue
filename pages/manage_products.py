#!/usr/bin/env python3
"""Page object for Catalog > Products > Manage Products (/Admin/Product/List).

    from pages.manage_products import ManageProductsPage

    products = ManageProductsPage(page).open_via_menu().verify()

open_via_menu() walks the real menu — hover Catalog, hover Products, click
Manage Products — rather than navigating straight to the URL. Slower and more
fragile than a goto, and that is the point: it is the only thing that proves the
menu a human uses still works. ManageProductsPage(page).open() is there for
when you just want to be on the page.

The menu helpers below are generic (any Catalog > X > Y path), and live here
only because this is the first page that needs them. Move them to a shared
AdminMenu object the moment a second page object wants the same walk.

Verified against this instance: nopCommerce 3.x, Kendo horizontal menu.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import library

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

log = logging.getLogger("pages.manage_products")


class ManageProductsPage:
    """The product list, and the menu path that reaches it."""

    URL_PATH = "/Admin/Product/List"                # verified
    MENU_PATH = ("Catalog", "Products", "Manage Products")

    SEL = {
        "menu":        "#admin-menu",               # verified: Kendo menu bar
        "grid":        "#products-grid",            # verified
        "search_name": "#SearchProductName",        # verified
        "search":      "#search-products",          # verified
        "header":      ".section-header",           # verified: "Manage Products ..."
        "sku_box":     "#GoDirectlyToSku",          # verified
        "sku_go":      "#go-to-product-by-sku",     # verified
        "add_new":     "a[href$='/Product/Create']",    # verified: the "Add new" button
    }

    def __init__(self, page: "Page", *, timeout: int = library.TIMEOUT):
        self.page = page
        self.timeout = timeout

    # --- getting here ----------------------------------------------------

    def open(self) -> "ManageProductsPage":
        """Straight to the URL. No menu involved."""
        library.goto(self.page, self.URL_PATH)
        return self

    def open_via_menu(self) -> "ManageProductsPage":
        """Hover Catalog, hover Products, click Manage Products.

        Requires an authenticated session — the menu does not exist on the
        login page. Each hover waits for the submenu it opens, so a menu that
        stops responding fails here with the label that didn't open rather than
        as a mystery click on a hidden element.
        """
        catalog, products, manage = self.MENU_PATH

        menu = self.page.locator(self.SEL["menu"])
        try:
            menu.wait_for(state="visible", timeout=self.timeout)
        except Exception:
            raise library.StepError(
                f"no admin menu at {self.page.url} — not signed in?", step="menu") from None

        node = self._pick(menu.locator("> li"), catalog, where="menu bar")
        self._open_submenu(node, catalog)

        node = self._pick(self._children(node), products, where=catalog)
        self._open_submenu(node, products)

        leaf = self._pick(self._children(node), manage, where=f"{catalog} > {products}")
        leaf.locator("a").first.click(timeout=self.timeout)

        self.page.wait_for_load_state("load")
        library.wait_ready(self.page, timeout=self.timeout)
        log.info("menu: %s", " > ".join(self.MENU_PATH))
        return self

    # --- finding and creating products -----------------------------------

    def find_by_sku(self, sku: str) -> str | None:
        """The edit URL for `sku`, or None if no product has that SKU.

        This is the guardrail before every add, and it uses the "Go directly to
        product SKU" box rather than the product-name search on purpose. The
        name search matches substrings: searching "CP-30-C100" returns the row
        for "CP-30-C100V", so a name-based existence check reports a product
        that isn't there and skips an add that should have happened. The SKU box
        is an exact lookup — it either redirects to the edit page or leaves you
        on the list.
        """
        self.open()
        library.fill(self.page, self.SEL["sku_box"], sku, timeout=self.timeout)
        library.click(self.page, self.SEL["sku_go"], timeout=self.timeout, settle=False)
        self.page.wait_for_load_state("load")
        library.wait_ready(self.page, timeout=self.timeout)

        if "/Product/Edit/" in self.page.url:
            return self.page.url
        if self.URL_PATH.casefold() in self.page.url.casefold():
            return None
        raise library.StepError(
            f"SKU lookup for {sku!r} landed somewhere unexpected: {self.page.url}",
            step="find_by_sku")

    def search_by_name(self, text: str) -> int:
        """Run the Product name search and return how many rows came back.

        Substring matching, which is why find_by_sku() does not use this.
        """
        self.open()
        library.fill(self.page, self.SEL["search_name"], text, timeout=self.timeout)
        library.click(self.page, self.SEL["search"], timeout=self.timeout)
        self.page.wait_for_timeout(600)             # the grid redraws over AJAX
        library.wait_ready(self.page, timeout=self.timeout)
        return self.product_count()

    def add_new(self):
        """Click Add new and return the empty product form.

        Imported here rather than at module scope: ProductPage's own navigation
        goes back through this class, and a top-level import would make the two
        modules import each other.
        """
        from pages.product_page import ProductPage

        self.open()
        library.click(self.page, self.SEL["add_new"], timeout=self.timeout)
        self.page.wait_for_load_state("load")
        library.wait_ready(self.page, timeout=self.timeout)

        product = ProductPage(self.page, timeout=self.timeout)
        if product.CREATE_PATH.casefold() not in self.page.url.casefold():
            raise library.StepError(
                f"Add new did not open the create form: {self.page.url}", step="add_new")
        return product

    # --- Kendo menu internals --------------------------------------------

    def _children(self, item: "Locator") -> "Locator":
        """The direct child items of a menu node.

        Not `> ul > li`: two things break that. Kendo wraps a submenu's <ul> in
        a .k-animation-container div as it opens, so the ul stops being a direct
        child mid-interaction; and a plain `ul li` descendant search returns the
        whole subtree — under Catalog that is 14 items, including "Manage
        products" three levels down, which is exactly the item a naive
        has_text="Products" match would grab instead of "Products".
        """
        return item.locator("ul.k-group").first.locator("> li")

    def _label(self, item: "Locator") -> str:
        """An item's own label, ignoring its descendants' text. Parent items are
        a <span class=k-link>, leaves are an <a class=k-link>."""
        link = item.locator(":scope > a.k-link, :scope > span.k-link").first
        try:
            return (link.inner_text() or "").strip()
        except Exception:
            return ""

    def _pick(self, items: "Locator", label: str, *, where: str) -> "Locator":
        """The item labelled `label`, matched whole and case-insensitively.

        The admin spells the same entry "Manage products" in one submenu and
        "Manage Products" in another, so case cannot be trusted. Exact-match
        rather than substring: "Products" is a prefix of three of its own
        siblings.
        """
        found = []
        for item in items.all():
            text = self._label(item)
            found.append(text)
            if text.casefold() == label.casefold():
                return item
        raise library.StepError(
            f"no menu item {label!r} under {where}; found: {found}", step="menu")

    def _open_submenu(self, item: "Locator", label: str) -> None:
        item.hover(timeout=self.timeout)
        try:
            item.locator("ul.k-group").first.wait_for(state="visible", timeout=self.timeout)
        except Exception:
            raise library.StepError(
                f"hovering {label!r} did not open its submenu", step="menu") from None

    # --- am I here? -------------------------------------------------------

    def verify(self) -> "ManageProductsPage":
        """Assert we are on the product list, or raise saying exactly why not.

        Checks the URL, the page title and the grid together. The URL alone is
        not enough — the admin answers an unauthenticated request to any admin
        path with the login page at that same URL, so a session that quietly
        expired would otherwise "pass".
        """
        problems = self._problems()
        if problems:
            raise library.StepError(
                "not on Manage Products: " + "; ".join(problems), step="verify")
        return self

    def _problems(self) -> list[str]:
        out = []
        if self.URL_PATH.casefold() not in self.page.url.casefold():
            out.append(f"url is {self.page.url}")
        title = self.page.title()
        if "manage products" not in title.casefold():
            out.append(f"title is {title!r}")
        if not library.exists(self.page, self.SEL["grid"]):
            out.append(f"no product grid ({self.SEL['grid']})")
        return out

    def heading(self) -> str:
        """The section header's first line — "Manage Products"."""
        text = library.text_of(self.page, self.SEL["header"], timeout=self.timeout)
        return text.splitlines()[0].strip() if text else ""

    def product_count(self) -> int:
        """Rows currently rendered in the grid. One page's worth, not the total."""
        return self.page.locator(f"{self.SEL['grid']} tbody tr").count()
