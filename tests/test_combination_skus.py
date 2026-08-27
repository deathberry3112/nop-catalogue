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
    assert cs.combination_map(row)[("Yellow",)] == "CP-30-C100V YW"


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


# --- sizes the child SKUs abbreviate ----------------------------------------
# Only where the row has already spelled at least one size out in full. A
# trailing token is not self-evidently a size, so prefix matching one that
# nothing has established as a size is how a colour code becomes a size.

def test_an_abbreviated_size_resolves_when_the_row_spells_others_out():
    """The real CP-SV4GM row: M/L/XL are written in full, 2XL/3XL are cut to
    2X/3X, all in the same Child SKUs cell."""
    row = {"sku": "CP-SV4GM", "colors": ["Lime"],
           "sizes": ["M", "L", "XL", "2XL", "3XL"],
           "combinations": ["CP-SV4GMM", "CP-SV4GML", "CP-SV4GMXL",
                            "CP-SV4GM2X", "CP-SV4GM3X"]}
    assert cs.size_aliases(row) == {"2x": "2XL", "3x": "3XL"}


def test_the_digit_is_what_separates_2x_from_3x():
    """_letters(), which the colour tiers score with, strips non-alphabetic
    characters — it reduces 2X and 3X to 'x' and 2XL and 3XL to 'xl'. Reusing
    it here would tie on every pair; the raw prefix test must not."""
    assert cs._letters("2X") == cs._letters("3X")        # the trap
    row = {"sku": "X", "colors": ["Lime"], "sizes": ["M", "2XL", "3XL"],
           "combinations": ["X M", "X 2X", "X 3X"]}
    assert cs.size_aliases(row) == {"2x": "2XL", "3x": "3XL"}


def test_a_row_that_spells_no_size_out_is_refused():
    """Nothing establishes that the trailing token is a size at all, so these
    could equally be colour codes. Refusing is the honest answer."""
    row = {"sku": "X", "colors": ["Lime"], "sizes": ["2XL", "3XL"],
           "combinations": ["X 2X", "X 3X"]}
    assert cs.size_aliases(row) == {}
    with pytest.raises(cs.CombinationError, match="does not end in one of"):
        cs.combination_map(row)


def test_a_colour_code_is_never_aliased_onto_a_size():
    """'S' is a prefix of 'Small', but S here is Smoke Lens. No size is spelled
    out, so nothing may be aliased."""
    row = {"sku": "X", "colors": ["Smoke Lens", "Clear Lens"],
           "sizes": ["Small", "Medium"], "combinations": ["X S", "X C"]}
    assert cs.size_aliases(row) == {}


def test_an_ambiguous_abbreviation_is_refused():
    """'X' prefixes both XL and XS. A tie is not an answer."""
    row = {"sku": "Y", "colors": ["Lime"], "sizes": ["M", "XL", "XS"],
           "combinations": ["Y M", "Y X"]}
    assert cs.size_aliases(row) == {}


def test_two_tokens_competing_for_one_size_cancel():
    """Both '2' and '2X' prefix 2XL and nothing else. Assigning either would be
    decided by iteration order, which nothing in this module may depend on."""
    row = {"sku": "Y", "colors": ["Lime"], "sizes": ["M", "2XL"],
           "combinations": ["Y M", "Y 2", "Y 2X"]}
    assert cs.size_aliases(row) == {}


# --- a single colour the child SKUs do not bother writing --------------------
# The mirror of implied_size, and refused for the same reasons where it cannot
# be derived: two or more colours, or a row that writes it on some children.

def test_the_grainger_row_resolves_end_to_end():
    """Fault 1 and fault 2 on one row: no colour code anywhere, and two sizes
    abbreviated. Every child SKU is parent + size, run together."""
    row = {"sku": "CP-SV4GM", "colors": ["Lime"],
           "sizes": ["M", "L", "XL", "2XL", "3XL"],
           "combinations": ["CP-SV4GMM", "CP-SV4GML", "CP-SV4GMXL",
                            "CP-SV4GM2X", "CP-SV4GM3X"]}
    assert cs.implied_colour(row) == "Lime"
    assert cs.colour_codes(row) == {"Lime": ""}
    assert cs.combination_map(row) == {
        ("Lime", "M"): "CP-SV4GMM", ("Lime", "L"): "CP-SV4GML",
        ("Lime", "XL"): "CP-SV4GMXL", ("Lime", "2XL"): "CP-SV4GM2X",
        ("Lime", "3XL"): "CP-SV4GM3X"}


