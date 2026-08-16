"""Parse the uploaded workbook into rows — the INPUT side.

    app -> parser.py -> rows -> runner -> summary -> xlsxCreator.py -> out.xlsx

The inverse of xlsxCreator, and deliberately its mirror: the same 12 template
columns, read instead of written. A template change is an edit in two files and
the round-trip test (tests/test_parser.py) fails until both are done.

    result = parse_workbook(path)
    result.rows      -> the same shape main.load_rows() produces
    result.errors    -> problems that stopped a sheet being read at all

WHAT THIS REFUSES TO DO IS THE POINT. A column it does not recognise is
reported, never ignored. An unexpected attribute column (say "Material") must
not be dropped in silence — the product would be created with only colours and
sizes, the combinations generated over two dimensions, and the report would
still say "Added". A wrong catalogue with a clean report is the worst outcome
available here, so an unknown column is a row-level error and the row does not
run.

Validation errors land on the row rather than raising: the user should see
every problem in their workbook at once, not the first one. main.partition()
then keeps errored rows out of the run, and main.run() refuses to commit a
batch containing any.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl

log = logging.getLogger("parser")

#: Header text -> the row field it fills. Matched case-insensitively with
#: whitespace collapsed, because hand-maintained workbooks are inconsistent
#: about both. The two "Product Attributes Type" columns are positional — they
#: share a header, so they are resolved by which side of the sheet they are on.
HEADER_MAP: dict[str, str] = {
    "product name": "product_name",
    "sku": "sku",
    "attribute specification value": "spec_value",
    "color": "colors",
    "colour": "colors",
    "color swatch": "swatches",
    "colour swatch": "swatches",
    "sizes": "sizes",
    "size": "sizes",
    "generate combinations": "generate_combinations",
    "allow out of stock": "allow_out_of_stock",
    "child sku varient": "combinations",
    "child sku variant": "combinations",
    "save product": "save_product",
    "status": "status",
    "note": "note",
}

ATTRIBUTE_TYPE_HEADER = "product attributes type"

TRUTHY = {"yes", "y", "true", "1", "on"}
FALSEY = {"no", "n", "false", "0", "off", "na", ""}

#: Prefix on the duplicate warning. The console keys the "remove them" control
#: off it, so it is a contract between the two, not decoration.
DUPLICATE_WARNING = "duplicated:"

#: Short row-level notes. The sheet-level banner carries the full instruction —
#: these exist so a row with the problem does not tick itself for committing.
SIZE_COLUMN_MISSING = ("no second 'Product Attributes Type' column to name the "
                       "size attribute — assuming 'Size'")
SIZE_COLUMN_WRONG = ("the second 'Product Attributes Type' column should say "
                     "'Size', not the colour attribute — assuming 'Size'")


@dataclass
class ParseResult:
    rows: list[dict] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Structural problems with a sheet's TEMPLATE rather than its data — a
    #: missing or mislabelled column. Separate from `errors` because the sheet
    #: was read fine, and separate from row warnings because the fix is one
    #: edit to a column, not twenty edits to twenty rows.
    sheet_warnings: list[str] = field(default_factory=list)
    source_file: str = ""

    @property
    def valid_rows(self) -> int:
        return sum(1 for r in self.rows if not r["errors"])

    def to_payload(self) -> dict:
        """The same envelope main.load_rows() accepts."""
        return {
            "source_file": self.source_file,
            "sheets": self.sheets,
            "total_rows": len(self.rows),
            "valid_rows": self.valid_rows,
            "sheet_errors": self.errors,
            "sheet_warnings": self.sheet_warnings,
            "rows": self.rows,
        }


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


def _text(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).strip()


def _flag(value: Any, *, default: bool = False) -> bool:
    text = _norm(value)
    if text in TRUTHY:
        return True
    if text in FALSEY:
        return False
    return default


def _split(value: Any, *, sep: str = ",") -> list[str]:
    """Split a cell into a list. Handles both separators the template uses:
    newlines (the combination column) and commas (colours, sizes)."""
    text = _text(value)
    if not text or _norm(text) == "na":
        return []
    parts = re.split(r"[\n\r]+", text) if "\n" in text or "\r" in text else text.split(sep)
    return [p.strip() for p in parts if p.strip()]


def _map_headers(header_row: tuple) -> tuple[dict[int, str], list[str]]:
    """Column index -> field name, plus any headers we did not recognise.

    The two attribute-type columns share the header "Product Attributes Type".
    They are told apart by position: the first is the colour attribute, the
    second the size attribute, which is how the template lays them out.
    """
    mapping: dict[int, str] = {}
    unknown: list[str] = []
    attribute_columns = 0

    for index, cell in enumerate(header_row):
        header = _norm(cell)
        if not header:
            continue
        if header == ATTRIBUTE_TYPE_HEADER:
            attribute_columns += 1
            mapping[index] = "color_attr" if attribute_columns == 1 else "size_attr"
        elif header in HEADER_MAP:
            mapping[index] = HEADER_MAP[header]
        else:
            unknown.append(str(cell).strip())
    return mapping, unknown


def _parse_row(values: tuple, mapping: dict[int, str], *, sheet: str,
               row_number: int, unknown_headers: list[str]) -> dict | None:
    """One worksheet row -> one payload row, or None if the line is blank."""
    raw = {field: values[i] for i, field in mapping.items() if i < len(values)}
    if not any(_text(v) for v in raw.values()):
        return None

    row: dict[str, Any] = {
        "sku": _text(raw.get("sku")),
        "product_name": _text(raw.get("product_name")) or _text(raw.get("sku")),
        "spec_value": _text(raw.get("spec_value")),
        "color_attr": _text(raw.get("color_attr")) or "Color",
        "colors": _split(raw.get("colors")),
        "swatches": _split(raw.get("swatches")),
        "size_attr": _text(raw.get("size_attr")) or "Size",
        "sizes": _split(raw.get("sizes")),
        "generate_combinations": _flag(raw.get("generate_combinations")),
        "allow_out_of_stock": _flag(raw.get("allow_out_of_stock")),
        "combinations": _split(raw.get("combinations")),
        "duplicates": {},          # filled by _validate; the console offers a fix
        "save_product": _flag(raw.get("save_product"), default=True),
        "source_sheet": sheet,
        "source_row": row_number,
        "errors": [],
        "warnings": [],
    }

    status = _text(raw.get("status"))
    if status:
        row["status"] = status                 # a re-uploaded report: resume it

    # The two "Product Attributes Type" columns are positional, so the second
    # one naming "Color" names the SAME attribute twice. Not a harmless typo:
    # add_attribute is idempotent BY NAME, so the size pass would fetch the
    # colour attribute back and file "OS" as a ninth colour. A blank cell
    # already falls back to "Size"; a cell repeating the colour attribute means
    # the same thing, so it is treated the same way.
    #
    # The row warning stays short because the full explanation belongs to the
    # SHEET — see _check_attribute_columns. Repeating three sentences on every
    # one of twenty rows is noise, but the row still needs a warning so it does
    # not tick itself for committing.
    if row["sizes"]:
        if "size_attr" not in mapping.values():
            row["warnings"].append(SIZE_COLUMN_MISSING)
            row["size_attr"] = "Size"
        elif row["size_attr"] == row["color_attr"]:
            row["warnings"].append(SIZE_COLUMN_WRONG)
            row["size_attr"] = "Size"

    _validate(row, unknown_headers)
    return row


def _validate(row: dict, unknown_headers: list[str]) -> None:
    """Everything wrong with this row, all at once."""
    errors, warnings = row["errors"], row["warnings"]

    if not row["sku"]:
        errors.append("SKU is required")
    if not row["product_name"]:
        errors.append("Product name is required")

    # The silent-drop guard. See the module docstring: an unrecognised column is
    # very likely an attribute this pipeline cannot yet model, and creating the
    # product without it produces a wrong catalogue and a clean report.
    if unknown_headers:
        errors.append(
            f"unrecognised column(s) {unknown_headers} — this automation only "
            f"models colour and size attributes, and will not create a product "
            f"from a sheet it does not fully understand")

    if row["swatches"] and len(row["swatches"]) != len(row["colors"]):
        warnings.append(
            f"{len(row['swatches'])} swatch(es) for {len(row['colors'])} colour(s)")

    # A WARNING WITH A DECISION ATTACHED, not something to fix silently.
    # CP-20-200 lists "…Clear Lens" twice and "CP-20-200 BK C" twice: 13
    # entries, 12 distinct. Dropping the extra is almost certainly right — but
    # only almost. If that second entry was meant to be a different lens
    # somebody forgot to rename, removing it deletes a product variant and says
    # nothing. So the console offers to remove them and a human decides; left
    # alone, the row behaves exactly as it does today (it fails the count check
    # in combination_skus, which is the honest outcome for contradictory data).
    duplicate_colours = _repeated(row["colors"])
    duplicate_skus = _repeated(row["combinations"])
    if duplicate_colours or duplicate_skus:
        row["duplicates"] = {"colors": duplicate_colours,
                             "combinations": duplicate_skus}
        parts = []
        if duplicate_colours:
            parts.append(f"{len(duplicate_colours)} colour(s) {duplicate_colours}")
        if duplicate_skus:
            parts.append(f"{len(duplicate_skus)} child SKU(s) {duplicate_skus}")
        warnings.append(
            f"{DUPLICATE_WARNING} {' and '.join(parts)} listed more than once — "
            f"remove the extras, or leave them and this row will fail when its "
            f"combinations are counted")

    if row["generate_combinations"]:
        if not row["colors"]:
            errors.append("Generate Combinations is set but no colours are listed")
        expected = len(row["colors"]) * max(1, len(row["sizes"]))
        actual = len(row["combinations"])
        if actual and actual != expected:
            errors.append(
                f"{actual} child SKU(s) for {len(row['colors'])} colour(s) x "
                f"{max(1, len(row['sizes']))} size(s) — expected {expected}")
        if not actual:
            errors.append("Generate Combinations is set but no child SKUs are listed")

    # A WARNING, NOT AN ERROR — deliberately, and it is the one check here that
    # is. The others describe data this pipeline cannot act on; this one
    # describes data it can. A child SKU that does not extend the parent is
    # nearly always one mistyped cell (SKU "20-100ECO" against children
    # "CP-20-100ECO C/CAF/S/SAF"), and combination_skus.base_prefix recovers
    # the real base from the combinations themselves, so the row still resolves
    # to the right colour codes and the right child SKUs.
    #
    # What the parent SKU actually governs is the product this automation looks
    # up and creates; the children are written verbatim from the sheet. So the
    # mismatch cannot silently produce a wrong child SKU — the failure mode the
    # module docstring is about — and blocking the batch over it only stopped
    # the other rows from committing too.
    mismatched = [c for c in row["combinations"]
                  if row["sku"] and not c.upper().startswith(row["sku"].upper())]
    if mismatched:
        shown = ", ".join(repr(c) for c in mismatched[:3])
        more = f" (+{len(mismatched) - 3} more)" if len(mismatched) > 3 else ""
        warnings.append(
            f"{len(mismatched)} child SKU(s) do not start with {row['sku']!r} — "
            f"{shown}{more}. They will be written exactly as listed; check the "
            f"parent SKU is right before committing.")


def parse_workbook(source: str | Path | Any, *, filename: str = "") -> ParseResult:
    """Read every sheet of the workbook into rows.

    `source` is a path or any file-like object, so the web layer can hand this
    an upload stream without writing it to disk first.
    """
    result = ParseResult(source_file=filename or str(source))

    try:
        workbook = openpyxl.load_workbook(source, data_only=True, read_only=True)
    except Exception as exc:
        result.errors.append(f"could not open the workbook: {exc}")
        return result

    for worksheet in workbook.worksheets:
        sheet = worksheet.title
        rows = list(worksheet.iter_rows(values_only=True))
        if not rows:
            result.errors.append(f"{sheet}: empty sheet")
            continue

        result.sheets.append(sheet)
        mapping, unknown = _map_headers(rows[0])

        if "sku" not in mapping.values():
            result.errors.append(
                f"{sheet}: no 'SKU' column — found {[str(c) for c in rows[0] if c]}")
            continue
        if unknown:
            log.warning("%s: unrecognised column(s) %s", sheet, unknown)

        parsed_rows = []
        for offset, values in enumerate(rows[1:], start=2):
            parsed = _parse_row(values, mapping, sheet=sheet, row_number=offset,
                                unknown_headers=unknown)
            if parsed:
                parsed_rows.append(parsed)

        _check_attribute_columns(sheet, mapping, parsed_rows, result)
        result.rows.extend(parsed_rows)

    workbook.close()

    duplicates = _duplicate_skus(result.rows)
    for row in result.rows:
        if row["sku"] in duplicates:
            row["errors"].append(f"SKU {row['sku']!r} appears more than once")

    log.info("parsed %s — %d row(s), %d valid, %d sheet error(s)",
             result.source_file, len(result.rows), result.valid_rows,
             len(result.errors))
    return result


def _check_attribute_columns(sheet: str, mapping: dict[int, str],
                             rows: list[dict], result: ParseResult) -> None:
    """Is this sheet's size attribute actually named anywhere?

    The template carries the header "Product Attributes Type" TWICE — once for
    the colour attribute and once for the size attribute — and they are told
    apart by position, because they are spelled identically. That makes them
    easy to get wrong: a missing second column used to fall back to "Size" in
    silence, and a second column repeating "Color" made the runner file every
    size as a colour. The check below reports that instead.

    Reported once per sheet and in the user's vocabulary. The header they can
    search for is "Product Attributes Type"; "the size attribute column" names
    something that appears nowhere in their spreadsheet.

    Only reported when the sheet actually uses sizes. A hard-hat sheet with
    Sizes = NA has no size attribute to name and nothing to fix.
    """
    if not any(row["sizes"] for row in rows):
        return

    example = next(row for row in rows if row["sizes"])
    if "size_attr" not in mapping.values():
        result.sheet_warnings.append(
            f"{sheet}: this sheet sets sizes (e.g. {example['sizes']} on "
            f"{example['sku']}) but has only ONE 'Product Attributes Type' "
            f"column. The template needs two: the first names the colour "
            f"attribute, the second names the size attribute. Add a second "
            f"'Product Attributes Type' column immediately before the Sizes "
            f"column and put 'Size' in every row. Assuming 'Size' for now.")
    elif any(row["warnings"] and SIZE_COLUMN_WRONG in row["warnings"]
             for row in rows):
        wrong = sum(1 for row in rows if SIZE_COLUMN_WRONG in row["warnings"])
        result.sheet_warnings.append(
            f"{sheet}: the SECOND 'Product Attributes Type' column (the one "
            f"just before Sizes) names the size attribute, but on {wrong} row(s) "
            f"it repeats the colour attribute instead. Put 'Size' in that column "
            f"on every row. Assuming 'Size' for now — left as it is, both "
            f"attributes would share one name and every size would be added as "
            f"a colour.")


def _repeated(values: list[str]) -> list[str]:
    """The entries appearing more than once, in first-seen order."""
    seen: dict[str, int] = {}
    for value in values:
        seen[value] = seen.get(value, 0) + 1
    return [value for value in dict.fromkeys(values) if seen[value] > 1]


def _duplicate_skus(rows: list[dict]) -> set[str]:
    seen: dict[str, int] = {}
    for row in rows:
        if row["sku"]:
            seen[row["sku"]] = seen.get(row["sku"], 0) + 1
    return {sku for sku, count in seen.items() if count > 1}
