#!/usr/bin/env python3
"""Refusing a sheet that names an attribute the store does not define.

Attributes live at two levels in nopCommerce: the store-wide list under
Catalog > Attributes > Product attributes defines the names, and a product's
own tab maps it to one of them. add_attribute drives the second and can only
pick what the first holds.

Without the check these cover, a sheet naming "Sign Material" in a store that
has never heard of it fails at the dropdown — AFTER the product has been
created and saved. The row reports failed and a half-configured product is left
in the client's catalogue. The point of every test here is that this now costs
nothing: the row is refused before a single page is loaded.

Nothing here creates the attribute, and test_the_store_is_never_written_to
below is the assertion that keeps it that way.
"""

from __future__ import annotations

import pytest

import library
from app.automation import preflight, runner
from app.errors import ErrorClass, classify

STORE = {"Color", "Size", "Sign Material"}


# ---------------------------------------------------------------------------
# which names are missing
# ---------------------------------------------------------------------------

def test_a_name_the_store_defines_is_not_reported():
    assert preflight.unknown_attributes(["Color", "Size"], STORE) == []


def test_a_name_the_store_lacks_is_reported():
    assert preflight.unknown_attributes(["Color", "Sign Header"], STORE) == [
        ("Sign Header", "")]


def test_a_name_differing_only_in_case_is_still_missing():
    """add_attribute's dropdown pick is case-sensitive (_pick_kendo matches
    ^label$ with no IGNORECASE), so folding case here would pass the row
    through to fail at exactly the step this exists to get ahead of."""
    unknown = preflight.unknown_attributes(["sign material"], STORE)
    assert unknown == [("sign material", "Sign Material")]


def test_a_name_differing_only_in_spacing_names_the_store_spelling():
    assert preflight.unknown_attributes(["Sign  Material"], STORE) == [
        ("Sign  Material", "Sign Material")]


def test_one_name_asked_for_twice_is_reported_once():
    assert preflight.unknown_attributes(["Sign Header", "Sign Header"], STORE) == [
        ("Sign Header", "")]


def test_the_report_follows_the_sheets_column_order():
    unknown = preflight.unknown_attributes(["Sign Header", "Sign Finish"], STORE)
    assert [name for name, _ in unknown] == ["Sign Header", "Sign Finish"]


# ---------------------------------------------------------------------------
# what the row is told
# ---------------------------------------------------------------------------

def test_the_message_says_which_name_and_where_to_add_it():
    message = preflight.refusal([("Sign Header", "")], STORE)
    assert "'Sign Header'" in message
    assert "Catalog > Attributes > Product attributes" in message


def test_the_message_quotes_the_stores_own_spelling():
    message = preflight.refusal([("sign material", "Sign Material")], STORE)
    assert "'Sign Material'" in message
    assert "match exactly" in message


def test_a_long_store_list_is_summarised_not_dumped():
    store = {f"Attribute {i}" for i in range(30)}
    message = preflight.refusal([("Sign Header", "")], store)
    assert f"+{30 - preflight.SHOWN} more" in message


def test_the_refusal_is_a_client_data_problem_not_a_colour_one():
    """The message quotes the store's attribute names. A store spelling its
    attribute "Colour" would otherwise match the DATA_COLOUR text pattern and
    be filed — and triaged — as an unresolvable colour."""
    error = preflight.UnknownAttribute(
        preflight.refusal([("Sign Header", "")], {"Colour", "Size"}),
        step="attributes")
    classified = classify(error)
    assert classified.error_class is ErrorClass.DATA_INVALID
    assert classified.blame == "client"
    assert not classified.retryable


# ---------------------------------------------------------------------------
# the guard the runner applies
# ---------------------------------------------------------------------------

def _row(*names: str) -> dict:
    return {"sku": "SN-A0710", "product_name": "BradySign",
            "attributes": [{"name": n, "values": ["x"], "swatches": [],
                            "is_colour": False} for n in names],
            "variants": [], "combinations": [], "errors": [], "warnings": []}


def test_a_row_the_store_can_map_passes():
    runner._refuse_unknown_attributes(_row("Color", "Size"), STORE)


def test_a_row_naming_an_unknown_attribute_is_refused():
    with pytest.raises(preflight.UnknownAttribute) as excinfo:
        runner._refuse_unknown_attributes(_row("Color", "Sign Header"), STORE)
    assert "Sign Header" in str(excinfo.value)


def test_a_row_with_no_attributes_has_nothing_to_check():
    """A configurator row, or a product that is only a specification."""
    runner._refuse_unknown_attributes(
        {"sku": "SN-1", "kind": "configurator"}, STORE)


