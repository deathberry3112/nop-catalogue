#!/usr/bin/env python3
"""Page object for the product create/edit form.

    /Admin/Product/Create      a new product
    /Admin/Product/Edit/{id}   an existing one — the same form, with an Id

    product = ManageProductsPage(page).add_new()
    product.fill_basics(row).save()
    product.add_specification(row["spec_value"])

The two are one page object because nopCommerce serves one form for both; the
only difference is whether #Id has a value. Anything that needs the product to
exist first (attributes, combinations) is only reachable after a save, which is
why save() defaults to Save-and-Continue-Edit.

Verified against this instance: nopCommerce 3.x. Note the save buttons are
<input name=save>, not the <button name=save> that library.SEL describes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Sequence

import library

if TYPE_CHECKING:
    from playwright.sync_api import Page


def _norm_option(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


def resolve_option(value: str, labels: list[str]) -> str | None:
    """Which of `labels` the workbook's `value` means, or None if none of them.

    Exact first, always: when the sheet says what the catalogue says, that is
    the answer and no normalising can change it. Only then fall back to
    case-and-spacing insensitive, because these values are typed into
    hand-maintained workbooks and are inconsistent in exactly the way the column
    headers are — app.parser already matches those with `_norm` for the same
    reason. "Zingsafetyglasses" against nop's "ZingSafetyGlasses" failed five
    rows *after* creating five products, leaving each one half-configured.

    The fallback cannot invent anything: `labels` is the closed list nop is
    offering, so whatever comes back is a real option. What it must not do is
    choose between two of them — options differing only in case are different
    options, and filing a product under the wrong one is both silent and wrong.
    That is a StepError, not a coin flip.
    """
    if value in labels:
        return value

    key = _norm_option(value)
    hits = [label for label in labels if _norm_option(label) == key]
    if len(hits) > 1:
        raise library.StepError(
            f"{value!r} matches {len(hits)} options that differ only in case or "
            f"spacing ({hits}) — those are different options, so the workbook "
            f"has to say which one", step="specification")
    return hits[0] if hits else None

log = logging.getLogger("pages.product")


class ProductPage:
    """The product form, new or existing."""

    CREATE_PATH = "/Admin/Product/Create"
    EDIT_PATH = "/Admin/Product/Edit/"

    SEL = {
        # --- product info ------------------------------------------------
        "id":        "#Id",
        "name":      "#Name",                       # verified
        "sku":       "#Sku",                        # verified
        "price":     "#Price",                      # verified
        "published": "#Published",

        # --- form actions ------------------------------------------------
        # <input>, not <button>: library.SEL["save"] does not match this admin.
        "save":          "input[name=save]",        # verified
        "save_continue": "input[name=save-continue]",   # verified
        # Deleting takes two clicks. The header button is a <span> that opens a
        # Kendo modal; the real submit sits inside that modal, hidden until it
        # opens. Clicking the submit directly waits forever on a hidden element.
        "delete_trigger": "#product-delete",        # verified: <span> in .options
        "delete_confirm": "#productmodel-delete-confirmation input[type=submit][value='Delete']",

        # --- specification attributes tab ---------------------------------
        "spec_type":      "#AddSpecificationAttributeModel_AttributeTypeId",       # verified
        "spec_attribute": "#AddSpecificationAttributeModel_SpecificationAttributeId",  # verified
        "spec_option":    "#AddSpecificationAttributeModel_SpecificationAttributeOptionId",  # verified
        "spec_add":       "#addProductSpec",        # verified: "Add attribute"
        "spec_grid":      "#specificationattributes-grid",                          # verified

        # --- product attributes tab ---------------------------------------
        "attr_grid":      "#productattributemappings-grid",                         # verified
        "values_grid":    "#productattributevalues-grid",                           # verified
        "generate_combinations": "#btnGenerateAllCombinations",                     # verified
        "combinations_grid":     "#attributecombinations-grid",                     # verified

        # --- the attribute-value editor (a popup form, see VALUE_*_PATH) ----
        "value_name":   "#Name",                    # verified
        "value_save":   "input[name=save]",         # verified
        # CUSTOM TO THIS ADMIN, not stock nopCommerce: a select2-backed <select>
        # of ~2,100 named colour swatches. Stock nop puts an RGB colour picker
        # (#ColorSquaresRgb) here and only when the attribute's control type is
        # "Color squares"; this one is a plain dropdown, present regardless of
        # control type, and it is what the storefront renders.
        "value_swatch": "#ddlColorSwatch",          # verified
    }

    # The value editor is a popup window opened by JS. Navigating straight to it
    # is the same form without the second window to manage.
    VALUE_CREATE_PATH = "/Admin/Product/ProductAttributeValueCreatePopup?productAttributeMappingId="
    VALUE_EDIT_PATH = "/Admin/Product/ProductAttributeValueEditPopup/"
    VALUES_PATH = "/Admin/Product/EditAttributeValues?productAttributeMappingId="

    def __init__(self, page: "Page", *, timeout: int = library.TIMEOUT):
        self.page = page
        self.timeout = timeout

    # --- identity ---------------------------------------------------------

    @property
    def product_id(self) -> str | None:
        """The id from the URL, or None on an unsaved product."""
        match = re.search(r"/Product/Edit/(\d+)", self.page.url, re.I)
        return match.group(1) if match else None

    def is_saved(self) -> bool:
        return self.product_id is not None

    # --- product info -----------------------------------------------------

    def fill_basics(self, row: dict) -> "ProductPage":
        """Fill what the payload actually specifies: the name and the SKU.

        Everything else on this form keeps the admin's own defaults. Filling
        fields the payload is silent about would be inventing data.
        """
        library.fill(self.page, self.SEL["name"], row["product_name"], timeout=self.timeout)
        library.fill(self.page, self.SEL["sku"], row["sku"], timeout=self.timeout)
        return self

    def save(self, *, continue_edit: bool = True) -> "ProductPage":
        """Save. Defaults to Save-and-Continue-Edit because the attribute tabs
        need a saved product and plain Save bounces you back to the list."""
        target = self.SEL["save_continue"] if continue_edit else self.SEL["save"]
        library.click(self.page, target, timeout=self.timeout)
        self.page.wait_for_load_state("load")
        library.wait_ready(self.page, timeout=self.timeout)

        problems = library.validation_errors(self.page)
        if problems:
            raise library.StepError("save rejected: " + "; ".join(problems), step="save")
        return self

    # --- tabs -------------------------------------------------------------

    def open_tab(self, name: str) -> "ProductPage":
        self.page.get_by_role("tab", name=name).first.click(timeout=self.timeout)
        self.page.wait_for_timeout(400)             # Kendo swaps the panel in
        library.wait_ready(self.page, timeout=self.timeout)
        return self

    # --- specification attributes -----------------------------------------

    def add_specification(self, value: str) -> "ProductPage":
        """Add the specification option `value` (e.g. "ZingHardHats").

        The payload names the *option* but not the attribute that owns it, so
        this searches the attribute dropdown for whichever one offers that
        option — today "ProductType", but the payload never says so and a second
        attribute could own the next value. Selecting an attribute reloads the
        option list over AJAX, hence the wait inside the loop.
        """
        if not self.is_saved():
            raise library.StepError(
                "add_specification needs a saved product", step="specification")

        self.open_tab("Specification attributes")
        # Resolved, not compared: a product carrying "ZingSafetyGlasses" is
        # already done even when the sheet spells it "Zingsafetyglasses", and an
        # exact check here would add a second copy on every resume.
        if resolve_option(value, self.specifications()):
            log.debug("specification %r already present", value)
            return self

        attribute = self.page.locator(self.SEL["spec_attribute"])
        attribute_values = attribute.locator("option").evaluate_all(
            "els => els.map(e => ({value: e.value, label: e.innerText.trim()}))")

        tried = {}
        for candidate in attribute_values:
            attribute.select_option(value=candidate["value"])
            # Wait for the reload this just triggered, rather than guessing at
            # 400ms and racing it. See _await_option_reload.
            self._await_option_reload()
            library.wait_ready(self.page, timeout=self.timeout)

            tried[candidate["label"]] = self._spec_option_labels()
            # `chosen` is nop's spelling of the value, which is not necessarily
            # the workbook's. Everything downstream checks against nop's.
            chosen = self._select_spec_option(value)
            if not chosen:
                continue

            # One last look before posting. _select_spec_option verified the
            # selection, but that was a moment ago and the list can still be
            # swapped in between — so re-read it at the latest possible point
            # and re-select rather than post whatever a reset landed on.
            for _ in range(3):
                if self._selected_spec_option() == chosen:
                    break
                log.warning("the option list moved under %r — reselecting", chosen)
                if not self._select_spec_option(value):
                    chosen = None
                    break
            else:
                chosen = None
            if not chosen or self._selected_spec_option() != chosen:
                continue

            library.click(self.page, self.SEL["spec_add"], timeout=self.timeout)
            library.wait_ready(self.page, timeout=self.timeout)

            # Poll, don't sleep. The add is an AJAX post and the Kendo grid
            # redraws when it returns; a fixed pause passes on a quick response
            # and fails on a slow one, which is exactly the flake this saw.
            if not self.has_specification(chosen):
                landed = self.specifications()
                raise library.StepError(
                    f"clicked Add attribute for {chosen!r} but the grid shows "
                    f"{landed} — the option list was replaced between choosing "
                    f"and adding, so the wrong option was posted. "
                    + (f"{landed} is now on this product and has to be removed "
                       f"by hand before re-running, or the product will end up "
                       f"with both." if landed else
                       "Nothing was added, so re-running is safe."),
                    step="specification")
            log.info("specification %s = %s", candidate["label"], chosen)
            return self

        raise library.StepError(
            f"no specification attribute offers the option {value!r}; "
            f"available: {tried}", step="specification")

    def _spec_option_labels(self) -> list[str]:
        return self.page.locator(f"{self.SEL['spec_option']} option").evaluate_all(
            "els => els.map(e => e.innerText.trim())")

    def _selected_spec_option(self) -> str:
        return self.page.locator(self.SEL["spec_option"]).evaluate(
            "e => e.selectedIndex >= 0 ? e.options[e.selectedIndex].text.trim() : ''")

    def _await_option_reload(self, *, timeout_ms: int = 2000) -> bool:
        """Wait for the option list to be REPLACED after choosing an attribute.

        Selecting an attribute reloads this list over AJAX, and
        library.wait_ready cannot see it happen: wait_ready watches for a
        visible busy overlay and this request does not raise one, so it returns
        immediately having proved nothing. Everything after it races the
        response — and losing that race is how CP-20-105ECO was filed as
        "HighVisVest", which is entry number one in the list.

        COMPARING THE CONTENT DOES NOT WORK, which is worth stating because it
        is the obvious thing to reach for and it fails silently: re-selecting
        the attribute already showing reloads the identical 37 options, so the
        list reads the same before and after while the <option> nodes beneath
        it are swapped and the selection resets to the first entry. Two equal
        reads prove nothing.

        So mark the nodes that are there now and wait for an unmarked one to
        appear. That detects the swap itself rather than its shadow.

        Returns False on timeout, which means either the response had already
        landed before the marking or it never came; the caller carries on and
        the verify-before-click check below is what covers both.
        """
        options = self.page.locator(f"{self.SEL['spec_option']} option")
        options.evaluate_all("els => els.forEach(e => e.dataset.nopSeen = '1')")

        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if options.evaluate_all("els => els.some(e => !e.dataset.nopSeen)"):
                return True
            self.page.wait_for_timeout(100)
        return False

    def _select_spec_option(self, value: str) -> str | None:
        """Select whichever option means `value`, and confirm it stuck.

        Choosing an attribute reloads this dropdown over AJAX. That response can
        land *after* the option has been picked, replacing the list and resetting
        the selection to its first entry — and then Add posts that entry instead.
        It is not hypothetical: a hard hat was filed as "HighVisVest" this way,
        and the run before it happened to be fast enough to work.

        So: read, select, re-read what is actually selected, and keep going
        until the DOM agrees. Returns the label actually selected — nop's
        spelling, not necessarily the workbook's — or None if the value is never
        on offer, which is how the caller knows to try the next attribute.

        Resolution happens inside the loop, not before it, because an empty or
        stale option list is exactly what this is polling through. Note that
        once resolved, the did-it-stick check is still an EXACT comparison:
        tolerating the workbook's spelling must not loosen the guard above.
        """
        option_select = self.page.locator(self.SEL["spec_option"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            canonical = resolve_option(value, self._spec_option_labels())
            if canonical:
                option_select.select_option(label=canonical)
                self.page.wait_for_timeout(250)
                if self._selected_spec_option() == canonical:
                    return canonical
            self.page.wait_for_timeout(250)
        return None

    def has_specification(self, value: str, *, wait: bool = True) -> bool:
        """Is `value` in the specification grid?

        Waits by default. The grid is filled by an AJAX call when the tab first
        opens, so reading it immediately sees an empty grid on a product that
        does have the value — a false negative that only shows up on a product
        created moments earlier, which is precisely when it gets checked.
        """
        if not wait:
            return value in self.specifications()
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            if value in self.specifications():
                return True
            self.page.wait_for_timeout(250)
        return False

    def specifications(self) -> list[str]:
        """The Value column of the specification grid, as it reads right now."""
        rows = self.page.locator(f"{self.SEL['spec_grid']} tbody tr")
        return [c.strip() for c in rows.evaluate_all(
            "els => els.map(e => (e.cells[2] ? e.cells[2].innerText : '').trim())") if c.strip()]

    # --- product attributes -----------------------------------------------

    def _pick_kendo(self, cell, label: str) -> None:
        """Choose `label` in a Kendo DropDownList.

        These are not <select> elements — Kendo hides the real input and draws a
        <span>, so select_option() has nothing to work with. The list is opened
        by clicking, and its items are rendered in a container appended to
        <body>, outside the cell they belong to.

        The label is matched whole: the attribute list holds both "Color" and
        "Sign Color", and a substring match takes whichever comes first.
        """
        cell.locator("span.k-dropdown").first.click(timeout=self.timeout)
        self.page.wait_for_timeout(300)
        item = self.page.locator(
            "div.k-animation-container:visible ul.k-list li.k-item"
        ).filter(has_text=re.compile(rf"^\s*{re.escape(label)}\s*$"))
        if item.count() == 0:
            available = self.page.locator(
                "div.k-animation-container:visible ul.k-list li.k-item"
            ).evaluate_all("els => els.map(e => e.innerText.trim())")
            raise library.StepError(
                f"no dropdown item {label!r}; available: {available}", step="attribute")
        item.first.click(timeout=self.timeout)
        self.page.wait_for_timeout(300)

    def _settled_rows(self, selector: str, wait: bool):
        """Rows of a Kendo grid, once it has had a chance to fill.

        Every one of these grids is populated by an AJAX call fired when its tab
        is first shown, so reading straight after open_tab() returns nothing on a
        product that is fully configured. Callers that need to distinguish "empty"
        from "not loaded yet" pay a short wait; nothing else changes.
        """
        rows = self.page.locator(selector)
        if wait:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and rows.count() == 0:
                self.page.wait_for_timeout(200)
        return rows

    def attribute_mappings(self, *, wait: bool = True) -> dict[str, str]:
        """{attribute name: mapping id} for this product, read off the grid."""
        rows = self._settled_rows(f"{self.SEL['attr_grid']} tbody tr", wait)
        found = {}
        for entry in rows.evaluate_all(
                "els => els.map(e => ({name: (e.cells[0]||{}).innerText || '',"
                " href: (e.querySelector('a[href*=EditAttributeValues]')||{}).href || ''}))"):
            match = re.search(r"productAttributeMappingId=(\d+)", entry["href"] or "")
            name = (entry["name"] or "").strip()
            if name and match:
                found[name] = match.group(1)
        return found

    def add_attribute(self, name: str) -> str:
        """Map the product attribute `name` (e.g. "Color") and return its id.

        A no-op when the mapping already exists — without that, re-running turns
        one Color attribute into three. Control type is left at the admin's
        default of "Drop-down list", which is what the existing products use.
        """
        if not self.is_saved():
            raise library.StepError("add_attribute needs a saved product", step="attribute")

        self.open_tab("Product attributes")
        existing = self.attribute_mappings()
        if name in existing:
            return existing[name]

        grid = self.page.locator(self.SEL["attr_grid"])
        grid.locator("a.k-grid-add").first.click(timeout=self.timeout)
        self.page.wait_for_timeout(600)

        row = grid.locator("tr.k-grid-edit-row").first
        self._pick_kendo(row.locator("td[data-container-for=ProductAttributeId]"), name)

        # Is Required matters more than it looks. Left off, "choose nothing" is
        # a valid selection, so Generate emits partial combinations too: a
        # 2-colour 1-size product produced five rows — the two real ones plus
        # {Size: OS}, {Color: Lime} and {Color: Orange}, which match nothing in
        # the payload. The existing product 388 has it on for the same reason.
        required = row.locator("input[name=IsRequired]")
        if not required.is_checked():
            required.click(timeout=self.timeout)

        grid.locator("a.k-grid-update").first.click(timeout=self.timeout)

        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            mappings = self.attribute_mappings()
            if name in mappings:
                log.info("attribute %s -> mapping %s", name, mappings[name])
                return mappings[name]
            self.page.wait_for_timeout(250)
        raise library.StepError(
            f"added the {name!r} attribute but it never appeared in the grid "
            f"(showing: {list(self.attribute_mappings())})", step="attribute")

    def _value_rows(self, mapping_id: str) -> list[dict]:
        """Every value on this mapping as {"name", "value_id"}.

        The value id only exists in the Edit button's onclick — the grid has no
        link column — which is why this reads an attribute rather than an href:

            OpenWindow('/Admin/Product/ProductAttributeValueEditPopup/4376?...')
        """
        wanted = f"{self.VALUES_PATH}{mapping_id}"
        if wanted not in self.page.url:
            library.goto(self.page, wanted, ready=False)
        rows = self.page.locator(f"{self.SEL['values_grid']} tbody tr")

        # The grid fills over AJAX; an immediate read sees nothing.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and rows.count() == 0:
            self.page.wait_for_timeout(200)

        out = []
        for entry in rows.evaluate_all(
                "els => els.map(e => {"
                " const b = e.querySelector('input[onclick*=ProductAttributeValueEditPopup]');"
                " const m = b ? /ValueEditPopup\\/(\\d+)/.exec(b.getAttribute('onclick')) : null;"
                " return {name: (e.cells[1] ? e.cells[1].innerText : '').trim(),"
                "         value_id: m ? m[1] : null};})"):
            if entry["name"]:
                out.append(entry)
        return out

    def attribute_values(self, mapping_id: str) -> list[str]:
        """The value names already defined for a mapping."""
        return [r["name"] for r in self._value_rows(mapping_id)]

    def swatch_options(self, mapping_id: str) -> list[tuple[str, str]]:
        """(id, label) for every colour swatch the value editor offers.

        Read off the live page rather than hardcoded because it is a catalogue
        table, not a constant — roughly 2,100 entries here, and the caller
        chooses among exactly these. Returns [] when the field is absent, which
        is how a stock nopCommerce (no swatch dropdown) degrades to doing
        nothing instead of failing every product.
        """
        library.goto(self.page, f"{self.VALUE_CREATE_PATH}{mapping_id}", ready=False)
        self.page.wait_for_timeout(400)
        select = self.page.locator(self.SEL["value_swatch"])
        if select.count() == 0:
            log.warning("this admin has no %s field — swatches will be left unset",
                        self.SEL["value_swatch"])
            return []
        return [(str(o["value"]), o["label"]) for o in
                select.locator("option").evaluate_all(
                    "els => els.map(e => ({value: e.value, label: e.innerText.trim()}))")
                if o["value"]]

    def _set_swatch(self, swatch_id: str) -> None:
        """Choose a swatch on the value form that is currently open.

        select_option works even though select2 hides the real <select> behind
        its own widget: the element keeps a 1x1 box, so Playwright's
        actionability check passes, and the form posts ColorSwatchId from the
        <select> itself. Verified end to end on this admin — set, saved,
        re-read.
        """
        self.page.locator(self.SEL["value_swatch"]).select_option(
            value=str(swatch_id), timeout=self.timeout)

    def value_swatch(self, value_id: str) -> str:
        """The swatch id currently set on a value, or "" for none.

        Costs a page load per value, and there is no cheaper way: the values
        grid has no swatch column, so the editor is the only place it is
        readable.
        """
        library.goto(self.page, f"{self.VALUE_EDIT_PATH}{value_id}", ready=False)
        self.page.wait_for_timeout(300)
        select = self.page.locator(self.SEL["value_swatch"])
        return select.input_value() if select.count() else ""

    def add_attribute_values(self, mapping_id: str, values: list[str], *,
                             swatches: dict[str, str] | None = None) -> dict:
        """Add each value that isn't already there. Idempotent, so a re-run
        after a half-finished one fills in the gaps instead of duplicating.

        `swatches` maps a value's name to the colour swatch id it should carry
        ({"Yellow": "25"}). Resolving a colour to an id is app.colour.swatch's
        job, not this one — a page object knows the DOM and nothing about what
        colours mean. A value missing from the dict keeps whatever the form
        defaults to, which is no swatch.

        Values that already existed are checked and repaired too, because the
        common case is exactly that: products created before swatches were set
        at all. An existing swatch is never overwritten — a human may have
        chosen it deliberately, and this has no way to tell.

        Returns {"set": {name: id}, "kept": {name: id}, "unchanged": [names]}.
        """
        swatches = swatches or {}
        existing = {r["name"] for r in self._value_rows(mapping_id)}
        report: dict = {"set": {}, "kept": {}, "unchanged": []}

        for value in values:
            if value in existing:
                continue
            library.goto(self.page, f"{self.VALUE_CREATE_PATH}{mapping_id}", ready=False)
            library.fill(self.page, self.SEL["value_name"], value, timeout=self.timeout)
            if value in swatches:
                self._set_swatch(swatches[value])
            library.click(self.page, self.SEL["value_save"],
                          timeout=self.timeout, settle=False)
            self.page.wait_for_load_state("load")

            if value not in self.attribute_values(mapping_id):
                raise library.StepError(
                    f"added value {value!r} to mapping {mapping_id} but it is not "
                    f"in the list (showing: {self.attribute_values(mapping_id)})",
                    step="attribute-value")
            if value in swatches:
                report["set"][value] = swatches[value]
            log.info("value %s -> mapping %s", value, mapping_id)

        # Repair pass, over values this call did not create.
        for row in self._value_rows(mapping_id):
            name, value_id = row["name"], row["value_id"]
            if name in report["set"] or name not in swatches or not value_id:
                continue
            current = self.value_swatch(value_id)
            if current:
                report["kept"][name] = current
                continue
            self._set_swatch(swatches[name])
            library.click(self.page, self.SEL["value_save"],
                          timeout=self.timeout, settle=False)
            self.page.wait_for_load_state("load")
            report["set"][name] = swatches[name]
            log.info("swatch %s -> value %s (%s)", swatches[name], name, value_id)

        report["unchanged"] = [v for v in values
                               if v not in report["set"] and v not in report["kept"]]
        return report

    # --- attribute combinations -------------------------------------------

    def combinations(self, *, wait: bool = True) -> list[dict]:
        """Every combination row as {"attributes": {"Color": "Yellow"}, "sku": ...}.

        The Attributes cell renders as "Color: Yellow\\nSize: L", which is the
        only place the grid says what a row actually is — and the reason nothing
        here needs to assume the rows come back in any particular order.
        """
        rows = self._settled_rows(f"{self.SEL['combinations_grid']} tbody tr", wait)
        out = []
        for entry in rows.evaluate_all(
                "els => els.map(e => ({attrs: (e.cells[0]||{}).innerText || '',"
                " sku: ((e.cells[3]||{}).innerText || '').trim(),"
                " allow: /active-true/.test(((e.cells[2]||{}).innerHTML)||'')}))"):
            attributes = {}
            for line in (entry["attrs"] or "").splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    if key.strip():
                        attributes[key.strip()] = value.strip()
            if attributes:
                out.append({"attributes": attributes, "sku": entry["sku"],
                            "allow_out_of_stock": bool(entry["allow"])})
        return out

    def generate_combinations(self) -> list[dict]:
        """Click "Generate all possible combinations" and return what appeared.

        Does nothing if combinations already exist: nopCommerce appends rather
        than replaces, so a second click on a product that already has them is
        how you end up with duplicates.
        """
        self.open_tab("Product attributes")
        self.open_tab("Attribute combinations")
        self.page.wait_for_timeout(600)

        existing = self.combinations()
        if existing:
            log.debug("%d combination(s) already present", len(existing))
            return existing

        self.page.once("dialog", lambda dialog: dialog.accept())
        library.click(self.page, self.SEL["generate_combinations"],
                      timeout=self.timeout, settle=False)
        self.page.wait_for_load_state("load")
        library.wait_ready(self.page, timeout=self.timeout)
        self.open_tab("Product attributes")
        self.open_tab("Attribute combinations")

        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            found = self.combinations()
            if found:
                log.info("generated %d combination(s)", len(found))
                return found
            self.page.wait_for_timeout(300)
        raise library.StepError("generating combinations produced no rows",
                                step="combinations")

    def apply_combination_skus(self, wanted: dict[tuple[str, ...], str], *,
                               attributes: "Sequence[str]",
                               allow_out_of_stock: bool = False) -> dict:
        """Write each combination's SKU, matching on what the row actually is.

        `wanted` is keyed by a tuple of attribute values in `attributes` order —
        ("Lime", "S/M") for a vest, ("Aluminum", "7 x 10") for a sign board,
        ("White",) for a hard hat with no sizes. The caller gets that order from
        combination_skus.attribute_names(), and this reads each generated row's
        own Attributes cell to build the same tuple, so nothing depends on the
        order nopCommerce generated them in or on that matching the payload.

        Returns {"set": [...], "unmatched": [...], "missing": [...]} rather than
        raising: the caller can decide whether a leftover row is fatal, and gets
        told about all of them at once instead of the first.
        """
        self.open_tab("Product attributes")
        self.open_tab("Attribute combinations")
        names = list(attributes)

        def key_of(combination: dict) -> tuple[str, ...]:
            cells = combination["attributes"]
            return tuple(str(cells.get(name) or "") for name in names)

        def needs_work(combination: dict) -> bool:
            key = key_of(combination)
            return (key in wanted
                    and (combination["sku"] != wanted[key]
                         or combination["allow_out_of_stock"] != allow_out_of_stock))

        # One row per pass, re-reading the grid each time. Kendo rebinds the
        # whole tbody after every Update, so walking a list of row handles taken
        # once at the start goes stale partway through — that silently left five
        # of nine SKUs unset. Re-reading also makes this resumable: a rerun over
        # a half-filled product only touches what is still wrong.
        stuck = 0
        for _ in range(len(wanted) * 3 + 25):
            # No wait: the grid is known to be populated by now, and a 4-second
            # pause on the final pass (which finds nothing to do) would be paid
            # once per product for nothing.
            pending = [(i, c) for i, c in enumerate(self.combinations(wait=False))
                       if needs_work(c)]
            if not pending:
                break
            index, combination = pending[0]
            try:
                self._edit_combination(index, wanted[key_of(combination)], allow_out_of_stock)
                stuck = 0
            except Exception as exc:
                # Almost always "element detached": the grid rebound underneath
                # this row. The next pass re-reads and tries again, so a lost
                # round trip costs a retry rather than the whole product.
                stuck += 1
                log.debug("combination row %d needs another go: %s", index, exc)
                self._cancel_open_editor()
                if stuck >= 5:
                    raise library.StepError(
                        f"could not edit combination {key_of(combination)} after "
                        f"5 attempts: {exc}", step="combinations") from exc
        else:
            raise library.StepError(
                "combination SKUs never settled — each pass still found work to do",
                step="combinations")

        final = self.combinations()
        applied = {key_of(c): c["sku"] for c in final
                   if key_of(c) in wanted and c["sku"] == wanted[key_of(c)]}
        unmatched = [c["attributes"] for c in final if key_of(c) not in wanted]
        missing = {k: v for k, v in wanted.items() if k not in applied}
        log.info("combination SKUs: %d set, %d unmatched row(s), %d missing",
                 len(applied), len(unmatched), len(missing))
        return {"set": applied, "unmatched": unmatched, "missing": missing}

    def _edit_combination(self, index: int, sku: str, allow_out_of_stock: bool) -> None:
        """Open row `index` for editing, set its SKU and stock flag, save.

        Waits for the edit row to close rather than pausing a fixed time: the
        save is an AJAX round trip, and the next pass must not read the grid
        while this one is still redrawing it.
        """
        grid = self.page.locator(self.SEL["combinations_grid"])

        # The save before this one leaves a loading mask over the page and a
        # half-rebound tbody behind it. Clicking into that gets you either a
        # detached element or "<html> intercepts pointer events".
        library.wait_ready(self.page, timeout=self.timeout)
        grid.locator("tbody tr").nth(index).locator("a.k-grid-edit").first.click(
            timeout=self.timeout)

        edit = grid.locator("tr.k-grid-edit-row").first
        edit.wait_for(state="visible", timeout=self.timeout)
        library.wait_ready(self.page, timeout=self.timeout)

        edit.locator("input[name=Sku]").fill(sku, timeout=self.timeout)
        edit.locator("input[name=AllowOutOfStockOrders]").set_checked(
            allow_out_of_stock, timeout=self.timeout)

        grid.locator("a.k-grid-update").first.click(timeout=self.timeout)
        edit.wait_for(state="detached", timeout=self.timeout)
        library.wait_ready(self.page, timeout=self.timeout)

    def _cancel_open_editor(self) -> None:
        """Close a half-open inline editor so the next pass starts clean."""
        cancel = self.page.locator(
            f"{self.SEL['combinations_grid']} a.k-grid-cancel")
        try:
            if cancel.count():
                cancel.first.click(timeout=3_000)
        except Exception:
            pass
        try:
            library.wait_ready(self.page, timeout=self.timeout)
        except Exception:
            pass

    # --- destructive ------------------------------------------------------

    def delete(self) -> None:
        """Delete this product. There is no undo — callers are expected to have
        checked that this is a product the automation itself created."""
        if not self.is_saved():
            raise library.StepError("nothing to delete: product was never saved",
                                    step="delete")
        sku = self.page.locator(self.SEL["sku"]).input_value()
        log.warning("deleting product %s (%s)", self.product_id, sku)

        library.click(self.page, self.SEL["delete_trigger"], timeout=self.timeout)
        confirm = self.page.locator(self.SEL["delete_confirm"]).first
        try:
            confirm.wait_for(state="visible", timeout=self.timeout)
        except Exception:
            raise library.StepError(
                "the delete confirmation dialog never opened", step="delete") from None

        confirm.click(timeout=self.timeout)
        self.page.wait_for_load_state("load")
        library.wait_ready(self.page, timeout=self.timeout)
