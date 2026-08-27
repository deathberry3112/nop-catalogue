#!/usr/bin/env python3
"""Attributes that are not colour and size.

The template names its attributes positionally: a "Product Attributes Type"
column says what an attribute is called, the column beside it holds that
attribute's values, and a "<Name> Swatch" column after that holds its swatches.
Any number of those groups, under any names — which is what lets a Sign Boards
sheet carry Sign Material and Sign Size without a line of code knowing those
words.

Colour keeps a privileged view (`colors`, `sizes`, and the `color`/`size` keys
on each variant) because it is genuinely special downstream: it is the only
attribute with swatches and the only one whose code a child SKU can encode. A
sheet with no colour attribute must not populate any of it, or the colour tiers
would engage on a product about sign materials.
"""

from __future__ import annotations

import io

import openpyxl
import pytest

import combination_skus as cs
import xlsxCreator
from app.parser import parse_workbook


def _workbook(headers: list[str], rows: list[list],
              sheet: str = "Products") -> io.BytesIO:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


#: The Sign Boards shape: two attributes, neither of them colour, no swatches.
SIGN_HEADERS = ["Product name", "SKU", "Attribute Specification Value",
                "Product Attributes Type", "Sign Material",
                "Product Attributes Type", "Sign Size",
                "Generate Combinations", "Allow Out Of Stock",
                "Child SKU Varient", "Save Product"]


def _sign(material: str, size: str, child: str) -> list:
    return ["SN-A0710", "SN-A0710", "BradySign", "Sign Material", material,
            "Sign Size", size, "Yes", "Yes", child, "Yes"]


SIGN_ROWS = [_sign("Aluminum", "7 x 10", "SN-A0710"),
             _sign("Plastic", "7 x 10", "SN-P0710"),
             _sign("Aluminum", "10 x 14", "SN-A1014"),
             _sign("Plastic", "10 x 14", "SN-P1014")]


def _one(source) -> dict:
    result = parse_workbook(source)
    rows = [r for r in result.rows if r.get("kind") != "configurator"]
    assert len(rows) == 1, [r["sku"] for r in rows]
    return rows[0]


# --- reading attributes nobody wrote code for --------------------------------

def test_a_sheet_of_attributes_nobody_named_in_code_parses():
    row = _one(_workbook(SIGN_HEADERS, SIGN_ROWS))
    assert row["errors"] == []
    assert [(a["name"], a["values"]) for a in row["attributes"]] == [
        ("Sign Material", ["Aluminum", "Plastic"]),
        ("Sign Size", ["7 x 10", "10 x 14"]),
    ]


def test_each_variant_states_every_attribute_it_names():
    row = _one(_workbook(SIGN_HEADERS, SIGN_ROWS))
    assert row["variants"][0]["values"] == {"Sign Material": "Aluminum",
                                            "Sign Size": "7 x 10"}
    assert row["variants"][0]["sku"] == "SN-A0710"


def test_a_sheet_with_no_colour_populates_no_colour_view():
    """Otherwise the colour tiers, the swatch resolver and the child-SKU
    inference would all engage on a product about sign materials."""
    row = _one(_workbook(SIGN_HEADERS, SIGN_ROWS))
    assert row["colors"] == []
    assert row["sizes"] == []
    assert all(v["color"] == "" and v["size"] is None for v in row["variants"])


def test_the_attribute_name_comes_from_the_type_column_not_the_header():
    """The header is the fallback; the cell is what the sheet actually says."""
    headers = list(SIGN_HEADERS)
    rows = [list(r) for r in SIGN_ROWS]
    for row in rows:
        row[3] = "Sign Substrate"          # the type cell disagrees with the header
    parsed = _one(_workbook(headers, rows))
    assert parsed["attributes"][0]["name"] == "Sign Substrate"


# --- any number of them ------------------------------------------------------

THREE_HEADERS = ["Product name", "SKU", "Attribute Specification Value",
                 "Product Attributes Type", "Sign Material",
                 "Product Attributes Type", "Sign Size",
                 "Product Attributes Type", "Sign Header",
                 "Generate Combinations", "Allow Out Of Stock",
                 "Child SKU Varient", "Save Product"]


