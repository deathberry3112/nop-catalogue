#!/usr/bin/env python3
"""What the console is told about swatches — app/automation/runner.py.

app/colour/swatch.py decides which swatch a colour SHOULD get; these cover the
half that only the runner can know — which ones the admin actually accepted,
which were already set by hand and left alone, and what a step that died
half-way leaves behind for somebody to read.

The distinction the whole thing exists for: a swatch this run wrote and a
swatch a human wrote look identical on the storefront and mean opposite things
about who chose it. Reporting them as one number is how a wrong swatch stays
invisible.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.automation import runner


class FakeStore:
    """Enough RunStore to record a step. `steps` is what Postgres would hold."""

    def __init__(self):
        self.steps: list[dict] = []
        self.events: list[dict] = []

    @contextmanager
    def step(self, row_id, name, *, attempt=1):
        detail: dict = {}
        record = {"name": name, "attempt": attempt, "detail": detail,
                  "status": "ok"}
        self.steps.append(record)
        try:
            yield detail
        except BaseException:
            record["status"] = "failed"
            raise

    def event(self, message, *, level="info", row_id=None, data=None):
        self.events.append({"message": message, "level": level,
                            "data": data or {}})


class FakeProduct:
    """The two calls _resolve_swatches makes of a page object."""

    def __init__(self, options):
        self._options = options

    def swatch_options(self, mapping_id):
        return self._options


OPTIONS = [("25", "Yellow"), ("19", "Blue"), ("40", "Black"),
           ("21", "Purple"), ("35", "Purple")]

ROW = {"colors": ["Yellow", "Black", "Teal"],
       "swatches": ["Yellow", "Black", "Teal"]}


def seed(row=ROW, options=OPTIONS):
    """The detail a step is given before it runs.

    Goes through attributes_of() exactly as the runner does, which also keeps a
    colour/size shaped row (the kind a JSON payload carries) exercised now that
    attributes are general.
    """
    attributes = runner.attributes_of(row)
    attribute = attributes[0] if attributes else {
        "name": "Color", "values": [], "swatches": [], "is_colour": True}
    _, _, detail = runner._resolve_swatches(
        FakeProduct(options), "1", row, attribute)
    return detail


def by_colour(detail):
    return {e["colour"]: e for e in detail["swatches"]}


# --- before the step runs -----------------------------------------------------

def test_a_row_that_asks_for_no_swatches_records_nothing():
    """Not an empty list — nothing. A console line saying zero swatches on
    every product is how the four that need a person get ignored."""
    row = {"colors": ["Yellow"]}
    swatches, problems, detail = runner._resolve_swatches(
        FakeProduct(OPTIONS), "1", row, runner.attributes_of(row)[0])
    assert (swatches, problems, detail) == ({}, [], {})


def test_an_admin_with_no_swatch_field_says_so_rather_than_going_quiet():
    """The sheet asked and this admin cannot answer. Silence here would read as
    'no swatches were wanted', which is the opposite of what happened."""
    swatches, problems, detail = runner._resolve_swatches(
        FakeProduct([]), "1", ROW, runner.attributes_of(ROW)[0])
    assert swatches == {} and problems == []
    assert detail == {"swatches": [], "unavailable": True}


def test_the_seed_names_every_colour_before_anything_is_written():
    assert list(by_colour(seed())) == ["Yellow", "Black", "Teal"]


# --- folding in what the admin accepted ---------------------------------------

def test_a_swatch_this_run_wrote_reads_as_set():
    detail = runner._swatch_states(seed(), {"set": {"Yellow": "25"}})
    assert by_colour(detail)["Yellow"]["state"] == "set"
    assert detail["counts"]["set"] == 1


def test_a_swatch_a_human_already_chose_reads_as_kept_not_as_set():
    """The one distinction this feature exists for. Both end with a swatch on
    the value; only one of them was this run's decision."""
    detail = runner._swatch_states(seed(), {"set": {"Yellow": "25"},
                                            "kept": {"Black": "40"}})
    entries = by_colour(detail)
    assert entries["Black"]["state"] == "kept"
    assert entries["Yellow"]["state"] == "set"
    assert detail["counts"] == {"set": 1, "kept": 1, "skipped": 1}


