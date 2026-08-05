#!/usr/bin/env python3
"""The workbook parser — and specifically what it refuses to do.

The round-trip tests matter because parser.py and xlsxCreator.py are mirrors of
each other: one reads the 12 template columns, the other writes them. A change
to one that is not made to the other shows up here rather than in production.
"""

from __future__ import annotations

import io

import openpyxl
import pytest

import xlsxCreator
from app.parser import parse_workbook

HEADERS = xlsxCreator.TEMPLATE_HEADERS


def _workbook(rows: list[list], headers: list[str] | None = None,
              sheet: str = "Products") -> io.BytesIO:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers or HEADERS)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _row(sku="SKU-1", colors="Red, Blue", sizes="NA", gen="Yes",
         combos="SKU-1 RD\nSKU-1 BE") -> list:
    return ["Product One", sku, "Zing", "Color", colors, colors,
            "Size", sizes, gen, "No", combos, "Yes"]


# --- the happy path ---------------------------------------------------------

def test_a_clean_row_parses():
    result = parse_workbook(_workbook([_row()]))
    assert result.valid_rows == 1
    row = result.rows[0]
    assert row["sku"] == "SKU-1"
    assert row["colors"] == ["Red", "Blue"]
    assert row["combinations"] == ["SKU-1 RD", "SKU-1 BE"]
    assert row["generate_combinations"] is True
    assert row["errors"] == []


def test_sizes_na_means_no_sizes():
    result = parse_workbook(_workbook([_row(sizes="NA")]))
    assert result.rows[0]["sizes"] == []


def test_every_sheet_is_read_and_attributed():
    wb = openpyxl.Workbook()
    for name in ("HardHat", "Vest"):
        ws = wb.create_sheet(name)
        ws.append(HEADERS)
        ws.append(_row(sku=f"{name}-1", combos=f"{name}-1 RD\n{name}-1 BE"))
    wb.remove(wb.active)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    result = parse_workbook(buf)
    assert result.sheets == ["HardHat", "Vest"]
    assert {r["source_sheet"] for r in result.rows} == {"HardHat", "Vest"}


def test_blank_lines_are_ignored():
    result = parse_workbook(_workbook([_row(), [None] * 12, [""] * 12]))
    assert len(result.rows) == 1


# --- the refusals: each of these would otherwise be a silent wrong result ----

def test_an_unrecognised_column_blocks_the_row():
    """The important one. An unknown column is very likely an attribute this
    pipeline cannot model, and creating the product without it yields a wrong
    catalogue and a clean report — the worst outcome available."""
    result = parse_workbook(_workbook([_row() + ["Leather"]],
                                      headers=HEADERS + ["Material"]))
    assert result.rows[0]["errors"], "an unknown column must block the row"
    assert "Material" in result.rows[0]["errors"][0]


def test_a_missing_sku_is_an_error():
    result = parse_workbook(_workbook([_row(sku="")]))
    assert any("SKU is required" in e for e in result.rows[0]["errors"])


def test_a_duplicate_sku_is_flagged_on_both_rows():
    result = parse_workbook(_workbook([_row(), _row()]))
    assert all(any("more than once" in e for e in r["errors"]) for r in result.rows)


def test_a_combination_count_mismatch_is_refused():
    result = parse_workbook(_workbook([_row(combos="SKU-1 RD")]))   # 1 for 2 colours
    assert any("expected 2" in e for e in result.rows[0]["errors"])


def test_a_child_sku_not_starting_with_the_sku_warns_but_still_runs():
    """Downgraded from an error on purpose. It is nearly always one mistyped
    parent SKU, the children are written verbatim either way, and blocking the
    row also blocked every other row in the workbook from committing."""
    result = parse_workbook(_workbook([_row(combos="SKU-1 RD\nOTHER BE")]))
    row = result.rows[0]
    assert row["errors"] == []
    assert result.valid_rows == 1
    assert any("do not start with" in w for w in row["warnings"])


def test_the_warning_names_the_offending_child_skus():
    result = parse_workbook(_workbook([_row(combos="SKU-1 RD\nOTHER BE")]))
    warning = next(w for w in result.rows[0]["warnings"] if "do not start with" in w)
    assert "'OTHER BE'" in warning and "'SKU-1'" in warning
    assert "SKU-1 RD" not in warning        # that one is fine; don't cry wolf