def _three(material, size, header, child):
    return ["SN-3", "SN-3", "BradySign", "Sign Material", material,
            "Sign Size", size, "Sign Header", header, "Yes", "Yes", child, "Yes"]


def test_a_third_attribute_is_read_rather_than_refused():
    """The gap docs/DESIGN.md called the largest open design item."""
    rows = [_three(m, s, h, f"SN-{m[0]}{s[0]}{h[0]}")
            for m in ("Aluminum", "Plastic")
            for s in ("7 x 10", "10 x 14")
            for h in ("Danger", "Warning")]
    row = _one(_workbook(THREE_HEADERS, rows))
    assert row["errors"] == []
    assert [a["name"] for a in row["attributes"]] == [
        "Sign Material", "Sign Size", "Sign Header"]
    assert len(row["variants"]) == 8


def test_a_gap_in_a_three_dimensional_grid_is_named():
    rows = [_three(m, s, h, f"SN-{m[0]}{s[0]}{h[0]}")
            for m in ("Aluminum", "Plastic")
            for s in ("7 x 10", "10 x 14")
            for h in ("Danger", "Warning")][:-1]
    row = _one(_workbook(THREE_HEADERS, rows))
    assert any("have no child SKU" in e for e in row["errors"])
    assert any("Sign Material x Sign Size x Sign Header" in e
               for e in row["errors"])


# --- combination keying ------------------------------------------------------

def test_combinations_are_keyed_on_every_attribute():
    """Keying on two named fields would collapse five materials onto one key
    and silently lose four fifths of the product."""
    row = _one(_workbook(SIGN_HEADERS, SIGN_ROWS))
    assert cs.attribute_names(row) == ("Sign Material", "Sign Size")
    assert cs.combination_map(row) == {
        ("Aluminum", "7 x 10"): "SN-A0710",
        ("Plastic", "7 x 10"): "SN-P0710",
        ("Aluminum", "10 x 14"): "SN-A1014",
        ("Plastic", "10 x 14"): "SN-P1014",
    }


def test_the_count_check_multiplies_every_attribute():
    row = _one(_workbook(SIGN_HEADERS, SIGN_ROWS[:3]))
    with pytest.raises(cs.CombinationError) as excinfo:
        cs.combination_map(row)
    assert "2 Sign Material x 2 Sign Size" in str(excinfo.value)


def test_a_row_with_no_attributes_key_falls_back_to_colour_and_size():
    """Payloads loaded from JSON predate general attributes and must keep
    working — the runner drives them through the same code."""
    legacy = {"sku": "SKU-1", "colors": ["Red"], "sizes": ["S", "M"],
              "color_attr": "Color", "size_attr": "Size"}
    assert cs.attribute_names(legacy) == ("Color", "Size")
    assert cs.variant_key({"color": "Red", "size": "S"},
                          ("Color", "Size")) == ("Red", "S")


# --- swatches on any attribute -----------------------------------------------

SWATCHED_HEADERS = ["Product name", "SKU", "Attribute Specification Value",
                    "Product Attributes Type", "Sign Color", "Sign Color Swatch",
                    "Product Attributes Type", "Sign Size",
                    "Generate Combinations", "Allow Out Of Stock",
                    "Child SKU Varient", "Save Product"]


def test_a_non_colour_attribute_may_carry_its_own_swatch_column():
    rows = [["SN-C", "SN-C", "BradySign", "Sign Color", "Red", "Red",
             "Sign Size", "7 x 10", "Yes", "Yes", "SN-C-R", "Yes"],
            ["SN-C", "SN-C", "BradySign", "Sign Color", "Blue", "Blue",
             "Sign Size", "7 x 10", "Yes", "Yes", "SN-C-B", "Yes"]]
    row = _one(_workbook(SWATCHED_HEADERS, rows))
    assert row["errors"] == []
    assert row["attributes"][0]["swatches"] == ["Red", "Blue"]
    assert row["attributes"][1]["swatches"] == []