def test_a_written_colour_code_is_not_implied():
    """The existing single-colour shape must be untouched: 'X LE' has a code,
    and the SIZE is what is implied there."""
    row = {"sku": "X", "colors": ["Lime"], "sizes": ["S"], "combinations": ["X LE"]}
    assert cs.implied_colour(row) is None
    assert cs.combination_map(row) == {("Lime", "S"): "X LE"}


def test_two_colours_without_a_code_stay_unknowable():
    """The codes are the only thing telling two colours apart, so a row that
    omits them cannot be derived — only guessed at."""
    row = {"sku": "X", "colors": ["Lime", "Orange"], "sizes": ["M", "L"],
           "combinations": ["X M", "X L"]}
    assert cs.implied_colour(row) is None
    with pytest.raises(cs.CombinationError, match="has a size but no colour code"):
        cs.combination_map(row)


def test_a_half_written_colour_code_is_ambiguous_and_fails():
    """One child names its colour and one does not. Is the bare one a missing
    code, or a code that happens to look like a size?"""
    row = {"sku": "X", "colors": ["Lime"], "sizes": ["M", "L"],
           "combinations": ["X LE M", "X L"]}
    assert cs.implied_colour(row) is None
    with pytest.raises(cs.CombinationError):
        cs.combination_map(row)


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


# --- the current template: the pairing is stated, not inferred --------------

def _stated(**over) -> dict:
    row = {
        "sku": "S", "colors": ["Lime", "Orange"], "sizes": ["S", "M"],
        "combinations": ["S LE S", "S LE M", "S OE S", "S OE M"],
        "variants": [{"color": "Lime", "size": "S", "sku": "S LE S"},
                     {"color": "Lime", "size": "M", "sku": "S LE M"},
                     {"color": "Orange", "size": "S", "sku": "S OE S"},
                     {"color": "Orange", "size": "M", "sku": "S OE M"}],
    }
    row.update(over)
    return row


def test_a_stated_pairing_is_used_verbatim():
    """One line per variant means the sheet says which child SKU goes where.
    Nothing is recovered from the SKU text — note these codes (LE/OE) would
    also resolve by inference, which is exactly why the test uses codes that
    would NOT: see the next one."""
    assert cs.combination_map(_stated()) == {
        ("Lime", "S"): "S LE S", ("Lime", "M"): "S LE M",
        ("Orange", "S"): "S OE S", ("Orange", "M"): "S OE M",
    }


def test_child_skus_that_no_heuristic_could_pair_still_map():
    """The client's hard hats number their children instead of coding them —
    'CP-280-HP241R-01' says nothing about White. Under the old template that
    was unresolvable; stated, it is not a question at all."""
    row = _stated(sizes=[], colors=["White", "Red"],
                  combinations=["CP-280-01", "CP-280-15"],
                  variants=[{"color": "White", "size": None, "sku": "CP-280-01"},
                            {"color": "Red", "size": None, "sku": "CP-280-15"}])
    assert cs.combination_map(row) == {("White",): "CP-280-01",
                                       ("Red",): "CP-280-15"}


def test_a_supplied_code_map_is_ignored_when_the_sheet_stated_the_pairing():
    """resolve() returns no codes on this path, and a stale one must not be
    able to redirect a child SKU onto the wrong colour."""
    assert cs.combination_map(_stated(), {"Lime": "OE", "Orange": "LE"}) == \
           cs.combination_map(_stated())


def test_a_stated_pairing_still_answers_to_the_count_check():
    row = _stated(sizes=["S", "M", "L"])            # 2 x 3, only 4 stated
    with pytest.raises(cs.CombinationError, match="expected 6"):
        cs.combination_map(row)


def test_two_lines_claiming_one_combination_are_refused_not_collapsed():
    """Building the map without this check would drop one silently, which is
    the one thing a caller of this module may assume never happens."""
    row = _stated()
    row["variants"][1]["size"] = "S"                # now two for Lime / S
    with pytest.raises(cs.CombinationError, match="two combinations"):
        cs.combination_map(row)