def test_an_unreadable_store_list_checks_nothing():
    """None means "could not be read", never "the store has none". Refusing
    every row because a selector moved would be worse than the late failure
    this check exists to replace."""
    runner._refuse_unknown_attributes(_row("Sign Header"), None)


# ---------------------------------------------------------------------------
# end to end through run_batch: the product is never created
# ---------------------------------------------------------------------------

class RecordingStore(runner._NullStore):
    """_NullStore, plus what the run said. Everything else stays a no-op."""

    def __init__(self):
        self.finished: list[dict] = []
        self.events: list[str] = []

    def row_started(self, row, index=0):
        return f"row-{index}"

    def row_finished(self, row_id, **fields):
        self.finished.append(fields)

    def event(self, message, **kwargs):
        self.events.append(message)


class RefusingProducts:
    """Manage Products, if anything reached it. Nothing should."""

    def __init__(self, page):
        pass

    def find_by_sku(self, sku):
        raise AssertionError(
            "the catalogue was opened for a row whose attribute does not exist")

    def add_new(self):
        raise AssertionError("a product was created for a refused row")


def _wire(monkeypatch, *, names, products=RefusingProducts):
    """run_batch with the browser replaced: login is a no-op, the store's
    attribute list is `names` (or an exception to raise), products is a fake."""

    class Landing:
        def __init__(self, page, url=""):
            pass

        def login(self, email, password):
            return None

    class Attributes:
        def __init__(self, page, **kwargs):
            pass

        def names(self):
            if isinstance(names, Exception):
                raise names
            return names

    monkeypatch.setattr(runner, "LandingPage", Landing)
    monkeypatch.setattr(runner, "ManageProductsPage", products)
    monkeypatch.setattr(runner, "ProductAttributesPage", Attributes)
    monkeypatch.setattr(runner, "_local_ledger_skus", set)


def _run(rows, store, *, commit=True):
    return runner.run_batch(
        object(), rows, base_url="http://x/Admin", email="e", password="p",
        commit=commit, on_existing="skip", progress=lambda *a: None, store=store)


def test_the_store_is_never_written_to(monkeypatch):
    """The whole trade this design makes: the sheet's name is checked, and a
    name nobody created is refused rather than created. RefusingProducts fails
    the test if the catalogue is so much as opened."""
    _wire(monkeypatch, names=STORE)
    store = RecordingStore()

    summary = _run([_row("Color", "Sign Header")], store)

    assert summary["created"] == 0
    assert summary["failed"] == 1
    assert "Sign Header" in summary["results"][0]["error"]
    assert summary["results"][0]["error_class"] == ErrorClass.DATA_INVALID.value


def test_a_refused_row_reports_no_orphan(monkeypatch):
    """Distinct from merely failing: a row that dies inside
    _configure_attributes points at the half-configured product it created.
    This one has none to point at, and must not imply otherwise."""
    _wire(monkeypatch, names=STORE)
    store = RecordingStore()

    _run([_row("Sign Header")], store)

    assert store.finished[0]["status"] == "failed"
    assert store.finished[0]["product_url"] is None


def test_the_check_runs_on_a_preview_too(monkeypatch):
    """Preview cannot check attributes on the product — nopCommerce hides the
    tabs until it exists — but the store's list needs no product at all, so
    this is one attribute problem a dry run can catch."""
    _wire(monkeypatch, names=STORE)
    store = RecordingStore()

    summary = _run([_row("Sign Header")], store, commit=False)

    assert summary["failed"] == 1
    assert "Sign Header" in summary["results"][0]["error"]


def test_an_unreadable_list_falls_back_to_the_old_behaviour(monkeypatch):
    """A moved selector must not refuse work the admin would have accepted. The
    row goes on to the catalogue exactly as it did before this check existed."""

    class ReachedTheCatalogue(RefusingProducts):
        def find_by_sku(self, sku):
            raise library.StepError("reached the catalogue", step="lookup")

    _wire(monkeypatch,
          names=library.StepError("no product attributes read", step="attributes"),
          products=ReachedTheCatalogue)
    store = RecordingStore()

    summary = _run([_row("Sign Header")], store)

    assert "reached the catalogue" in summary["results"][0]["error"]
    assert any("could not read" in event for event in store.events)


def test_the_list_is_read_once_for_the_whole_batch(monkeypatch):
    """Per row it would be a page load per product to answer a question whose
    answer cannot change mid-run."""
    reads = []

    class Counting:
        def __init__(self, page, **kwargs):
            pass

        def names(self):
            reads.append(1)
            return STORE

    _wire(monkeypatch, names=STORE)
    monkeypatch.setattr(runner, "ProductAttributesPage", Counting)

    _run([_row("Sign Header"), _row("Sign Finish"), _row("Sign Header")],
         RecordingStore())

    assert len(reads) == 1