def test_a_swatch_column_belongs_to_the_attribute_it_follows():
    rows = [["SN-C", "SN-C", "BradySign", "Sign Color", "Red", "Red",
             "Sign Size", "7 x 10", "Yes", "Yes", "SN-C-R", "Yes"]]
    row = _one(_workbook(SWATCHED_HEADERS, rows))
    assert row["attributes"][0]["has_swatch_column"] is True
    assert row["attributes"][1]["has_swatch_column"] is False


def test_two_lines_giving_one_value_different_swatches_are_refused():
    rows = [["SN-C", "SN-C", "BradySign", "Sign Color", "Red", "Crimson",
             "Sign Size", "7 x 10", "Yes", "Yes", "SN-C-R1", "Yes"],
            ["SN-C", "SN-C", "BradySign", "Sign Color", "Red", "Scarlet",
             "Sign Size", "10 x 14", "Yes", "Yes", "SN-C-R2", "Yes"]]
    row = _one(_workbook(SWATCHED_HEADERS, rows))
    assert any("different swatches" in e for e in row["errors"])


# --- the report writes the sheet back as it found it -------------------------

def test_the_report_reproduces_the_sheets_own_columns():
    row = _one(_workbook(SIGN_HEADERS, SIGN_ROWS))
    assert xlsxCreator.template_headers(row) == SIGN_HEADERS


def test_a_swatch_column_survives_the_report(tmp_path):
    rows = [["SN-C", "SN-C", "BradySign", "Sign Color", "Red", "Red",
             "Sign Size", "7 x 10", "Yes", "Yes", "SN-C-R", "Yes"]]
    row = _one(_workbook(SWATCHED_HEADERS, rows))
    assert xlsxCreator.template_headers(row) == SWATCHED_HEADERS


def test_a_sign_board_round_trips_through_the_report(tmp_path):
    before = _one(_workbook(SIGN_HEADERS, SIGN_ROWS))
    out = tmp_path / "report.xlsx"
    xlsxCreator.write_report([before], None, out)
    after = _one(out)

    def shape(row):
        return ([(a["name"], a["values"]) for a in row["attributes"]],
                [(v["values"], v["sku"]) for v in row["variants"]])

    assert shape(after) == shape(before)


def test_a_colour_sheet_still_writes_the_original_template():
    """The clothing sheets must come back out exactly as they went in."""
    row = _one(_workbook(xlsxCreator.TEMPLATE_HEADERS, [
        ["P", "SKU-1", "Zing", "Color", "Red", "Red", "Size", "S",
         "Yes", "No", "SKU-1 RD S", "Yes"]]))
    assert xlsxCreator.template_headers(row) == xlsxCreator.TEMPLATE_HEADERS


def test_two_columns_naming_one_attribute_are_refused_by_name():
    """add_attribute is idempotent BY NAME, so both columns would map to the
    same nopCommerce attribute and pool their values into it. The symptom
    downstream ("Every Sign Size x Sign Size needs a line") describes the
    consequence; this names the cause."""
    headers = ["Product name", "SKU", "Attribute Specification Value",
               "Product Attributes Type", "Sign Size",
               "Product Attributes Type", "Sign Size",
               "Generate Combinations", "Allow Out Of Stock",
               "Child SKU Varient", "Save Product"]
    row = _one(_workbook(headers, [
        ["S", "S", "Brady", "Sign Size", "7 x 10", "Sign Size", "A",
         "Yes", "Yes", "S-1", "Yes"]]))
    assert any("attribute columns are named 'Sign Size'" in e
               for e in row["errors"])


def test_a_repeated_name_on_the_colour_template_is_still_repaired():
    """The old Color/Size sheets have a column header to fall back on, so a
    second type column repeating 'Color' is corrected rather than refused."""
    row = _one(_workbook(xlsxCreator.TEMPLATE_HEADERS, [
        ["P", "SKU-1", "Zing", "Color", "Red", "Red", "Color", "S",
         "Yes", "No", "SKU-1 RD S", "Yes"]]))
    assert [a["name"] for a in row["attributes"]] == ["Color", "Size"]
    assert row["errors"] == []
