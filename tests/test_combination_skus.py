#!/usr/bin/env python3
"""combination_skus against the real payload. No browser, no credentials."""

from __future__ import annotations

import pytest

import combination_skus as cs
import main


@pytest.fixture(scope="module")
def payload() -> list[dict]:
    return main.load_rows(main.DEFAULT_PAYLOAD)


def test_every_payload_row_resolves(payload):
    """The whole point: all 7 rows map cleanly, with no overrides."""
    assert cs.OVERRIDES == {}, "these rows should not need any override"
    for row in payload:
        mapping = cs.combination_map(row)
        expected = len(row["colors"]) * max(1, len(row["sizes"]))
        assert len(mapping) == expected, f"{row['sku']}: {mapping}"
        assert set(mapping.values()) == set(row["combinations"])


def test_the_hard_hat_codes(payload):
    row = next(r for r in payload if r["sku"] == "CP-30-C100")
    assert cs.colour_codes(row) == {
        "Yellow": "YW", "White": "WE", "Blue": "BE",
        "Hi-Vis Orange": "HOE", "Hi-Vis Lime": "HLE",
        "Green": "GN", "Pink": "PK", "Red": "RD", "Grey": "GY",
    }


def test_grey_and_green_are_not_confused(payload):
    """Both start with G and both are four/five letters; the last letter is the
    only thing separating GY from GN."""
    row = next(r for r in payload if r["sku"] == "CP-30-C100")
    codes = cs.colour_codes(row)
    assert codes["Grey"] == "GY"
    assert codes["Green"] == "GN"


def test_the_vest_rows_drop_the_qualifier(payload):
    """"Hi-Viz Lime" is LE here, but "Hi-Vis Lime" is HLE on the hard hats.
    This is the case that rules out computing the code from the name."""
    row = next(r for r in payload if r["sku"] == "CP-V-010")
    assert cs.colour_codes(row) == {"Hi-Viz Lime": "LE", "Hi-Viz Orange": "OE"}

    hats = next(r for r in payload if r["sku"] == "CP-30-C100V")
    assert cs.colour_codes(hats)["Hi-Vis Lime"] == "HLE"


def test_sizes_are_split_off(payload):
    row = next(r for r in payload if r["sku"] == "CP-V-200")
    mapping = cs.combination_map(row)
    assert mapping[("Lime", "2XL")] == "CP-V-200 LE 2XL"
    assert mapping[("Orange", "S")] == "CP-V-200 OE S"
    assert len(mapping) == 16                       # 2 colours x 8 sizes


def test_sizeless_rows_key_on_none(payload):
    row = next(r for r in payload if r["sku"] == "CP-30-C100V")
    assert cs.combination_map(row)[("Yellow", None)] == "CP-30-C100V YW"


def test_split_combination():
    assert cs.split_combination("CP-V-200 LE 2XL", "CP-V-200", ["2XL"]) == ("LE", "2XL")
    assert cs.split_combination("CP-30-C100 YW", "CP-30-C100", []) == ("YW", None)


# --- recovering the base when the parent SKU is mistyped --------------------
# This is what lets app.parser treat the mismatch as a warning: the row still
# resolves to the right codes instead of failing halfway through the run.

def test_the_sku_is_the_base_when_it_matches():
    row = {"sku": "CP-30-C100", "combinations": ["CP-30-C100 YW", "CP-30-C100 WE"]}
    assert cs.base_prefix(row) == "CP-30-C100"


def test_a_mistyped_sku_falls_back_to_what_the_children_agree_on():
    """The real 20-100ECO row: the sheet dropped the CP- family prefix."""
    row = {"sku": "20-100ECO", "colors": ["Clear Lens", "Smoke Lens"], "sizes": [],
           "combinations": ["CP-20-100ECO C", "CP-20-100ECO CAF",
                            "CP-20-100ECO S", "CP-20-100ECO SAF"]}
    assert cs.base_prefix(row) == "CP-20-100ECO"
    codes = [cs.split_combination(c, cs.base_prefix(row), [])[0]
             for c in row["combinations"]]
    assert codes == ["C", "CAF", "S", "SAF"]


def test_the_base_never_eats_part_of_a_code():
    """THE risk of inferring the base. C/CAF/S/SAF share leading letters, so a
    character-wise common prefix would leave '', 'AF', '', 'AF' — a plausible
    mapping onto the wrong colours, which is worse than any hard failure."""
    row = {"sku": "WRONG",
           "combinations": ["CP-9 C", "CP-9 CAF", "CP-9 CX"]}
    assert cs.base_prefix(row) == "CP-9"
    codes = [cs.split_combination(c, "CP-9", [])[0] for c in row["combinations"]]
    assert codes == ["C", "CAF", "CX"]


def test_a_single_combination_teaches_nothing_and_keeps_the_sku():
    """One string agrees with itself perfectly; stripping all of it would leave
    no code at all. Nothing to infer, so the SKU stands and the row fails
    honestly rather than on a guess."""
    row = {"sku": "WRONG", "combinations": ["CP-9 C"]}
    assert cs.base_prefix(row) == "WRONG"


