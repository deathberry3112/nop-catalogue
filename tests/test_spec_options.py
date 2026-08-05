#!/usr/bin/env python3
"""Resolving a workbook's specification value to the option nop actually offers.

Pure string matching, no browser: resolve_option is deliberately a free
function so the rule can be tested without an admin to point at.

The case this exists for: the Eyewear sheet said "Zingsafetyglasses" where the
catalogue says "ZingSafetyGlasses". Five rows failed — each one *after* its
product had been created, leaving it half-configured.
"""

from __future__ import annotations

import pytest

import library
from pages.product_page import resolve_option

# the real ProductType list, trimmed to the interesting neighbours
NOP = ["HighVisVest", "HardHat", "SafetyGlasses", "HoneywellHardhat",
       "Mallory Diamond-M Vests", "ZingSafetyGlasses", "ZingHighViz",
       "ZingHardHats"]


def test_an_exact_value_resolves_to_itself():
    assert resolve_option("ZingHardHats", NOP) == "ZingHardHats"


def test_the_case_that_broke_the_eyewear_sheet():
    assert resolve_option("Zingsafetyglasses", NOP) == "ZingSafetyGlasses"


@pytest.mark.parametrize("typed", [
    "ZINGSAFETYGLASSES", "zingsafetyglasses", "  ZingSafetyGlasses  ",
])
def test_case_and_surrounding_space_are_tolerated(typed):
    assert resolve_option(typed, NOP) == "ZingSafetyGlasses"


def test_internal_spacing_is_collapsed_not_ignored():
    assert resolve_option("Mallory  Diamond-M   Vests", NOP) == "Mallory Diamond-M Vests"
    # collapsed, not stripped — the spaces still have to be there
    assert resolve_option("MalloryDiamond-MVests", NOP) is None


def test_a_value_nop_does_not_offer_is_still_a_miss():
    """The fallback loosens spelling, never membership — it must not start
    guessing at near-misses, or a typo becomes the wrong category."""
    assert resolve_option("ZingSafetyGlass", NOP) is None
    assert resolve_option("Sunglasses", NOP) is None


def test_an_exact_match_wins_over_a_case_insensitive_one():
    labels = ["ProductType", "producttype"]
    assert resolve_option("producttype", labels) == "producttype"


def test_two_options_differing_only_by_case_are_refused():
    """They are different options. Picking one would file the product under a
    category nobody chose, and say nothing about it."""
    labels = ["ProductType", "producttype"]
    with pytest.raises(library.StepError, match="differ only in case"):
        resolve_option("PRODUCTTYPE", labels)


def test_an_empty_option_list_is_a_miss_not_a_crash():
    """The dropdown reads empty while its AJAX reload is in flight, which is
    what _select_spec_option polls through."""
    assert resolve_option("ZingHardHats", []) is None
