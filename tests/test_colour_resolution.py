#!/usr/bin/env python3
"""The three-tier colour resolver.

No network: tier 3 is off in every test here (allow_llm=False) except the ones
that explicitly stub it. What is being pinned is the pipeline — that each tier
hands what it could not solve to the next, that the map is scoped, and that
nothing escapes verification.
"""

from __future__ import annotations

import json

import pytest

import combination_skus
import main
from app.colour import resolve
from app.colour.resolver import Tier, _heuristic
from app.colour.store import GLOBAL, ColourMap, normalise


@pytest.fixture(scope="module")
def payload() -> list[dict]:
    return main.load_rows(main.DEFAULT_PAYLOAD)


@pytest.fixture(autouse=True)
def _isolate_the_map(tmp_path, monkeypatch):
    """Every test gets the seed map and nothing else.

    resolve() persists what only the model could work out, which is a global
    side effect — without this the tests would depend on each other's order,
    and a developer's real var/colour-map.json would change the results.
    """
    import app.colour.store as store
    monkeypatch.setattr(store, "LOCAL_PATH", tmp_path / "colour-map.json")
    monkeypatch.setattr(store.ColourMap, "_load_remote", lambda self: [])
    monkeypatch.setattr(store.MAP, "_write_remote",
                        lambda entry, confidence: None)
    store.MAP.refresh()
    yield
    store.MAP.refresh()


# --- the shipped payload ----------------------------------------------------

def test_every_payload_row_resolves_without_the_model(payload):
    for row in payload:
        resolution = resolve(row, allow_llm=False)
        assert resolution.ok, f"{row['sku']}: {resolution.error}"


def test_the_payload_needs_no_model_at_all(payload):
    """The baseline the design rests on: tier 3 currently fires 0% of the time.
    If this ever fails, the curated map has drifted from the data."""
    for row in payload:
        resolution = resolve(row, allow_llm=False)
        assert Tier.LLM not in resolution.tiers.values()
        assert Tier.UNRESOLVED not in resolution.tiers.values()


def test_the_resolved_codes_match_the_heuristic_that_shipped(payload):
    """Belt and braces: the tiered pipeline must agree with the original
    single-tier implementation on the data both have seen."""
    for row in payload:
        assert resolve(row, allow_llm=False).codes == combination_skus.colour_codes(row)


# --- scoping: the reason the map is not flat --------------------------------

def test_the_same_colour_can_carry_different_codes_per_scope():
    """The shipped payload's direct counterexample to a flat colour map:
    Hi-Vis Lime is HLE on hard hats, Hi-Viz Lime is LE on vests."""
    hat = {"sku": "CP-30-C100V", "source_sheet": "HardHat",
           "colors": ["Hi-Vis Lime"], "sizes": [],
           "generate_combinations": True, "combinations": ["CP-30-C100V HLE"]}
    vest = {"sku": "CP-V-010", "source_sheet": "Vest",
            "colors": ["Hi-Viz Lime"], "sizes": ["OS"],
            "generate_combinations": True, "combinations": ["CP-V-010 LE OS"]}

    assert resolve(hat, allow_llm=False).codes == {"Hi-Vis Lime": "HLE"}
    assert resolve(vest, allow_llm=False).codes == {"Hi-Viz Lime": "LE"}


def test_normalise_folds_case_and_spacing_but_not_vis_viz():
    assert normalise("Hi-Vis  Lime") == normalise("hi vis lime")
    # Deliberately distinct: on this data they point at different codes.
    assert normalise("Hi-Vis Lime") != normalise("Hi-Viz Lime")


def test_a_scoped_entry_beats_a_global_one():
    colours = ColourMap()
    colours._entries = {}
    colours.record(GLOBAL, "Lime", "LM", source="human", verified=True)
    colours.record("Vest", "Lime", "LE", source="human", verified=True)
    assert colours.lookup("Lime", "Vest").code == "LE"
    assert colours.lookup("Lime", "HardHat").code == "LM"


def test_a_verified_entry_is_not_overwritten_by_a_proposal():
    colours = ColourMap()
    colours._entries = {}
    colours.record("Vest", "Lime", "LE", source="human", verified=True)
    colours.record("Vest", "Lime", "XX", source="llm", verified=False)
    assert colours.lookup("Lime", "Vest").code == "LE"


# --- tier hand-off ----------------------------------------------------------