def test_duplicates_are_reported_for_a_human_to_decide():
    """Not removed here. Dropping a repeated entry is probably right and only
    probably — if it was a variant somebody forgot to rename, removing it
    deletes a product silently. The console offers the fix; a person takes it."""
    result = parse_workbook(_workbook([_row(
        colors="Red, Blue, Red", combos="SKU-1 RD\nSKU-1 BE\nSKU-1 RD")]))
    row = result.rows[0]
    assert row["errors"] == []                     # a decision, not a blocker
    assert row["duplicates"] == {"colors": ["Red"], "combinations": ["SKU-1 RD"]}
    assert any(w.startswith("duplicated:") for w in row["warnings"])


def test_a_clean_row_carries_no_duplicate_payload():
    assert parse_workbook(_workbook([_row()])).rows[0]["duplicates"] == {}


def test_a_size_column_repeating_the_colour_attribute_is_corrected():
    """Both attribute columns saying "Color" would make add_attribute hand back
    the colour attribute for the size pass, filing "OS" as a ninth colour."""
    row = _row(sizes="OS")
    row[6] = "Color"                               # the second attribute column
    result = parse_workbook(_workbook([row]))
    assert result.rows[0]["size_attr"] == "Size"
    assert result.rows[0]["warnings"]

    # said once for the sheet, in the words that appear in the spreadsheet
    assert len(result.sheet_warnings) == 1
    note = result.sheet_warnings[0]
    assert "Product Attributes Type" in note
    assert "'Size'" in note


def test_a_sheet_with_only_one_attribute_column_is_told_to_add_the_second():
    headers = [h for h in HEADERS]
    del headers[6]                                 # drop the size attribute column
    row = _row(sizes="OS")
    del row[6]
    result = parse_workbook(_workbook([row], headers=headers))
    assert result.rows[0]["size_attr"] == "Size"
    note = result.sheet_warnings[0]
    assert "only ONE 'Product Attributes Type'" in note
    assert "before the Sizes column" in note


def test_a_sheet_without_sizes_is_not_nagged_about_a_size_attribute():
    """HardHat sets Sizes = NA. There is no size attribute to name, so there is
    nothing to fix and nothing to say."""
    headers = [h for h in HEADERS]
    del headers[6]
    row = _row(sizes="NA")
    del row[6]
    result = parse_workbook(_workbook([row], headers=headers))
    assert result.sheet_warnings == []


def test_a_normal_size_attribute_is_left_alone():
    result = parse_workbook(_workbook([_row(sizes="OS")]))
    assert result.rows[0]["size_attr"] == "Size"
    assert result.rows[0]["warnings"] == []
    assert result.sheet_warnings == []


def test_generate_without_colours_is_refused():
    result = parse_workbook(_workbook([_row(colors="", combos="")]))
    assert any("no colours" in e for e in result.rows[0]["errors"])


def test_a_sheet_without_a_sku_column_is_reported_not_ignored():
    result = parse_workbook(_workbook([["a", "b"]], headers=["Product name", "Note"]))
    assert result.errors and "SKU" in result.errors[0]


# --- the round trip ---------------------------------------------------------

def test_a_written_report_parses_back(tmp_path):
    """xlsxCreator writes it, the parser reads it, and the fields survive.
    This is what lets a user re-upload a report to finish a partial batch."""
    original = {
        "sku": "CP-V-200", "product_name": "Vest", "spec_value": "ZingHighViz",
        "color_attr": "Color", "colors": ["Lime", "Orange"],
        "swatches": ["Lime", "Orange"], "size_attr": "Size",
        "sizes": ["S", "M"], "generate_combinations": True,
        "allow_out_of_stock": True, "save_product": True,
        "combinations": ["CP-V-200 LE S", "CP-V-200 LE M",
                         "CP-V-200 OE S", "CP-V-200 OE M"],
        "source_sheet": "Vest", "errors": [],
    }
    path = tmp_path / "report.xlsx"
    xlsxCreator.write_report([original], None, path)

    parsed = parse_workbook(path).rows[0]
    for field in ("sku", "product_name", "spec_value", "colors", "sizes",
                  "combinations", "generate_combinations", "allow_out_of_stock"):
        assert parsed[field] == original[field], field
    assert parsed["source_sheet"] == "Vest"


def test_a_carried_status_survives_so_a_rerun_can_skip_the_row():
    result = parse_workbook(_workbook(
        [_row() + ["Added", ""]], headers=HEADERS + ["Status", "Note"]))
    assert result.rows[0]["status"] == "Added"
    import main
    assert main._already_done(result.rows[0])
