#!/usr/bin/env python3
"""Which swatch a colour name means — app/colour/swatch.py.

The interesting cases are all about what counts as an answer. This admin offers
~2,100 swatches, several of which share a name: same-named swatches are
interchangeable and the first one is taken, but a colour no swatch is actually
named after gets none — a wrong swatch is invisible until a customer sees it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.colour import swatch as sw

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_the_map(tmp_path, monkeypatch):
    """SEED only — no local file, no remote store. Without this the tests read
    whatever the developer's var/ happens to hold and pass or fail by accident."""
    monkeypatch.setattr(sw, "LOCAL_PATH", tmp_path / "colour-swatches.json")
    monkeypatch.setattr(sw.MAP, "_entries", None)
    monkeypatch.setattr(sw.SwatchMap, "_load_remote", lambda self: [])
    yield
    sw.MAP.refresh()


#: A faithful slice of the real dropdown: duplicate labels included, because
#: they are the whole problem.
OPTIONS = [
    ("18", "White"), ("19", "Blue"), ("20", "Red"), ("22", "Green"),
    ("23", "Lime"), ("24", "Orange"), ("25", "Yellow"),
    ("33", "Blue"), ("34", "Red"), ("36", "Green"), ("37", "Lime"),
    ("38", "Orange"), ("39", "Yellow"), ("40", "Black"),
    ("2051", "Grey"), ("2056", "Pink"), ("2064", "Silver"),
    ("2071", "DSHigh-Vis Green"), ("2072", "DSHigh-Vis Orange"),
    ("2068", "DSBlack"), ("2124", "Propper Black"),
]


# --- the map -----------------------------------------------------------------

def test_a_duplicated_label_is_settled_by_the_map_not_by_order():
    """"Blue" is both 19 and 33. The catalogue uses 19 x29 and 33 never, which
    is why that is seeded — but the point is that it comes from the map, not
    from 19 happening to be first in the list."""
    choice = sw.resolve_swatches(["Blue"], OPTIONS)["Blue"]
    assert choice.swatch_id == "19"
    assert choice.tier is sw.Tier.MAP


def test_a_scoped_entry_beats_the_global_one():
    hard_hat = sw.resolve_swatches(["Hi-Vis Lime"], OPTIONS, scope="HardHat")
    assert hard_hat["Hi-Vis Lime"].swatch_id == "2071"
    assert hard_hat["Hi-Vis Lime"].swatch_label == "DSHigh-Vis Green"


def test_the_name_is_not_a_guide_and_the_seed_proves_it():
    """Hi-Vis Lime is filed under a swatch named Green. No string-similarity
    rule would ever produce that, which is the reason this is a map."""
    choice = sw.resolve_swatches(["Hi-Vis Lime"], OPTIONS, scope="HardHat")["Hi-Vis Lime"]
    assert "Lime" not in choice.swatch_label


def test_an_unscoped_row_still_resolves_the_plain_colours():
    got = sw.resolve_swatches(["Yellow", "Red", "Black"], OPTIONS)
    assert {c: v.swatch_id for c, v in got.items()} == {
        "Yellow": "25", "Red": "20", "Black": "40"}


# --- tier 2: exactly one swatch carries the name ------------------------------

def test_a_uniquely_named_swatch_resolves_without_a_map_entry():
    choice = sw.resolve_swatches(["Pink"], OPTIONS)["Pink"]
    assert choice.swatch_id == "2056"
    assert choice.tier is sw.Tier.LABEL


def test_the_label_tier_folds_case_and_spacing():
    assert sw.resolve_swatches(["  pink "], OPTIONS)["  pink "].swatch_id == "2056"


# --- refusing to answer -------------------------------------------------------

def test_two_swatches_with_the_same_name_take_the_first():
    """Purple is not seeded, and the dropdown offers two of them. They are
    indistinguishable on the page, so the first one is the answer — the reason
    still names both, so a human who cares can seed the other."""
    options = OPTIONS + [("21", "Purple"), ("35", "Purple")]
    choice = sw.resolve_swatches(["Purple"], options)["Purple"]
    assert choice.swatch_id == "21"
    assert choice.tier is sw.Tier.LABEL
    assert "21" in choice.reason and "35" in choice.reason


