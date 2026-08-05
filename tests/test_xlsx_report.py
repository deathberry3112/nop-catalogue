#!/usr/bin/env python3
"""
The report is the only record of what a run did, so its Status column has to be
right even when the run died halfway. These tests pin down xlsxCreator's status
precedence and the row -> cells reconstruction.

No browser, no credentials, no network — this file runs on a bare `pytest`.
"""

from __future__ import annotations

import json

import openpyxl
import pytest

import main
import xlsxCreator as xc


def _row(**over) -> dict:
    """A minimal valid parsed row; override just the field under test."""
    base = {
        "sku": "SKU-1",
        "product_name": "Product One",
        "spec_value": "ZingHardHats",
        "color_attr": "Color",
        "colors": ["Yellow", "Blue"],
        "swatches": ["Yellow", "Blue"],
        "size_attr": "Size",
        "sizes": [],
        "generate_combinations": True,
        "allow_out_of_stock": False,
        "combinations": ["SKU-1 YW", "SKU-1 BE"],
        "save_product": True,
        "source_sheet": "HardHat",
        "errors": [],
    }
    base.update(over)
    return base


def _index(*entries) -> dict:
    return {e["sku"]: e for e in entries}


# --- status precedence -----------------------------------------------------
# Documented order in xlsxCreator.status_for: runner result > row errors >
# carried-in status > Pending.

def test_result_without_error_is_added():
    status, note = xc.status_for(_row(), _index({"sku": "SKU-1", "error": None}))
    assert (status, note) == (xc.STATUS_ADDED, "")


def test_result_with_error_is_failed_and_carries_the_message():
    idx = _index({"sku": "SKU-1", "error": "save button never enabled"})
    status, note = xc.status_for(_row(), idx)
    assert status == xc.STATUS_FAILED
    assert note == "save button never enabled"


def test_explicit_runner_status_wins_and_is_title_cased():
    idx = _index({"sku": "SKU-1", "status": "skipped", "error": "SKU already exists"})
    status, note = xc.status_for(_row(), idx)
    assert status == "Skipped"
    assert note == "SKU already exists"


def test_runner_result_beats_a_validation_error_on_the_row():
    """A row the runner actually reported on is described by that report, even
    if the row also carries upstream errors."""
    row = _row(errors=["no colors"])
    status, _ = xc.status_for(row, _index({"sku": "SKU-1", "error": None}))
    assert status == xc.STATUS_ADDED


def test_validation_errors_are_failed_when_the_runner_never_saw_the_row():
    status, note = xc.status_for(_row(errors=["no colors", "bad swatch"]), {})
    assert status == xc.STATUS_FAILED
    assert note == "no colors; bad swatch"


@pytest.mark.parametrize("carried", sorted(xc.DONE_STATUSES))
def test_carried_in_done_status_reports_as_added(carried):
    """A re-uploaded report's Status must survive a second run untouched."""
    status, note = xc.status_for(_row(status=carried), {})
    assert (status, note) == (xc.STATUS_ADDED, "")


def test_unreached_row_is_pending():
    status, note = xc.status_for(_row(), {})
    assert (status, note) == (xc.STATUS_PENDING, "")


def test_done_statuses_match_mains_skip_list():
    """main.py skips rows by this set and xlsxCreator reports on it. If they
    drift, a resumed batch adds a product twice."""
    assert xc.DONE_STATUSES == main.DONE_STATUSES


# --- row -> cells ----------------------------------------------------------

def test_cells_line_up_with_the_template_headers():
    assert len(xc.row_to_cells(_row())) == len(xc.TEMPLATE_HEADERS)


def test_empty_sizes_render_as_na():
    cells = xc.row_to_cells(_row(sizes=[]))
    assert cells[xc.TEMPLATE_HEADERS.index("Sizes")] == "NA"


def test_booleans_render_as_yes_no():
    cells = xc.row_to_cells(_row(generate_combinations=True, allow_out_of_stock=False))
    assert cells[8] == "Yes"
    assert cells[9] == "No"


def test_combinations_are_newline_joined_into_one_cell():
    cells = xc.row_to_cells(_row(combinations=["A", "B", "C"]))
    assert cells[10] == "A\nB\nC"


# --- the workbook itself ---------------------------------------------------

def test_report_groups_rows_by_source_sheet(tmp_path):
    rows = [
        _row(sku="H-1", source_sheet="HardHat"),
        _row(sku="V-1", source_sheet="Vest"),
        _row(sku="H-2", source_sheet="HardHat"),
    ]
    out = tmp_path / "report.xlsx"
    xc.write_report(rows, None, out)

    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["HardHat", "Vest"]          # first-seen order
    assert wb["HardHat"].max_row == 3                    # header + 2 rows
    assert wb["Vest"].max_row == 2


def test_report_appends_status_and_note_columns(tmp_path):
    out = tmp_path / "report.xlsx"
    summary = {"results": [{"sku": "SKU-1", "error": "boom"}]}
    xc.write_report([_row()], summary, out)

    ws = openpyxl.load_workbook(out).active
    header = [c.value for c in ws[1]]
    assert header[-2:] == xc.EXTRA_HEADERS
    assert ws.cell(row=2, column=len(header) - 1).value == xc.STATUS_FAILED
    assert ws.cell(row=2, column=len(header)).value == "boom"


def test_counts_cover_every_row(tmp_path):
    """The tally main.py logs has to add up to the batch, or the operator can't
    trust it."""
    rows = [_row(sku="a"), _row(sku="b", errors=["bad"]), _row(sku="c")]
    summary = {"results": [{"sku": "a", "error": None}]}
    counts = xc.write_report(rows, summary, tmp_path / "r.xlsx")

    assert sum(counts.values()) == len(rows)
    assert counts == {xc.STATUS_ADDED: 1, xc.STATUS_FAILED: 1, xc.STATUS_PENDING: 1}


def test_long_sheet_names_are_truncated_not_rejected(tmp_path):
    """openpyxl hard-caps titles at 31 chars; a long category must not crash a
    run that already touched the catalogue."""
    rows = [_row(source_sheet="A" * 60)]
    xc.write_report(rows, None, tmp_path / "r.xlsx")
    assert openpyxl.load_workbook(tmp_path / "r.xlsx").sheetnames == ["A" * 31]


# --- the payload contract main.py enforces ---------------------------------

def test_load_rows_accepts_the_parser_report_shape(tmp_path):
    p = tmp_path / "rows.json"
    p.write_text(json.dumps({"total_rows": 1, "rows": [_row()]}), encoding="utf-8")
    assert len(main.load_rows(str(p))) == 1


def test_load_rows_accepts_a_bare_list(tmp_path):
    p = tmp_path / "rows.json"
    p.write_text(json.dumps([_row()]), encoding="utf-8")
    assert len(main.load_rows(str(p))) == 1


def test_load_rows_rejects_a_row_with_no_sku(tmp_path):
    p = tmp_path / "rows.json"
    p.write_text(json.dumps([_row(sku="")]), encoding="utf-8")
    with pytest.raises(ValueError, match="missing an 'sku'"):
        main.load_rows(str(p))


def test_checked_in_rows_are_loadable(rows):
    """Guards the fixture the live tests are built on (conftest `rows`)."""
    assert len(rows) == 7
    assert all(r["sku"] for r in rows)
