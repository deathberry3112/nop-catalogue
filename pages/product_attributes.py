#!/usr/bin/env python3
"""Page object for Catalog > Attributes > Product attributes.

    from pages.product_attributes import ProductAttributesPage

    known = ProductAttributesPage(page).names()      # {"Color", "Size", ...}

THE STORE-WIDE LIST, not a product's. A product's "Product attributes" tab maps
it to one of these names; this page is where the names themselves are defined.
ProductPage.add_attribute picks from a dropdown filled by exactly this list, so
reading it is how the runner can tell in advance that a sheet names something
the admin cannot offer.

READ-ONLY, DELIBERATELY. There is no create() here and there should not be one
without somebody asking for it: this list is store-wide, so a row added from a
mistyped workbook cell is visible against every product in the catalogue and
nothing in a run would flag it. See app/automation/preflight.

ALL OR NOTHING. names() returns the complete list or raises. A partial read is
the one dangerous outcome — the caller's whole purpose is to say "the store does
not have this", and half a list turns attributes that exist into attributes that
do not. So both failure modes (an unreadable grid, a pager we could not follow
to the end) raise, and the runner treats a raise as "cannot check", falling back
to the late failure this exists to avoid rather than to a wrong refusal.

Verified against this instance: nopCommerce 3.x, Kendo grid — EXCEPT the
selectors marked unverified below, which follow the conventions of the grids in
product_page.py but have not been driven against the live admin.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import library

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger("pages.product_attributes")


class ProductAttributesPage:
    """The catalogue's product attribute definitions."""

    URL_PATH = "/Admin/ProductAttribute/List"
    MENU_PATH = ("Catalog", "Attributes", "Product attributes")

    SEL = {
        # unverified: the id follows the admin's convention (#products-grid,
        # #productattributes-grid). If it is wrong, names() raises and the run
        # carries on without the check — see the module docstring.
        "grid": "#productattributes-grid",
        "next_page": "a.k-pager-nav[title=\"Go to the next page\"], "
                     "a.k-link[title=\"Go to the next page\"]",
        "pager_info": ".k-pager-info",
    }

    #: A stop, not a limit. 40 pages is ~600 attributes at the admin's default
    #: page size; a run that reaches it is a pager we are failing to advance,
    #: and looping on it forever would hang the whole batch.
    MAX_PAGES = 40

    def __init__(self, page: "Page", *, timeout: int = library.TIMEOUT):
        self.page = page
        self.timeout = timeout

    def open(self) -> "ProductAttributesPage":
        library.goto(self.page, self.URL_PATH)
        return self

    def names(self) -> set[str]:
        """Every product attribute name this store defines.

        Raises rather than returning a short list — see the module docstring.
        """
        self.open()

        found: list[str] = []
        pages = 0
        while pages < self.MAX_PAGES:
            pages += 1
            found.extend(self._names_on_this_page())
            if not self._go_to_next_page():
                break
        else:
            raise library.StepError(
                f"stopped after {self.MAX_PAGES} pages of the product attribute "
                f"list — the pager is not advancing", step="attributes")

        if not found:
            raise library.StepError(
                f"no product attributes read from {self.URL_PATH} "
                f"(grid {self.SEL['grid']!r}) — a store this automation can map "
                f"attributes in has at least one", step="attributes")

        # The pager's own count, against what we actually collected. This is the
        # guard that makes a wrong pager selector harmless: reading page one of
        # three and reporting the other two pages' attributes as missing would
        # refuse rows that are perfectly fine.
        total = self._total()
        if total is not None and len(found) < total:
            raise library.StepError(
                f"read {len(found)} of the {total} product attributes this store "
                f"reports — refusing to judge a sheet against a partial list",
                step="attributes")

        log.info("store defines %d product attributes over %d page(s)",
                 len(found), pages)
        return set(found)

    # --- grid internals ---------------------------------------------------

    def _rows(self):
        """The grid's rows, once the AJAX that fills it has had a chance.

        Same trade as ProductPage._settled_rows: an immediate read of a Kendo
        grid sees nothing, and the wait is only paid while it is empty.
        """
        rows = self.page.locator(f"{self.SEL['grid']} tbody tr")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and rows.count() == 0:
            self.page.wait_for_timeout(200)
        return rows

    def _names_on_this_page(self) -> list[str]:
        """The Name column of every row currently rendered.

        Kendo renders "no records" as a row of its own, which would otherwise
        come back as an attribute called "No data available".
        """
        cells = self._rows().evaluate_all(
            "els => els.filter(e => !e.classList.contains('k-grid-norecords'))"
            "        .map(e => ((e.cells[0] || {}).innerText || '').trim())")
        return [name for name in cells if name]

    def _first_row_text(self) -> str:
        rows = self.page.locator(f"{self.SEL['grid']} tbody tr")
        return rows.first.inner_text().strip() if rows.count() else ""

    def _go_to_next_page(self) -> bool:
        """Click through to the next page; False when this was the last.

        Advancing is confirmed by the first row changing rather than by waiting
        a fixed time: the grid re-binds over AJAX, and reading straight after
        the click returns the page we were already on.
        """
        nav = self.page.locator(self.SEL["next_page"])
        if nav.count() == 0:
            return False                      # no pager at all: one page
        button = nav.first
        classes = button.get_attribute("class") or ""
        if "k-state-disabled" in classes or "k-disabled" in classes:
            return False                      # already on the last page

        before = self._first_row_text()
        button.click(timeout=self.timeout)

        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            if self._first_row_text() not in ("", before):
                return True
            self.page.wait_for_timeout(200)
        raise library.StepError(
            "clicked to the next page of the product attribute list but the "
            "grid never changed", step="attributes")

    def _total(self) -> int | None:
        """How many attributes the pager says exist ("1 - 15 of 23 items").

        None when there is no pager — a grid short enough not to need one — and
        None rather than a raise when the text is there but unparseable, because
        this is a cross-check on the walk above, not the source of the answer.
        """
        info = self.page.locator(f"{self.SEL['grid']} {self.SEL['pager_info']}")
        if info.count() == 0:
            return None
        match = re.search(r"of\s+([\d,]+)\s+items", info.first.inner_text(), re.I)
        return int(match.group(1).replace(",", "")) if match else None