def test_the_first_of_several_still_has_to_be_an_exact_match():
    """Taking the first is not permission to take a near one. "Royal Blue" is
    named by no swatch, and "Blue" is not close enough to be it."""
    options = OPTIONS + [("41", "Royal Blue Metallic")]
    choice = sw.resolve_swatches(["Royal Blue"], options)["Royal Blue"]
    assert not choice.ok
    assert choice.tier is sw.Tier.UNRESOLVED


def test_duplicates_of_an_exact_match_take_the_first_of_those():
    options = OPTIONS + [("42", "Royal Blue"), ("43", "royal  blue"),
                         ("44", "Royal Blue Metallic")]
    choice = sw.resolve_swatches(["Royal Blue"], options)["Royal Blue"]
    assert choice.swatch_id == "42"
    assert choice.tier is sw.Tier.LABEL


def test_a_colour_no_swatch_is_named_after_is_refused():
    choice = sw.resolve_swatches(["Hi-Viz Lime"], OPTIONS, scope="Vest")["Hi-Viz Lime"]
    assert not choice.ok
    assert "Hi-Viz Lime" in choice.reason


def test_an_unresolved_colour_never_raises():
    """A missing swatch must not fail a product that is otherwise correct."""
    got = sw.resolve_swatches(["Yellow", "Nonsense Colour"], OPTIONS)
    assert got["Yellow"].ok
    assert not got["Nonsense Colour"].ok


# --- the map is a claim about THIS admin, and is checked ----------------------

def test_a_map_entry_pointing_at_a_missing_id_is_refused():
    """The seed says Yellow is 25. An admin that has no 25 gets nothing, not
    an id it cannot honour."""
    without_25 = [o for o in OPTIONS if o[0] != "25"]
    choice = sw.resolve_swatches(["Yellow"], without_25)["Yellow"]
    assert not choice.ok
    assert "does not offer" in choice.reason


def test_a_map_entry_whose_id_now_means_something_else_is_refused():
    """Swatch ids are per-instance keys. If 25 is 'Fuchsia' here, the entry is
    stale — and filing a hard hat under Fuchsia silently is the exact failure
    the stored label exists to prevent."""
    moved = [("25", "Fuchsia") if o[0] == "25" else o for o in OPTIONS]
    choice = sw.resolve_swatches(["Yellow"], moved)["Yellow"]
    assert not choice.ok
    assert "out of date" in choice.reason


def test_an_empty_dropdown_resolves_nothing_and_does_not_crash():
    got = sw.resolve_swatches(["Yellow", "Pink"], [])
    assert all(not c.ok for c in got.values())


# --- the sheet decides: swatches_for_row --------------------------------------

def test_an_empty_swatch_column_sets_nothing_and_says_nothing():
    """The safetyglasses workbook leaves Color Swatch blank. That is a
    legitimate way to fill the sheet — no swatches, and no complaint about it.
    Inferring one from the colour name would be inventing data."""
    row = {"colors": ["Yellow", "White"], "swatches": [], "source_sheet": "X"}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {}
    assert problems == []


def test_a_missing_swatch_key_behaves_the_same_as_an_empty_one():
    swatches, problems, _out = sw.swatches_for_row({"colors": ["Yellow"]}, OPTIONS)
    assert swatches == {} and problems == []


def test_the_column_is_what_is_looked_up_not_the_colour():
    """Colour and swatch are separate columns and need not agree. If the sheet
    pairs the colour 'Sunshine' with the swatch 'Yellow', the swatch is what
    gets resolved — and the result is keyed by the colour."""
    row = {"colors": ["Sunshine"], "swatches": ["Yellow"]}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"Sunshine": "25"}
    assert problems == []


def test_colours_and_swatches_pair_by_position():
    row = {"colors": ["A", "B"], "swatches": ["Yellow", "Blue"]}
    swatches, _, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"A": "25", "B": "19"}