def test_combinations_that_agree_on_nothing_keep_the_sku():
    row = {"sku": "WRONG", "combinations": ["AAA C", "BBB S"]}
    assert cs.base_prefix(row) == "WRONG"


# --- a single size the child SKUs do not bother writing ---------------------
# Decided per ROW. Judging each SKU alone would let a half-written row resolve
# half one way and half the other, which is a wrong mapping that looks fine.

def test_one_size_that_nobody_writes_is_implied():
    """The real Eyewear shape: Sizes says OS, the children never repeat it."""
    row = {"sku": "CP-20-100", "sizes": ["OS"], "colors": ["Amber", "Clear"],
           "combinations": ["CP-20-100 A", "CP-20-100 C"]}
    assert cs.implied_size(row) == "OS"
    assert cs.split_combination("CP-20-100 A", "CP-20-100", ["OS"],
                                implied="OS") == ("A", "OS")
    assert cs.combination_map(row) == {("Amber", "OS"): "CP-20-100 A",
                                       ("Clear", "OS"): "CP-20-100 C"}


def test_a_written_size_is_still_stripped():
    """The Vest shape must be untouched by any of this."""
    row = {"sku": "CP-V-010", "sizes": ["OS"],
           "combinations": ["CP-V-010 LE OS", "CP-V-010 OE OS"]}
    assert cs.implied_size(row) is None          # they carry it; nothing implied
    assert cs.split_combination("CP-V-010 LE OS", "CP-V-010", ["OS"]) == ("LE", "OS")


def test_two_sizes_without_a_suffix_is_unknowable_and_fails():
    """8 colours x 2 sizes needs 16 distinct SKUs. With no size written there
    is no way to tell which is which, and inventing one is how you get a
    catalogue that looks right and is wrong."""
    row = {"sku": "X", "sizes": ["S", "M"], "colors": ["Red"],
           "combinations": ["X RD", "X BE"]}
    assert cs.implied_size(row) is None
    with pytest.raises(cs.CombinationError, match="does not end in one of"):
        cs.combination_map(row)


def test_a_half_written_row_is_ambiguous_and_fails():
    """Some carry the size, some do not. Is 'X RD' a missing size, or a colour
    code that happens to look like one? Unknowable — so it must not resolve."""
    row = {"sku": "X", "sizes": ["OS"], "colors": ["Red", "Blue"],
           "combinations": ["X RD OS", "X BE"]}
    assert cs.implied_size(row) is None
    with pytest.raises(cs.CombinationError, match="does not end in one of"):
        cs.combination_map(row)


def test_a_sizeless_row_is_unaffected():
    row = {"sku": "CP-30-C100V", "sizes": [], "combinations": ["CP-30-C100V YW"]}
    assert cs.implied_size(row) is None
    assert cs.split_combination("CP-30-C100V YW", "CP-30-C100V", []) == ("YW", None)


# --- the failure modes, which matter more than the happy path ---------------

def test_an_unrecognisable_code_is_refused():
    row = {"sku": "X", "colors": ["Yellow"], "sizes": [], "combinations": ["X QQ"]}
    with pytest.raises(cs.CombinationError, match="looks like 'Yellow'"):
        cs.colour_codes(row)


def test_a_count_mismatch_is_refused():
    """Three colours, two codes: something is missing and we must not guess."""
    row = {"sku": "X", "colors": ["Yellow", "White", "Blue"], "sizes": [],
           "combinations": ["X YW", "X WE"]}
    with pytest.raises(cs.CombinationError, match="3 colour"):
        cs.colour_codes(row)


def test_a_lone_size_left_off_the_child_sku_is_inferred_not_refused():
    """This used to raise, and the change is deliberate: with exactly one size
    the pairing is forced, so refusing was refusing arithmetic. Where a missing
    size genuinely cannot be recovered — two or more sizes, or a row that
    writes it on some children and not others — it still raises; see
    test_two_sizes_without_a_suffix_is_unknowable_and_fails and
    test_a_half_written_row_is_ambiguous_and_fails."""
    row = {"sku": "X", "colors": ["Lime"], "sizes": ["S"], "combinations": ["X LE"]}
    assert cs.combination_map(row) == {("Lime", "S"): "X LE"}


def test_a_tie_is_refused(monkeypatch):
    """Two colours that no signal can separate must fail, not coin-flip."""
    row = {"sku": "X", "colors": ["Ab", "Ab"], "sizes": [],
           "combinations": ["X AB", "X AB2"]}
    with pytest.raises(cs.CombinationError):
        cs.colour_codes(row)


def test_an_override_wins():
    row = {"sku": "X", "colors": ["Chartreuse"], "sizes": [], "combinations": ["X ZZ"]}
    with pytest.raises(cs.CombinationError):
        cs.colour_codes(row)                        # ZZ is not derivable

    cs.OVERRIDES["Chartreuse"] = "ZZ"
    try:
        assert cs.colour_codes(row) == {"Chartreuse": "ZZ"}
    finally:
        del cs.OVERRIDES["Chartreuse"]
