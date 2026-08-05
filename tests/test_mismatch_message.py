#!/usr/bin/env python3
"""What the runner says when the combination grid and the payload disagree.

The old message printed both halves of the report as raw dicts:

    combinations do not line up — unmatched rows: [{'Color': 'OS', 'Size': 'OS'}],
    payload entries with no row: []

True, and nearly useless — it says nothing about which value is wrong, why it
is there, or that re-running will not clear it.
"""

from __future__ import annotations

from app.automation.runner import mismatch_message

ROW = {
    "sku": "CP-20-100",
    "color_attr": "Color",
    "colors": ["Amber Lens", "Clear Lens"],
    "size_attr": "Size",
    "sizes": ["OS"],
}


def test_a_stray_attribute_value_is_named_and_explained():
    """The real failure: "OS" filed as a ninth COLOUR by an earlier run."""
    report = {"unmatched": [{"Color": "OS", "Size": "OS"}], "missing": []}
    message = mismatch_message(report, ROW)

    assert "Color = 'OS'" in message          # the value that is actually wrong
    assert "left over from an earlier run" in message
    assert "survive re-running" in message    # why retrying will not help
    assert "Product attributes tab" in message


def test_the_size_is_not_blamed_when_the_workbook_lists_it():
    """Both cells of that grid row read 'OS'. Only the colour is wrong — the
    size 'OS' is exactly what the workbook asked for."""
    report = {"unmatched": [{"Color": "OS", "Size": "OS"}], "missing": []}
    assert "Size = " not in mismatch_message(report, ROW)


def test_a_missing_combination_is_the_opposite_problem():
    report = {"unmatched": [], "missing": [("Amber Lens", "OS")]}
    message = mismatch_message(report, ROW)
    assert "no combination generated" in message
    assert "missing something the workbook expects" in message
    assert "left over" not in message         # not the same fix, do not say it


def test_both_directions_are_reported_together():
    report = {"unmatched": [{"Color": "OS", "Size": "OS"}],
              "missing": [("Clear Lens", "OS")]}
    message = mismatch_message(report, ROW)
    assert "left over from an earlier run" in message
    assert "no combination generated" in message


def test_known_values_in_an_odd_pairing_fall_back_to_listing_the_rows():
    """Every value is one the workbook lists, so nothing is stray — the pairing
    itself is wrong. No cleanup advice, because cleanup is not the fix."""
    report = {"unmatched": [{"Color": "Amber Lens", "Size": "OS"}], "missing": []}
    message = mismatch_message(report, ROW)
    assert "match nothing in the workbook" in message
    assert "left over" not in message


def test_matching_is_case_insensitive_against_the_workbook():
    report = {"unmatched": [{"Color": "AMBER LENS", "Size": "os"}], "missing": []}
    assert "match nothing in the workbook" in mismatch_message(report, ROW)