def test_a_count_mismatch_falls_back_to_matching_on_name():
    """CP-30-F100V lists 5 colours and 9 swatches, and its colours appear
    verbatim among them. Position is meaningless here, but an entry spelled
    exactly like the colour still pairs with it unambiguously."""
    row = {"colors": ["Yellow", "Blue"],
           "swatches": ["Yellow", "White", "Blue", "Red", "Green"]}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"Yellow": "25", "Blue": "19"}
    assert problems == []


def test_a_mismatch_never_pairs_by_position():
    """The danger a mismatch creates: assuming the extras are at the end. Here
    'Teal' is not in the column at all, so it gets nothing — rather than
    silently taking whichever entry happens to sit at its index."""
    row = {"colors": ["Teal", "Blue"],
           "swatches": ["Yellow", "White", "Blue", "Red", "Green"]}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"Blue": "19"}
    assert len(problems) == 1
    assert "Teal" in problems[0] and "cannot be matched up in order" in problems[0]


def test_the_name_fallback_ignores_order_entirely():
    row = {"colors": ["Blue", "Yellow"],
           "swatches": ["Grey", "Yellow", "Red", "Blue"]}
    swatches, _, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"Blue": "19", "Yellow": "25"}


def test_equal_lengths_still_pair_by_position_not_by_name():
    """Position must keep winning when the counts agree, or a colour could
    never carry a differently-named swatch."""
    row = {"colors": ["Sunshine", "Sky"], "swatches": ["Yellow", "Blue"]}
    swatches, _, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"Sunshine": "25", "Sky": "19"}


def test_an_unresolvable_entry_names_the_colour_and_what_the_sheet_said():
    row = {"colors": ["Lime Vest"], "swatches": ["Hi-Viz Lime"], "source_sheet": "Vest"}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {}
    assert "Lime Vest" in problems[0] and "Hi-Viz Lime" in problems[0]


def test_a_blank_entry_among_real_ones_is_skipped_quietly():
    """One colour left blank in the column is a choice about that colour, not
    an error — the others still get theirs."""
    row = {"colors": ["A", "B"], "swatches": ["Yellow", "  "]}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"A": "25"}
    assert problems == []


def test_the_hard_hat_sheet_resolves_through_the_column():
    """End to end on the shipped payload: the column says 'Hi-Vis Lime', the
    map turns that into the swatch this admin calls 'DSHigh-Vis Green'."""
    row = {"colors": ["Hi-Vis Lime", "Yellow"],
           "swatches": ["Hi-Vis Lime", "Yellow"], "source_sheet": "HardHat"}
    swatches, problems, _out = sw.swatches_for_row(row, OPTIONS)
    assert swatches == {"Hi-Vis Lime": "2071", "Yellow": "25"}
    assert problems == []


# --- the seed matches what the catalogue actually does ------------------------

def test_the_seed_only_uses_ids_the_client_actually_uses():
    """Measured across the 204 hand-configured mappings on the instance: where
    two swatches share a name the client uses the lower id, emphatically
    (lime 23 x113 vs 37 never). Guard the seed against a well-meaning edit."""
    assert sw.SEED[sw.GLOBAL]["lime"] == ("23", "Lime")
    assert sw.SEED[sw.GLOBAL]["orange"] == ("24", "Orange")
    assert sw.SEED[sw.GLOBAL]["blue"] == ("19", "Blue")


def test_every_seeded_colour_key_is_normalised():
    """A key that is not normalised can never be looked up — lookup()
    normalises the query, so 'Hi-Vis Lime' as a key would be dead weight."""
    for scope, entries in sw.SEED.items():
        for key in entries:
            assert key == sw.normalise(key), f"{scope}/{key} is not normalised"