def test_a_kept_swatch_reports_the_id_that_is_actually_on_the_value():
    """An existing swatch is never overwritten, so when the human's choice and
    ours disagree the console must show theirs — showing what we resolved would
    describe a product that does not exist."""
    detail = runner._swatch_states(seed(), {"kept": {"Yellow": "2049"}})
    yellow = by_colour(detail)["Yellow"]
    assert yellow["swatch_id"] == "2049"
    assert yellow["swatch_label"] is None
    assert "left it as it was" in yellow["note"]


def test_a_colour_with_no_swatch_stays_skipped_with_its_reason():
    detail = runner._swatch_states(seed(), {"set": {"Yellow": "25"}})
    teal = by_colour(detail)["Teal"]
    assert teal["state"] == "skipped"
    assert teal["cause"] == "unnamed"
    assert "Teal" in teal["note"]


def test_a_resolved_colour_the_editor_never_mentions_is_not_called_done():
    """We resolved it, the value editor reports neither setting nor keeping it.
    Calling that 'set' would be a lie about the storefront."""
    detail = runner._swatch_states(seed(), {"set": {}, "kept": {},
                                            "unchanged": ["Yellow", "Black"]})
    entries = by_colour(detail)
    assert entries["Yellow"]["state"] == "missed"
    assert "worth checking" in entries["Yellow"]["note"]
    assert detail["counts"]["missed"] == 2


def test_folding_leaves_the_seed_alone():
    """The seed is the step's own record of what it set out to do. A retry
    re-folds a fresh report onto it, so it has to survive being read twice."""
    original = seed()
    snapshot = json.dumps(original)
    runner._swatch_states(original, {"set": {"Yellow": "25"}})
    assert json.dumps(original) == snapshot


def test_a_row_with_nothing_recorded_folds_to_nothing():
    assert runner._swatch_states({}, {"set": {"Yellow": "25"}}) == {}


def test_the_folded_detail_is_still_json():
    detail = runner._swatch_states(seed(), {"set": {"Yellow": "25"},
                                            "kept": {"Black": "40"}})
    assert json.loads(json.dumps(detail)) == detail


# --- how it reaches the step --------------------------------------------------

def test_the_step_carries_the_detail_when_the_action_succeeds():
    store = FakeStore()
    runner._retrying(store, "row", "values_colour",
                     lambda: {"set": {"Yellow": "25"}},
                     detail=seed(),
                     detail_from=lambda report: runner._swatch_states(seed(), report))
    # Black resolves to 40 and the report mentions neither setting nor keeping
    # it, so it is 'missed' rather than done — see the test above.
    assert store.steps[0]["detail"]["counts"] == {"set": 1, "missed": 1,
                                                  "skipped": 1}


def test_a_step_that_dies_still_says_what_it_set_out_to_do():
    """The moment a run becomes interesting is the moment it fails. Recording
    the detail only on success is exactly the wrong way round."""
    store = FakeStore()

    def boom():
        raise RuntimeError("kendo ate the grid")

    with pytest.raises(RuntimeError):
        runner._retrying(store, "row", "values_colour", boom, detail=seed(),
                         detail_from=lambda report: {"counts": {"set": 99}})

    recorded = store.steps[-1]
    assert recorded["status"] == "failed"
    assert [e["colour"] for e in recorded["detail"]["swatches"]] \
        == ["Yellow", "Black", "Teal"]
    assert "counts" not in recorded["detail"]
    assert all(e["state"] in ("planned", "skipped")
               for e in recorded["detail"]["swatches"])


def test_a_broken_report_never_costs_a_product_that_worked():
    """Reporting is downstream of the work. A bug in here is a log line, not a
    failed row and not a retry of an action that already ran."""
    store = FakeStore()

    def explode(report):
        raise ValueError("bad report")

    result = runner._retrying(store, "row", "values_colour",
                              lambda: {"set": {}}, detail=seed(),
                              detail_from=explode)
    assert result == {"set": {}}
    assert store.steps[0]["status"] == "ok"
    assert len(store.steps) == 1


def test_a_step_with_nothing_to_say_records_nothing():
    store = FakeStore()
    runner._retrying(store, "row", "attribute_colour", lambda: "7")
    assert store.steps[0]["detail"] == {}
