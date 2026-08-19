#!/usr/bin/env python3
"""
Write the automation's results back out as an .xlsx — the OUTPUT side.

This is the guardrail that closes the loop. The app parses the uploaded
workbook into rows; main.run drives Playwright and produces a run summary; this
module turns (original rows + that summary) into a new workbook the user gets
back, so they can see — per product — what was Added, what Failed, and what was
never reached because the run stopped midway:

    xlsx -> parser -> main.run -> Playwright -> summary -> xlsxCreator.py -> out.xlsx

The output mirrors the input template (one sheet per category, same 12 columns)
and appends two columns:

    Status   Added | Failed | Pending | Skipped
    Note     the error message for a failed row, or why it was skipped

Because every row carries a Status, the user can re-upload this same file to
finish the batch: the app re-parses it into rows (carrying Status into each
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

STATUS_ADDED = "Added"
STATUS_FAILED = "Failed"
STATUS_PENDING = "Pending"
STATUS_SKIPPED = "Skipped"

# Row `status` values (e.g. carried in from a re-uploaded report) that mean
# "already handled, don't add again". Kept in sync with main.py's DONE_STATUSES.
DONE_STATUSES = {"added", "done", "success", "complete", "skipped"}


def _yn(value) -> str:
    return "Yes" if value else "No"


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
    swatch_for = dict(zip(row.get("colors") or [], row.get("swatches") or []))

    def line(colour: str, size: str | None, child: str) -> list[str]:
        return [
            row.get("product_name", ""),
            row.get("sku", ""),
            row.get("spec_value", ""),
            row.get("color_attr", "Color"),
            colour,
            swatch_for.get(colour, ""),
            row.get("size_attr", "Size"),
            size or "",
            _yn(row.get("generate_combinations")),
            _yn(row.get("allow_out_of_stock")),
            child,
            _yn(row.get("save_product")),
        ]

    variants = row.get("variants") or []
    if variants:
        return [line(v.get("color") or "", v.get("size"), v.get("sku") or "")
                for v in variants]

    paired = _recover_pairing(row)
    colours = row.get("colors") or [""]
    sizes = row.get("sizes") or [None]
    return [line(colour, size, paired.get((colour, size), ""))
            for colour in colours for size in sizes]


def _recover_pairing(row: dict) -> dict:
    """{(colour, size): child SKU} for a row that did not state one."""
    if not row.get("combinations"):
        return {}
    try:
        import combination_skus
        return combination_skus.combination_map(row)
    except Exception:
        return {}


def status_for(row: dict, results_index: dict) -> tuple[str, str]:
    """Decide (Status, Note) for one row.

    Precedence:
      1. a result the runner reported for this SKU (Added / Failed, or an
         explicit status the runner set) wins;
      2. else a validation error on the row itself -> Failed;
      3. else a carried-in "already done" status -> Added;
      4. else the run never reached it -> Pending.
    """
    sku = row.get("sku")
    entry = results_index.get(sku)
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
    results_index = {r.get("sku"): r for r in (summary or {}).get("results", [])}

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
        ws.append(TEMPLATE_HEADERS + EXTRA_HEADERS)
        for row in sheet_rows:
            status, note = status_for(row, results_index)
            # Counted per PRODUCT, written per LINE. The tally is what the run
            # did — five products added — and repeating the status down a
            # product's eight lines would make it read as forty.
            counts[status] = counts.get(status, 0) + 1
            for cells in row_to_lines(row):
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