def test_the_heuristic_leaves_what_it_cannot_score():
    """Unlike combination_skus.colour_codes this must not raise — leaving a
    colour out is exactly what hands it to the next tier."""
    resolved = _heuristic(["Yellow", "Ocean Slate"], ["YW", "XKQ"])
    assert resolved.get("Yellow") == "YW"
    assert "Ocean Slate" not in resolved


def test_the_heuristic_refuses_a_tie_rather_than_guessing():
    assert _heuristic(["Lime"], []) == {}


def test_a_lone_remainder_is_settled_by_elimination():
    """One unresolved colour and one unused code has exactly one possible
    answer. Taking it is deduction, not guessing — and it saves a model call."""
    row = {"sku": "X", "source_sheet": "nowhere",
           "colors": ["Yellow", "Ocean Slate"], "sizes": [],
           "generate_combinations": True,
           "combinations": ["X YW", "X ZZQ"]}
    resolution = resolve(row, allow_llm=False)
    assert resolution.ok
    assert resolution.codes["Ocean Slate"] == "ZZQ"


def test_an_unresolvable_row_fails_loudly_and_names_itself():
    row = {"sku": "X", "source_sheet": "nowhere",
           "colors": ["Ocean Slate", "Deep Fathom"], "sizes": [],
           "generate_combinations": True,
           "combinations": ["X ZZQ", "X QQZ"]}
    resolution = resolve(row, allow_llm=False)
    assert not resolution.ok
    assert "Ocean Slate" in resolution.error and "colour map" in resolution.error


def test_a_code_count_mismatch_is_refused_before_any_tier_runs():
    row = {"sku": "X", "source_sheet": "nowhere",
           "colors": ["Red", "Blue", "Green"], "sizes": [],
           "generate_combinations": True, "combinations": ["X RD", "X BE"]}
    resolution = resolve(row, allow_llm=False)
    assert not resolution.ok
    assert "3 colour(s)" in resolution.error


# --- verification: the gate every tier answers to ---------------------------

def test_a_wrong_mapping_does_not_survive_verification(monkeypatch):
    """The property that makes a model proposal safe: an assignment that does
    not account for every payload combination is rejected before it is used."""
    row = {"sku": "X", "source_sheet": "nowhere", "colors": ["Red", "Blue"],
           "sizes": [], "generate_combinations": True,
           "combinations": ["X RD", "X BE"]}
    with pytest.raises(combination_skus.CombinationError):
        combination_skus.combination_map(row, {"Red": "RD", "Blue": "NOPE"})


def test_rows_without_colours_resolve_trivially():
    resolution = resolve({"sku": "X", "colors": [], "combinations": []})
    assert resolution.ok and resolution.codes == {}


# --- tier 3, stubbed --------------------------------------------------------

def test_the_model_is_only_asked_about_what_is_left(monkeypatch):
    """Tier 3 must receive the REMAINDER, not the whole row — that constraint
    is what usually collapses the answer to a single option."""
    import app.colour.resolver as resolver

    seen = {}

    def fake_infer(colours, codes, *, scope="", examples=None):
        seen["colours"], seen["codes"] = list(colours), list(codes)
        return resolver.llm_tier.LLMResult(
            assignments={colours[0]: codes[0]}, model="stub")

    monkeypatch.setattr(resolver.llm_tier, "available", lambda: True)
    monkeypatch.setattr(resolver.llm_tier, "infer", fake_infer)

    row = {"sku": "CP-30-C100", "source_sheet": "HardHat",
           "colors": ["Yellow", "White", "Ocean Slate", "Deep Fathom"],
           "sizes": [], "generate_combinations": True,
           "combinations": ["CP-30-C100 YW", "CP-30-C100 WE",
                            "CP-30-C100 ZZQ", "CP-30-C100 QQZ"]}
    resolve(row)

    assert seen["colours"] == ["Ocean Slate", "Deep Fathom"]
    assert set(seen["codes"]) == {"ZZQ", "QQZ"}      # YW and WE already consumed


def test_an_out_of_set_code_from_the_model_is_dropped(monkeypatch):
    import app.colour.resolver as resolver

    monkeypatch.setattr(resolver.llm_tier, "available", lambda: True)
    monkeypatch.setattr(resolver.llm_tier, "infer",
                        lambda c, k, **kw: resolver.llm_tier.LLMResult(
                            assignments={c[0]: "INVENTED"}, model="stub"))

    row = {"sku": "X", "source_sheet": "nowhere",
           "colors": ["Ocean Slate", "Deep Fathom"], "sizes": [],
           "generate_combinations": True, "combinations": ["X ZZQ", "X QQZ"]}
    resolution = resolve(row)
    assert not resolution.ok           # refused, not written into the catalogue