def test_the_payload_colours_resolve_or_say_why(tmp_path):
    """The shipped payload, end to end. Not an assertion that everything
    resolves — the vest colours deliberately do not — but that every colour
    gets either a swatch or a reason, and never silence."""
    rows = json.loads((REPO_ROOT / "parsed_rows.json").read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else rows["rows"]
    seen = 0
    for row in rows:
        for colour, choice in sw.resolve_swatches(
                row.get("colors") or [], OPTIONS,
                scope=row.get("source_sheet")).items():
            seen += 1
            assert choice.ok or choice.reason, f"{colour} resolved to nothing at all"
    assert seen > 0


# --- what the console is told -------------------------------------------------
#
# The third return value. Its job is the question the first two could not
# answer: not "what went wrong" but "what happened to each colour" — a run that
# set every swatch and a run that set none used to report the same silence.

def test_every_colour_the_sheet_asked_about_comes_back_named():
    row = {"colors": ["Yellow", "Teal"], "swatches": ["Yellow", "Teal"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert [o["colour"] for o in outcomes] == ["Yellow", "Teal"]
    assert outcomes[0]["swatch_id"] == "25"
    assert outcomes[0]["swatch_label"] == "Yellow"
    assert outcomes[1]["swatch_id"] is None


def test_a_colour_the_sheet_did_not_ask_about_is_absent_not_zero():
    """Silence is the point. A line per unasked colour would bury the one that
    needs a person under four hundred that do not."""
    row = {"colors": ["Yellow", "Blue"], "swatches": ["Yellow", ""]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert [o["colour"] for o in outcomes] == ["Yellow"]


def test_a_row_with_no_swatch_column_reports_nothing_at_all():
    assert sw.swatches_for_row({"colors": ["Yellow"]}, OPTIONS) == ({}, [], [])


def test_an_outcome_carries_the_cause_as_a_slug_for_grouping():
    """The console groups a 200-row run by cause, and prose does not group."""
    row = {"colors": ["Teal"], "swatches": ["Teal"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert outcomes[0]["cause"] == "unnamed"
    assert outcomes[0]["cause"] in sw.CAUSE


def test_a_duplicate_pick_says_which_ids_it_beat():
    """Resolved, but by a coin flip — and that is worth showing somebody even
    though nothing failed. The alternatives are what makes it seedable."""
    options = OPTIONS + [("21", "Purple"), ("35", "Purple")]
    row = {"colors": ["Purple"], "swatches": ["Purple"]}
    swatches, problems, outcomes = sw.swatches_for_row(row, options)
    assert swatches == {"Purple": "21"} and problems == []
    assert outcomes[0]["cause"] == "duplicate"
    assert outcomes[0]["alternatives"] == ["35"]


def test_a_single_named_swatch_has_no_cause_and_no_alternatives():
    row = {"colors": ["Pink"], "swatches": ["Pink"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert outcomes[0]["cause"] == "" and outcomes[0]["alternatives"] == []


def test_a_colour_carrying_a_differently_named_swatch_keeps_both_names():
    row = {"colors": ["Sunshine"], "swatches": ["Yellow"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert outcomes[0]["colour"] == "Sunshine"
    assert outcomes[0]["asked"] == "Yellow"


def test_an_unpairable_colour_is_reported_with_its_own_cause():
    row = {"colors": ["Teal", "Blue"],
           "swatches": ["Yellow", "White", "Blue", "Red", "Green"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    teal = [o for o in outcomes if o["colour"] == "Teal"][0]
    assert teal["cause"] == "unpaired"
    assert "cannot be matched up in order" in teal["note"]


def test_a_stale_map_entry_is_a_cause_of_its_own():
    """25 is seeded as 'Yellow'. On an admin where it is something else the
    entry is stale, and that is a different problem from an unknown colour."""
    options = [("25", "Chartreuse")]
    row = {"colors": ["Yellow"], "swatches": ["Yellow"]}
    _, _, outcomes = sw.swatches_for_row(row, options)
    assert outcomes[0]["cause"] == "map_stale"


def test_an_outcome_starts_out_saying_what_was_intended():
    """State is filled in by the runner once the admin has had its say. Until
    then a resolved colour is 'planned' — which is what a step that died
    half-way leaves behind, and it must not read as 'done'."""
    row = {"colors": ["Yellow", "Teal"], "swatches": ["Yellow", "Teal"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert [o["state"] for o in outcomes] == ["planned", "skipped"]


def test_an_outcome_is_json_and_stays_json():
    """It goes into row_steps.detail and comes back out of Postgres, so its
    shape is a wire format, not an internal convenience."""
    row = {"colors": ["Yellow"], "swatches": ["Yellow"]}
    _, _, outcomes = sw.swatches_for_row(row, OPTIONS)
    assert json.loads(json.dumps(outcomes)) == outcomes
