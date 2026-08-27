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
        "variants": [{"color": "Yellow", "size": None, "sku": "SKU-1 YW"},
                     {"color": "Blue", "size": None, "sku": "SKU-1 BE"}],
        "save_product": True,
        "source_sheet": "HardHat",
        "errors": [],
    }
    base.update(over)
    return base


def _index(*entries) -> dict:
    """Results keyed the way write_report keys them — by (kind, SKU), because a
    phase-1 product row and a phase-2 configurator row may share a SKU."""
    return {xc._result_key(e): e for e in entries}


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
    assert all(len(line) == len(xc.TEMPLATE_HEADERS)
               for line in xc.row_to_lines(_row()))


def test_a_product_is_written_one_line_per_variant():
    """The mirror of the parser, which reads one line per variant and groups
    lines sharing a SKU. A report written any other way cannot be re-uploaded."""
    lines = xc.row_to_lines(_row())
    assert len(lines) == 2
    colour = xc.TEMPLATE_HEADERS.index("Color")
    assert [line[colour] for line in lines] == ["Yellow", "Blue"]


def test_a_colour_only_product_leaves_the_size_cell_empty():
    lines = xc.row_to_lines(_row(sizes=[]))
    assert all(line[xc.TEMPLATE_HEADERS.index("Size")] == "" for line in lines)


def test_each_line_carries_its_own_size():
    row = _row(sizes=["S", "M"],
               combinations=["SKU-1 YW S", "SKU-1 YW M"],
               colors=["Yellow"], swatches=["Yellow"],
               variants=[{"color": "Yellow", "size": "S", "sku": "SKU-1 YW S"},
                         {"color": "Yellow", "size": "M", "sku": "SKU-1 YW M"}])
    size = xc.TEMPLATE_HEADERS.index("Size")
    assert [line[size] for line in xc.row_to_lines(row)] == ["S", "M"]


def test_booleans_render_as_yes_no():
    cells = xc.row_to_lines(_row(generate_combinations=True,
                                 allow_out_of_stock=False))[0]
    assert cells[8] == "Yes"
    assert cells[9] == "No"


def test_each_line_carries_one_child_sku():
    lines = xc.row_to_lines(_row())
    assert [line[10] for line in lines] == ["SKU-1 YW", "SKU-1 BE"]


def test_a_swatch_stays_with_its_colour():
    row = _row(colors=["Yellow", "Blue"], swatches=["Hi-Vis Yellow", "Royal Blue"])
    swatch = xc.TEMPLATE_HEADERS.index("Color Swatch")
    assert [line[swatch] for line in xc.row_to_lines(row)] == \
           ["Hi-Vis Yellow", "Royal Blue"]


def test_a_product_with_no_variants_still_gets_a_line():
    """A row that failed before it had any must survive the report so it can be
    corrected in place, rather than vanishing from the file it was in."""
    lines = xc.row_to_lines(_row(variants=[], combinations=[]))
    assert len(lines) == 2                          # one per colour
    assert all(line[10] == "" for line in lines)    # no child SKU to write


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
    assert wb["HardHat"].max_row == 5                    # header + 2 products x 2
    assert wb["Vest"].max_row == 3


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
