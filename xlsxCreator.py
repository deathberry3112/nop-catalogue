#!/usr/bin/env python3
"""
Write the automation's results back out as an .xlsx — the OUTPUT side.

This is the guardrail that closes the loop. The app parses the user's uploaded
workbook into JSON; main.py drives Playwright and produces a run summary; this
module turns (original rows + that summary) into a new workbook the user gets
back, so they can see — per product — what was Added, what Failed, and what was
never reached because the run stopped midway:

    app -> JSON -> main.py -> Playwright -> summary -> xlsxCreator.py -> out.xlsx

The output mirrors the input template (one sheet per category, same 12 columns)
and appends two columns:

    Status   Added | Failed | Pending | Skipped
    Note     the error message for a failed row, or why it was skipped

Because every row carries a Status, the user can re-upload this same file to
finish the batch: the app re-parses it into JSON (carrying Status into each
row), and main.py skips any row already marked Added — so nothing is added
twice. That skip lives in main.py; this module just makes the Status honest.

main.run() calls write_report() at the end of a run; the web layer then serves
the file back to whoever uploaded the workbook. There are no command-line
arguments — running this file directly regenerates a report from the checked-in
payload with no run attached (so every row comes out Pending), which is a handy
way to eyeball the template:

    python xlsxCreator.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.stderr.write(
        "openpyxl is required to write .xlsx files.\n"
        f"  fix:  {sys.executable} -m pip install openpyxl\n"
    )
    raise SystemExit(2)


# The template's 12 columns, in order. The output reproduces these, then adds
# Status + Note. Kept here so a template change is a one-place edit.
TEMPLATE_HEADERS = [
    "Product name",
    "SKU",
    "Attribute Specification Value",
    "Product Attributes Type",   # color_attr
    "Color",
    "Color Swatch",
    "Product Attributes Type",   # size_attr
    "Size",
    "Generate Combinations",
    "Allow Out Of Stock",
    "Child SKU Varient",
    "Save Product",
]
EXTRA_HEADERS = ["Status", "Note"]

# Phase 2's template, in order — the mirror of app/configurator.py, the same way
# TEMPLATE_HEADERS mirrors app/parser.py. The two "Save" columns are spelled
# identically in the client's sheet and told apart by position, so they are
# written back that way too: a report that renamed them would not re-upload.
CONFIGURATOR_HEADERS = [
    "Product name",
    "SKU",
    "Configurator Areas",
    "Add new location",
    "Location Name",
    "Picario RefrenceId",
    "Save ",                     # the location
    "Picario Name",
    "Thumbnail Name",
    "Save ",                     # its areas
    "Save Product",
]

STATUS_ADDED = "Added"
STATUS_FAILED = "Failed"
STATUS_PENDING = "Pending"
STATUS_SKIPPED = "Skipped"

# Row `status` values (e.g. carried in from a re-uploaded report) that mean
# "already handled, don't add again". Kept in sync with main.py's DONE_STATUSES.
DONE_STATUSES = {"added", "done", "success", "complete", "skipped",
                 "configured"}


def _yn(value) -> str:
    return "Yes" if value else "No"


def configurator_to_lines(row: dict) -> list[list[str]]:
    """A phase-2 row -> its template lines, one per area.

    The mirror of app/configurator.parse_sheet, and it has to reproduce that
    sheet's one quirk to survive a round trip: the Picario reference is written
    on the FIRST line of a location and left blank on the rest, because a blank
    is what joins a line to the location above it. Repeating the reference on
    every line would re-parse as the same thing, but writing it on none of them
    would not — and the report exists to be re-uploaded.
    """
    lines: list[list[str]] = []
    for location in row.get("locations") or []:
        for index, area in enumerate(location.get("areas") or []):
            first = index == 0
            lines.append([
                row.get("product_name", ""),
                row.get("sku", ""),
                "Select",                       # navigation, as the template writes it
                "Yes" if first else "",
                location.get("name", ""),
                location.get("reference_id", "") if first else "",
                _yn(True) if first else "",
                area.get("picario_name", ""),
                area.get("thumbnail_asked")
                or (area.get("thumbnail", "") or "").rsplit("/", 1)[-1],
                _yn(True),
                _yn(row.get("save_product", True)),
            ])
    return lines


def attribute_columns(row: dict) -> list[dict]:
    """The attribute column groups this row writes back, in sheet order.

    A row parsed from a workbook says what its attributes are and whether each
    carried a swatch column. One loaded from JSON predates general attributes
    and is written back the way it always was: Color + Color Swatch, then Size.
    """
    attributes = row.get("attributes")
    if attributes:
        return [{"name": a["name"],
                 "values": a.get("values") or [],
                 "swatch": bool(a.get("has_swatch_column")),
                 "swatch_of": dict(zip(a.get("values") or [],
                                       a.get("swatches") or []))}
                for a in attributes]
    return [
        {"name": row.get("color_attr") or "Color",
         "values": row.get("colors") or [], "swatch": True,
         "swatch_of": dict(zip(row.get("colors") or [], row.get("swatches") or []))},
        {"name": row.get("size_attr") or "Size",
         "values": row.get("sizes") or [], "swatch": False, "swatch_of": {}},
    ]


def template_headers(row: dict) -> list[str]:
    """This row's own template columns.

    The mirror of app.parser._map_headers: a "Product Attributes Type" column
    naming each attribute, the values beside it, and a swatch column where the
    sheet had one. A Sign Boards row therefore comes back out as Sign Material
    and Sign Size, and re-uploading the report re-parses to the same product.
    """
    headers = ["Product name", "SKU", "Attribute Specification Value"]
    for column in attribute_columns(row):
        headers += ["Product Attributes Type", column["name"]]
        if column["swatch"]:
            headers.append(f"{column['name']} Swatch")
    headers += ["Generate Combinations", "Allow Out Of Stock",
                "Child SKU Varient", "Save Product"]
    return headers


def row_to_lines(row: dict) -> list[list[str]]:
    """A parsed row -> its template lines, one per variant.

    The mirror of app/parser: that reads one line per colour/size/child SKU and
    groups lines sharing a SKU into a product, so this writes a product back
    out the same way. It matters beyond symmetry — the report is meant to be
    re-uploaded to finish a batch, and a report written in the old aggregated
    shape would come back as a workbook the parser now refuses.

    A row that never stated its pairing — a payload written to the old template,
    or one loaded straight from JSON — has it recovered by combination_skus,
    which is the module that exists for exactly that. The report is not a place
    to fail: if the pairing cannot be recovered the child SKUs are left off
    rather than guessed at, and the run's Status and Note still come through,
    which is what the user opened the file for.

    A product with no variants at all (nothing to combine, or a row that failed
    before it had any) still gets one line per colour, so it survives the round
    trip and can be corrected in place rather than vanishing from the report.
    """
    import itertools

    columns = attribute_columns(row)

    def line(values: dict, child: str) -> list[str]:
        cells = [row.get("product_name", ""), row.get("sku", ""),
                 row.get("spec_value", "")]
        for column in columns:
            value = values.get(column["name"], "") or ""
            cells += [column["name"], value]
            if column["swatch"]:
                cells.append(column["swatch_of"].get(value, ""))
        cells += [_yn(row.get("generate_combinations")),
                  _yn(row.get("allow_out_of_stock")), child,
                  _yn(row.get("save_product"))]
        return cells

    def values_of(variant: dict) -> dict:
        stated = variant.get("values")
        if stated is not None:
            return stated
        # A variant written the colour/size way, from a JSON payload.
        pairs = {}
        if variant.get("color"):
            pairs[columns[0]["name"]] = variant["color"]
        if variant.get("size") and len(columns) > 1:
            pairs[columns[1]["name"]] = variant["size"]
        return pairs

    variants = row.get("variants") or []
    if variants:
        return [line(values_of(v), v.get("sku") or "") for v in variants]

    paired = _recover_pairing(row)
    names = [c["name"] for c in columns if c["values"]]
    grids = [c["values"] for c in columns if c["values"]] or [[""]]
    lines = []
    for combination in itertools.product(*grids):
        values = dict(zip(names, combination))
        key = tuple(combination)
        lines.append(line(values, paired.get(key, "")))
    return lines


def _recover_pairing(row: dict) -> dict:
    """{(colour, size): child SKU} for a row that did not state one."""
    if not row.get("combinations"):
        return {}
    try:
        import combination_skus
        return combination_skus.combination_map(row)
    except Exception:
        return {}


def _result_key(entry: dict) -> tuple[str, str]:
    """How a row and a runner result are matched up. See status_for."""
    return (entry.get("kind") or "product", entry.get("sku") or "")


def status_for(row: dict, results_index: dict) -> tuple[str, str]:
    """Decide (Status, Note) for one row.

    Precedence:
      1. a result the runner reported for this SKU (Added / Failed, or an
         explicit status the runner set) wins;
      2. else a validation error on the row itself -> Failed;
      3. else a carried-in "already done" status -> Added;
      4. else the run never reached it -> Pending.

    Indexed by (kind, SKU), not by SKU. A phase-1 product row and a phase-2
    configurator row are allowed to name the same SKU — that is what a workbook
    doing both looks like — and keying on the SKU alone would give one of them
    the other's status.
    """
    entry = results_index.get(_result_key(row))
    if entry is not None:
        explicit = str(entry.get("status", "")).strip()
        if explicit:
            return explicit.title(), entry.get("error") or ""
        if entry.get("error"):
            return STATUS_FAILED, str(entry["error"])
        return STATUS_ADDED, ""

    if row.get("errors"):
        return STATUS_FAILED, "; ".join(row["errors"])
    if str(row.get("status", "")).strip().lower() in DONE_STATUSES:
        return STATUS_ADDED, ""
    return STATUS_PENDING, ""


def write_report(rows: list[dict], summary: dict | None, output_path: str | Path) -> dict:
    """Write rows + their run status to an .xlsx, grouped by source sheet.

    Returns a small counts dict {added, failed, pending, ...} for logging.
    """
    results_index = {_result_key(r): r for r in (summary or {}).get("results", [])}

    # Group rows by their originating sheet, preserving first-seen order.
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("source_sheet") or "Products", []).append(row)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)                       # drop the default empty sheet

    counts: dict[str, int] = {}
    for sheet_name, sheet_rows in groups.items():
        # openpyxl caps sheet titles at 31 chars and forbids some characters.
        ws = wb.create_sheet(title=str(sheet_name)[:31])
        # A sheet holds one kind of row, because it came from one sheet of the
        # upload — so the headers are decided once, per sheet, from what is in
        # it rather than per line.
        configurator = any(r.get("kind") == "configurator" for r in sheet_rows)
        # A product sheet's columns come from its own attributes, so a Sign
        # Boards sheet is written back as Sign Material and Sign Size rather
        # than forced into Color and Size. Taken from the first row: one sheet
        # of the upload has one set of columns, which is what made them
        # positional in the first place.
        headers = (CONFIGURATOR_HEADERS if configurator
                   else template_headers(sheet_rows[0]))
        ws.append(headers + EXTRA_HEADERS)
        for row in sheet_rows:
            status, note = status_for(row, results_index)
            # Counted per PRODUCT, written per LINE. The tally is what the run
            # did — five products added — and repeating the status down a
            # product's eight lines would make it read as forty.
            counts[status] = counts.get(status, 0) + 1
            lines = (configurator_to_lines(row) if row.get("kind") == "configurator"
                     else row_to_lines(row))
            for cells in lines:
                ws.append(cells + [status, note])

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return counts


DEFAULT_OUTPUT = "report.xlsx"


if __name__ == "__main__":
    # No arguments: regenerate a report from the checked-in payload, with no run
    # summary, so every row lands as Pending. main.run() is what produces a real
    # one — this is just for looking at the template.
    import main

    try:
        rows = main.load_rows(main.DEFAULT_PAYLOAD)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Could not read {main.DEFAULT_PAYLOAD}: {exc}\n")
        sys.exit(2)

    counts = write_report(rows, None, DEFAULT_OUTPUT)
    tally = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no rows"
    sys.stderr.write(f"Wrote {DEFAULT_OUTPUT} — {tally}\n")
